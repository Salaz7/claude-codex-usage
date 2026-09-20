"""Usage Monitor: a tiny always-on-top Windows widget for Claude Code and Codex limits.

It is purely a monitor. It never switches accounts and never writes any credential
file, so it cannot conflict with Claude Code or Codex.

Data sources (read directly, no account switcher and no Codex CLI required):

  Claude Code
    Reads the OAuth access token that Claude Code stores on Windows in
    ~/.claude/.credentials.json (key claudeAiOauth.accessToken), then calls the
    Anthropic usage endpoint the same way Claude Code does:
        GET https://api.anthropic.com/api/oauth/usage
        Authorization: Bearer <token>
        anthropic-beta: oauth-2025-04-20
    The response carries the 5-hour window, the 7-day (weekly) window, and a
    per-model "limits" array whose entries include Fable.

  Codex
    Codex writes a rate_limits snapshot into its session rollout logs at
    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. We read the most recent
    snapshot (primary and secondary windows, classified by window_minutes).

Only the Python standard library is used, so the packaged .exe needs nothing
installed on the target machine.
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import queue
import random
import struct
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from tkinter import font as tkfont

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

APP_NAME = "UsageMonitor"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "usage-monitor/1.0 (+local)"

# How often each source is polled, in seconds. Claude is deliberately slower
# than Codex because it hits a network endpoint whose per-client budget cswap
# measured at ~28-30 requests per rolling hour (a sliding window, so a burst can
# lock the client out for up to an hour). 180s is 20 requests/hour, safely under
# that; the 429 recovery policy below handles the rest. Countdown timers still
# tick live every second regardless. Both are overridable in config.json.
DEFAULT_CLAUDE_POLL = 180
DEFAULT_CODEX_POLL = 10
TICK_MS = 1000

# 429 recovery for the usage endpoint. Because the budget is a sliding hour and
# retrying near the server's deadline tends to re-block, a 429 makes us hold the
# cadence well above normal for a full window (growing it while 429s persist),
# honor a positive Retry-After plus a margin, and jitter every interval so
# independent pollers do not fetch in lockstep. See plan_claude_interval.
POST_429_MIN_POLL = 360.0     # floor after any 429 (6 min)
POST_429_MAX_POLL = 1800.0    # ceiling while 429s persist (30 min)
POST_429_BACKOFF_MULT = 1.5   # grow the interval per recurring 429
RECENT_429_WINDOW = 3600.0    # keep the floor this long after the last 429
RETRY_AFTER_MARGIN = 900.0    # added to a positive Retry-After before waiting
RETRY_AFTER_MAX = 4500.0      # cap on the total honored wait
POLL_JITTER_FRAC = 0.1        # +/-10% so independent pollers do not sync

FIVE_HOUR_S = 5 * 3600
SEVEN_DAY_S = 7 * 86400

# Palette (dark).
COL = {
    "bg": "#0d1117",
    "panel": "#161b22",
    "border": "#30363d",
    "track": "#21262d",
    "text": "#e6edf3",
    "dim": "#8b949e",
    "faint": "#6e7681",
    "green": "#3fb950",
    "amber": "#d29922",
    "red": "#f85149",
    "claude": "#d97757",  # Anthropic clay
    "codex": "#10a37f",   # OpenAI green
    "pace": "#c9d1d9",
    "btn_hover": "#f85149",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def parse_reset(value) -> datetime | None:
    """Parse a reset time that is either an ISO string or a unix timestamp."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def human_countdown(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    if mins:
        return f"{mins}m"
    return f"{seconds}s"


def human_ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    secs = (now_utc() - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def expected_pct(reset_dt: datetime | None, window_s: int | None) -> float | None:
    """Where usage 'should' be right now if spent evenly across the window."""
    if reset_dt is None or not window_s:
        return None
    remaining = (reset_dt - now_utc()).total_seconds()
    elapsed = window_s - remaining
    frac = elapsed / window_s
    return max(0.0, min(100.0, frac * 100.0))


def plan_claude_interval(
    *,
    base_poll: float,
    prev_interval: float,
    had_429: bool,
    retry_after: float | None,
    recent_429: bool,
    rng=random.random,
) -> float:
    """Seconds until the next Claude usage poll, given the last poll's outcome.

    Follows cswap's discipline for the shared usage-endpoint budget:

    - a 429 with a positive Retry-After honors it plus ``RETRY_AFTER_MARGIN``
      (retrying on the server's own deadline tends to re-block), capped at
      ``RETRY_AFTER_MAX`` and never below ``POST_429_MIN_POLL``;
    - any other 429 (Retry-After 0 or absent) grows the previous interval by
      ``POST_429_BACKOFF_MULT`` toward ``POST_429_MAX_POLL``, floored at
      ``POST_429_MIN_POLL`` (additive-increase congestion control);
    - a success while a 429 is still recent holds that floor, so the saturated
      rolling hour ages out instead of being re-spent;
    - a clean success returns the normal configured cadence.

    Every result gets +/-``POLL_JITTER_FRAC`` jitter so independent pollers do
    not fetch in lockstep. ``rng`` (0..1) is injectable for deterministic tests.
    """
    if had_429:
        if retry_after and retry_after > 0:
            interval = min(retry_after + RETRY_AFTER_MARGIN, RETRY_AFTER_MAX)
            interval = max(interval, POST_429_MIN_POLL)
        else:
            grown = max(prev_interval * POST_429_BACKOFF_MULT, POST_429_MIN_POLL)
            interval = min(grown, POST_429_MAX_POLL)
    elif recent_429:
        interval = max(base_poll, POST_429_MIN_POLL)
    else:
        interval = base_poll
    return interval * (1.0 + POLL_JITTER_FRAC * (2.0 * rng() - 1.0))


# --------------------------------------------------------------------------- #
# Claude Code data
# --------------------------------------------------------------------------- #

class UsageError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def claude_config_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else (Path.home() / ".claude")


def read_claude_oauth() -> dict:
    """Return the claudeAiOauth object from Claude Code's credentials file.

    Raises UsageError('noauth', ...) when there is no OAuth login on disk.
    """
    cred_file = claude_config_home() / ".credentials.json"
    if not cred_file.exists():
        raise UsageError("noauth", "Claude Code not logged in")
    try:
        data = json.loads(cred_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise UsageError("noauth", f"Cannot read credentials ({e})")
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise UsageError("noauth", "No Claude OAuth token")
    return oauth


def read_claude_email() -> str | None:
    """Best-effort account email from ~/.claude.json (may lag after a switch)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    candidates = []
    if env:
        candidates.append(Path(env) / ".claude.json")
    candidates.append(Path.home() / ".claude.json")
    for path in candidates:
        try:
            if not path.exists() or path.stat().st_size > 25_000_000:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            acct = data.get("oauthAccount")
            if isinstance(acct, dict):
                email = acct.get("emailAddress")
                if email:
                    return str(email)
        except Exception:
            continue
    return None


def fetch_claude_usage() -> dict:
    oauth = read_claude_oauth()
    token = oauth["accessToken"]
    sub = oauth.get("subscriptionType")

    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UsageError("stale", "Token stale (Claude will refresh on use)")
        if e.code == 429:
            retry = None
            try:
                retry = int(e.headers.get("Retry-After")) if e.headers else None
            except Exception:
                retry = None
            err = UsageError("ratelimit", "Rate-limited, backing off")
            err.retry_after = retry  # type: ignore[attr-defined]
            raise err
        raise UsageError("http", f"HTTP {e.code}")
    except urllib.error.URLError:
        raise UsageError("network", "Offline")
    except Exception as e:
        raise UsageError("network", f"{e}")

    return {"windows": build_claude_windows(raw), "sub": sub, "at": now_utc()}


def build_claude_windows(raw: dict) -> list[dict]:
    """Normalize a raw usage-endpoint response into window dicts (pure, no IO)."""
    windows: list[dict] = []

    h5 = raw.get("five_hour")
    if isinstance(h5, dict) and h5.get("utilization") is not None:
        windows.append({
            "name": "5-hour",
            "pct": float(h5["utilization"]),
            "reset_dt": parse_reset(h5.get("resets_at")),
            "window_s": FIVE_HOUR_S,
        })

    d7 = raw.get("seven_day")
    if isinstance(d7, dict) and d7.get("utilization") is not None:
        windows.append({
            "name": "Weekly",
            "pct": float(d7["utilization"]),
            "reset_dt": parse_reset(d7.get("resets_at")),
            "window_s": SEVEN_DAY_S,
        })

    limits = raw.get("limits")
    if isinstance(limits, list):
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            scope = lim.get("scope") if isinstance(lim.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = model.get("display_name")
            pct = lim.get("percent")
            if not name or not isinstance(pct, (int, float)):
                continue
            windows.append({
                "name": str(name),
                "pct": float(pct),
                "reset_dt": parse_reset(lim.get("resets_at")),
                "window_s": SEVEN_DAY_S,
            })

    return windows


# --------------------------------------------------------------------------- #
# Codex data
# --------------------------------------------------------------------------- #

def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else (Path.home() / ".codex")


def _tail_text(path: Path, nbytes: int = 262_144) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", "ignore")


def codex_window_label(minutes) -> str:
    if not minutes:
        return "Limit"
    m = int(minutes)
    if 240 <= m <= 360:
        return "5-hour"
    if m <= 90:
        return f"{m}m"
    if m == 1440:
        return "Daily"
    if m == 10080:
        return "Weekly"
    if m % 1440 == 0:
        return f"{m // 1440}d"
    return f"{m // 60}h"


def fetch_codex_usage() -> dict:
    sessions = codex_home() / "sessions"
    if not sessions.exists():
        raise UsageError("noauth", "No Codex sessions found")

    files = glob.glob(str(sessions / "**" / "rollout-*.jsonl"), recursive=True)
    if not files:
        raise UsageError("noauth", "No Codex sessions found")
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    best_ts: datetime | None = None
    best_rl: dict | None = None

    for path in files[:5]:
        try:
            text = _tail_text(Path(path))
        except Exception:
            continue
        for line in reversed(text.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            payload = obj.get("payload") if isinstance(obj, dict) else None
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            if not isinstance(rl, dict):
                continue
            ts = parse_reset(obj.get("timestamp"))
            if ts is None:
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_rl = rl
            break  # newest rate_limits line in this file is enough

    if best_rl is None:
        raise UsageError("nodata", "No Codex usage recorded yet")

    windows: list[dict] = []
    for slot in ("primary", "secondary"):
        w = best_rl.get(slot)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        minutes = w.get("window_minutes")
        windows.append({
            "name": codex_window_label(minutes),
            "pct": float(w["used_percent"]),
            "reset_dt": parse_reset(w.get("resets_at")),
            "window_s": int(minutes) * 60 if minutes else None,
        })

    windows.sort(key=lambda x: x["window_s"] or 0)
    plan = best_rl.get("plan_type")
    return {"windows": windows, "plan": plan, "at": best_ts}


# --------------------------------------------------------------------------- #
# System tray icon (pure Win32 via ctypes, no third-party dependency)
# --------------------------------------------------------------------------- #

# A compact 3x5 pixel font, enough to render usage percentages on the icon.
_FONT = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    "-": ["000", "000", "111", "000", "000"],
    " ": ["000", "000", "000", "000", "000"],
}

_TILE = (0x18, 0x1f, 0x2b)
_TRACK = (0x2a, 0x30, 0x3b)


def _blank(size: int) -> bytearray:
    return bytearray(size * size * 4)  # top-down BGRA


def _set(px, w, x, y, rgb, a=255):
    if x < 0 or y < 0 or x >= w:
        return
    i = (y * w + x) * 4
    if i < 0 or i + 3 >= len(px):
        return
    px[i], px[i + 1], px[i + 2], px[i + 3] = rgb[2], rgb[1], rgb[0], a


def _draw_tile(px, w, h):
    m = max(1, w // 16)
    rad = max(3, w // 5)
    x0, y0, x1, y1 = m, m, w - 1 - m, h - 1 - m
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            cx = min(max(x, x0 + rad), x1 - rad)
            cy = min(max(y, y0 + rad), y1 - rad)
            if (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad:
                _set(px, w, x, y, _TILE)


def _draw_bars(px, w, h):
    m = max(1, w // 16)
    x0, x1 = m, w - 1 - m
    bx0 = x0 + max(2, w // 8)
    bx1 = x1 - max(2, w // 8)
    barw = bx1 - bx0
    bh = max(2, w // 8)
    gap = max(1, w // 16)
    rows = [((0x3f, 0xb9, 0x50), 0.45), ((0xd2, 0x99, 0x22), 0.72), ((0xf8, 0x51, 0x49), 1.0)]
    by = (h - (len(rows) * bh + (len(rows) - 1) * gap)) // 2
    for color, frac in rows:
        for y in range(by, by + bh):
            for x in range(bx0, bx1):
                _set(px, w, x, y, _TRACK)
            for x in range(bx0, bx0 + int(barw * frac)):
                _set(px, w, x, y, color)
        by += bh + gap


def _draw_number(px, w, x0, y0, x1, y1, text, rgb):
    """Draw digits centered inside the box, scaled to fit."""
    if not text:
        return
    n = len(text)
    cells_w = n * 3 + (n - 1)  # digits are 3 wide with a 1-cell gap
    box_w, box_h = x1 - x0 + 1, y1 - y0 + 1
    b = max(1, min(box_w // cells_w, box_h // 5))
    tw, th = cells_w * b, 5 * b
    sx = x0 + (box_w - tw) // 2
    sy = y0 + (box_h - th) // 2
    cx = sx
    for ch in text:
        glyph = _FONT.get(ch, _FONT[" "])
        for r in range(5):
            for c in range(3):
                if glyph[r][c] == "1":
                    for dy in range(b):
                        for dx in range(b):
                            _set(px, w, cx + c * b + dx, sy + r * b + dy, rgb)
        cx += (3 + 1) * b


def _pixels_to_dib(px, w, h) -> bytes:
    """Pack a top-down BGRA buffer into an icon DIB (header + XOR + AND mask)."""
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        xor += px[y * w * 4:(y + 1) * w * 4]
    and_row = ((w + 31) // 32) * 4
    andmask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(and_row)
        for x in range(w):
            if px[(y * w + x) * 4 + 3] == 0:
                bits[x // 8] |= (0x80 >> (x % 8))
        andmask += bits
    bih = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(andmask), 0, 0, 0, 0)
    return bih + bytes(xor) + bytes(andmask)


def _dib_to_ico(dib: bytes, size: int) -> bytes:
    icondir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size & 0xFF, size & 0xFF, 0, 0, 1, 32, len(dib), 22)
    return icondir + entry + dib


def bars_dib(size: int = 32) -> bytes:
    px = _blank(size)
    _draw_tile(px, size, size)
    _draw_bars(px, size, size)
    return _pixels_to_dib(px, size, size)


def meter_dib(size: int, rows) -> bytes:
    """Icon DIB showing one or two colored numbers on the dark tile.

    ``rows`` is a list of ``(text, rgb)``; one entry fills the tile, two entries
    stack (top and bottom).
    """
    px = _blank(size)
    _draw_tile(px, size, size)
    m = max(1, size // 16)
    pad = max(1, size // 32)  # tight inner padding so the digits render larger
    x0, x1 = m + pad, size - 1 - m - pad
    y0, y1 = m + pad, size - 1 - m - pad
    if len(rows) <= 1:
        if rows:
            _draw_number(px, size, x0, y0, x1, y1, rows[0][0], rows[0][1])
    else:
        gap = max(1, size // 32)  # thin gap so each stacked number is taller
        row_h = (y1 - y0 + 1 - gap) // 2  # equal-height rows so digits match
        _draw_number(px, size, x0, y0, x1, y0 + row_h - 1, rows[0][0], rows[0][1])
        _draw_number(px, size, x0, y1 - row_h + 1, x1, y1, rows[1][0], rows[1][1])
    return _pixels_to_dib(px, size, size)


def make_tray_ico(size: int = 32) -> bytes:
    """Full .ico bytes for the default three-bar icon (used for tests/preview)."""
    return _dib_to_ico(bars_dib(size), size)


if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _shell32 = ctypes.windll.shell32
    _kernel32 = ctypes.windll.kernel32

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", ctypes.c_wchar * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", ctypes.c_wchar * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hWnd", wintypes.HWND), ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD), ("pt", POINT),
        ]

    _user32.DefWindowProcW.restype = LRESULT
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    _user32.TrackPopupMenu.restype = ctypes.c_int
    _user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.CreateIconFromResourceEx.restype = wintypes.HICON
    _user32.CreateIconFromResourceEx.argtypes = [
        ctypes.POINTER(ctypes.c_byte), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
    WM_TRAY = 0x8000 + 1
    WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
    WM_DESTROY, WM_CLOSE, WM_NULL = 0x0002, 0x0010, 0x0000
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
    TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
    MF_STRING, MF_CHECKED, MF_SEPARATOR = 0x0, 0x8, 0x800
    WS_OVERLAPPED = 0x00000000
    CW_USEDEFAULT = -2147483648
    # Rendered at 64px so Windows scales the notification-area icon DOWN (crisp)
    # instead of upscaling a 32px source (blurry) on 150-250% displays, which
    # made the stacked numbers hard to read.
    ICON_SIZE = 64

    TRAY_CLASS = "UsageMonitorTrayCls"

    def _hicon_from_dib(dib: bytes):
        buf = (ctypes.c_byte * len(dib)).from_buffer_copy(dib)
        return _user32.CreateIconFromResourceEx(buf, len(dib), True, 0x00030000,
                                                ICON_SIZE, ICON_SIZE, 0)

    class Tray:
        """A notification-area icon backed by its own hidden window + message loop.

        Runs entirely in a background thread. User actions are posted as short
        strings onto ``cmd_queue`` for the Tk main thread to consume, so no Tk
        call ever happens off the main thread.
        """

        def __init__(self, cmd_queue, is_topmost, is_visible, tooltip="Usage Monitor"):
            self.q = cmd_queue
            self.is_topmost = is_topmost
            self.is_visible = is_visible
            self._tooltip = tooltip
            self.hwnd = None
            self.hicon = None
            self.nid = None
            self._lock = threading.Lock()
            self._taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
            self._ready = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self._ready.wait(3)

        def _wndproc(self, hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.q.put("toggle")
                elif event == WM_RBUTTONUP:
                    self._show_menu(hwnd)
                return 0
            if msg == self._taskbar_created:
                self._add_icon()
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _show_menu(self, hwnd):
            menu = _user32.CreatePopupMenu()
            _user32.AppendMenuW(menu, MF_STRING, 1,
                                "Hide widget" if self.is_visible() else "Show widget")
            _user32.AppendMenuW(menu, MF_STRING, 2, "Refresh now")
            _user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if self.is_topmost() else 0),
                                3, "Always on top")
            _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(menu, MF_STRING, 4, "Quit")
            pt = POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            _user32.SetForegroundWindow(hwnd)
            cmd = _user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                         pt.x, pt.y, 0, hwnd, None)
            _user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            _user32.DestroyMenu(menu)
            self.q.put({1: "toggle", 2: "refresh", 3: "top", 4: "quit"}.get(cmd, ""))

        def _add_icon(self):
            with self._lock:
                if self.nid is not None:
                    _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))

        def _run(self):
            try:
                hinst = _kernel32.GetModuleHandleW(None)
                self._wndproc_ref = WNDPROC(self._wndproc)  # keep a strong ref
                cls = WNDCLASS()
                cls.lpfnWndProc = self._wndproc_ref
                cls.hInstance = hinst
                cls.lpszClassName = TRAY_CLASS
                _user32.RegisterClassW(ctypes.byref(cls))
                self.hwnd = _user32.CreateWindowExW(
                    0, TRAY_CLASS, "UsageMonitorTray", WS_OVERLAPPED,
                    CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT,
                    None, None, hinst, None)
                try:
                    self.hicon = _hicon_from_dib(bars_dib(ICON_SIZE))
                except Exception:
                    self.hicon = None
                nid = NOTIFYICONDATA()
                nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
                nid.hWnd = self.hwnd
                nid.uID = 1
                nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
                nid.uCallbackMessage = WM_TRAY
                nid.hIcon = self.hicon
                nid.szTip = self._tooltip
                self.nid = nid
                _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            finally:
                self._ready.set()

            msg = MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            with self._lock:
                if self.nid is not None:
                    _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
                    self.nid = None
            if self.hicon:
                _user32.DestroyIcon(self.hicon)

        def set_tooltip(self, text):
            text = (text or "")[:127]
            with self._lock:
                if self.nid is None or self.nid.szTip == text:
                    return
                self.nid.szTip = text
                try:
                    _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))
                except Exception:
                    pass

        def set_meter(self, rows):
            """Swap the tray icon for one showing the given number rows."""
            try:
                dib = meter_dib(ICON_SIZE, rows)
                hicon = _hicon_from_dib(dib)
            except Exception:
                return
            if not hicon:
                return
            with self._lock:
                if self.nid is None:
                    _user32.DestroyIcon(hicon)
                    return
                old = self.hicon
                self.nid.hIcon = hicon
                _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))
                self.hicon = hicon
            if old:
                _user32.DestroyIcon(old)

        def stop(self):
            if self.hwnd:
                _user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self._thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# UI: one usage row (label + capsule bar + percent + reset countdown)
# --------------------------------------------------------------------------- #

def bar_color(pct: float) -> str:
    if pct >= 90:
        return COL["red"]
    if pct >= 70:
        return COL["amber"]
    return COL["green"]


def pick_meter_window(windows: list[dict]) -> dict | None:
    """The window the tray icon should display for a service.

    Prefers the Weekly window (the limit most worth watching at a glance);
    falls back to the worst (highest-percent) window when no weekly one is
    reported, and returns None when there are no windows at all.
    """
    if not windows:
        return None
    weekly = next((w for w in windows if w.get("name") == "Weekly"), None)
    return weekly or max(windows, key=lambda w: w["pct"])


class Row:
    def __init__(self, parent: tk.Widget, s, fonts: dict, grid_row: int):
        self.s = s
        self.px = lambda n: int(round(n * s))
        self.bar_w = self.px(150)
        self.bar_h = self.px(9)

        self.frame = tk.Frame(parent, bg=COL["bg"])
        self.frame.grid(row=grid_row, column=0, sticky="ew", pady=self.px(3))
        self.frame.columnconfigure(1, weight=1)

        self.name = tk.Label(
            self.frame, text="", bg=COL["bg"], fg=COL["text"], font=fonts["row"],
            anchor="w", width=8,
        )
        self.name.grid(row=0, column=0, sticky="w", padx=(0, self.px(8)))

        self.canvas = tk.Canvas(
            self.frame, width=self.bar_w, height=self.bar_h,
            bg=COL["bg"], highlightthickness=0, bd=0,
        )
        self.canvas.grid(row=0, column=1, sticky="w")

        self.pct = tk.Label(
            self.frame, text="", bg=COL["bg"], fg=COL["text"], font=fonts["pct"],
            anchor="e", width=5,
        )
        self.pct.grid(row=0, column=2, sticky="e", padx=(self.px(8), self.px(6)))

        self.reset = tk.Label(
            self.frame, text="", bg=COL["bg"], fg=COL["dim"], font=fonts["reset"],
            anchor="e", width=7,
        )
        self.reset.grid(row=0, column=3, sticky="e")

    def _rrect(self, x1, y1, x2, y2, r, **kw):
        if x2 - x1 < 2 * r:
            r = max(0, (x2 - x1) / 2)
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def draw_bar(self, pct: float, expected: float | None):
        c = self.canvas
        c.delete("all")
        w, h = self.bar_w, self.bar_h
        r = h / 2
        self._rrect(0, 0, w, h, r, fill=COL["track"], outline="")
        fill_w = max(0.0, min(1.0, pct / 100.0)) * w
        if fill_w >= 2:
            self._rrect(0, 0, fill_w, h, r, fill=bar_color(pct), outline="")
        if expected is not None:
            x = min(max(expected / 100.0 * w, 1), w - 1)
            c.create_line(x, 0, x, h, fill=COL["pace"], width=1)

    def update(self, win: dict):
        self.frame.grid()
        pct = win["pct"]
        expected = expected_pct(win.get("reset_dt"), win.get("window_s"))
        self.name.config(text=win["name"])
        self.draw_bar(pct, expected)
        self.pct.config(text=f"{pct:.0f}%", fg=bar_color(pct))
        rd = win.get("reset_dt")
        if rd is not None:
            self.reset.config(text=human_countdown((rd - now_utc()).total_seconds()))
        else:
            self.reset.config(text="")

    def hide(self):
        self.frame.grid_remove()


class Section:
    """A titled group with a colored dot, a status line, and a pool of rows."""

    def __init__(self, parent, s, fonts, dot_color, title, max_rows=4):
        self.px = lambda n: int(round(n * s))
        self.container = tk.Frame(parent, bg=COL["bg"])
        self.container.pack(fill="x", padx=self.px(12), pady=(self.px(4), 0))

        header = tk.Frame(self.container, bg=COL["bg"])
        header.pack(fill="x")
        dot = tk.Canvas(header, width=self.px(9), height=self.px(9),
                        bg=COL["bg"], highlightthickness=0, bd=0)
        d = self.px(8)
        dot.create_oval(1, 1, d, d, fill=dot_color, outline="")
        dot.pack(side="left", pady=(0, self.px(1)))
        tk.Label(header, text=title, bg=COL["bg"], fg=COL["text"],
                 font=fonts["header"]).pack(side="left", padx=(self.px(6), 0))
        self.status = tk.Label(header, text="", bg=COL["bg"], fg=COL["faint"],
                               font=fonts["status"], anchor="e")
        self.status.pack(side="right")

        body = tk.Frame(self.container, bg=COL["bg"])
        body.pack(fill="x", pady=(self.px(2), 0))
        body.columnconfigure(0, weight=1)
        self.rows = [Row(body, s, fonts, i) for i in range(max_rows)]

        self.message = tk.Label(self.container, text="", bg=COL["bg"],
                                fg=COL["dim"], font=fonts["status"], anchor="w")

    def show_windows(self, windows: list[dict], status: str):
        self.message.pack_forget()
        self.status.config(text=status)
        for i, row in enumerate(self.rows):
            if i < len(windows):
                row.update(windows[i])
            else:
                row.hide()

    def show_message(self, text: str, status: str = "", color: str = None):
        self.status.config(text=status)
        for row in self.rows:
            row.hide()
        self.message.config(text=text, fg=color or COL["dim"])
        self.message.pack(fill="x", pady=(self.px(1), self.px(2)))


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.claude_poll = int(self.cfg.get("claude_poll", DEFAULT_CLAUDE_POLL))
        self.codex_poll = int(self.cfg.get("codex_poll", DEFAULT_CODEX_POLL))

        s = self.winfo_fpixels("1i") / 96.0
        self.s = s
        self.px = lambda n: int(round(n * s))
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass

        self.title(APP_NAME)
        self.overrideredirect(True)
        self.configure(bg=COL["border"])
        self.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        self._topmost = bool(self.attributes("-topmost"))
        self._visible = True

        fam = "Segoe UI"
        mono = "Consolas"
        self.fonts = {
            "title": tkfont.Font(family=fam, size=9, weight="bold"),
            "header": tkfont.Font(family=fam, size=9, weight="bold"),
            "status": tkfont.Font(family=fam, size=8),
            "row": tkfont.Font(family=fam, size=9),
            "pct": tkfont.Font(family=mono, size=9, weight="bold"),
            "reset": tkfont.Font(family=mono, size=8),
            "btn": tkfont.Font(family=fam, size=11, weight="bold"),
        }

        # 1px border via an inner frame inset from the border-colored root.
        outer = tk.Frame(self, bg=COL["bg"])
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self.outer = outer

        self._build_titlebar(outer)
        self.claude_section = Section(outer, s, self.fonts, COL["claude"],
                                      "CLAUDE CODE", max_rows=5)
        self._divider(outer)
        self.codex_section = Section(outer, s, self.fonts, COL["codex"],
                                     "CODEX", max_rows=3)
        tk.Frame(outer, bg=COL["bg"], height=self.px(8)).pack(fill="x")

        # Shared state written by the worker thread, read by the UI tick.
        self.lock = threading.Lock()
        self.claude_state = {"data": None, "err": None}
        self.codex_state = {"data": None, "err": None}
        self.claude_email = None
        self._stop = threading.Event()
        self._force = threading.Event()
        # Adaptive Claude cadence: the current interval (grown/decayed by the
        # 429 policy) and when this token last 429'd, both in monotonic time.
        self._claude_interval = float(self.claude_poll)
        self._last_429_at = None
        self._last_tip = None
        self._last_meter = None

        self._context_menu()
        self._place_window()

        # System tray icon (Windows). Its actions arrive on this queue.
        self.cmd_queue = queue.Queue()
        self.tray = None
        if sys.platform == "win32":
            try:
                self.tray = Tray(self.cmd_queue, lambda: self._topmost,
                                 lambda: self._visible)
            except Exception:
                self.tray = None

        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()
        self.after(150, self._tick)
        self.after(100, self._drain_cmds)
        self.protocol("WM_DELETE_WINDOW", self._quit)

    # -- title bar ---------------------------------------------------------- #
    def _build_titlebar(self, parent):
        bar = tk.Frame(parent, bg=COL["panel"])
        bar.pack(fill="x")
        self.titlebar = bar

        tk.Label(bar, text="Usage Monitor", bg=COL["panel"], fg=COL["text"],
                 font=self.fonts["title"]).pack(side="left", padx=self.px(8),
                                                pady=self.px(4))

        close = tk.Label(bar, text="×", bg=COL["panel"], fg=COL["dim"],
                         font=self.fonts["btn"], cursor="hand2")
        close.pack(side="right", padx=(0, self.px(6)))
        close.bind("<Button-1>", lambda e: self._quit())
        close.bind("<Enter>", lambda e: close.config(fg=COL["btn_hover"]))
        close.bind("<Leave>", lambda e: close.config(fg=COL["dim"]))

        hide = tk.Label(bar, text="−", bg=COL["panel"], fg=COL["dim"],
                        font=self.fonts["btn"], cursor="hand2")
        hide.pack(side="right", padx=self.px(4))
        hide.bind("<Button-1>", lambda e: self._hide_to_tray())
        hide.bind("<Enter>", lambda e: hide.config(fg=COL["text"]))
        hide.bind("<Leave>", lambda e: hide.config(fg=COL["dim"]))

        self.pin = tk.Label(bar, text="", bg=COL["panel"],
                            font=self.fonts["btn"], cursor="hand2")
        self.pin.pack(side="right", padx=self.px(4))
        self.pin.bind("<Button-1>", lambda e: self._toggle_topmost())
        self._refresh_pin()

        refresh = tk.Label(bar, text="↻", bg=COL["panel"], fg=COL["dim"],
                           font=self.fonts["btn"], cursor="hand2")
        refresh.pack(side="right", padx=self.px(4))
        refresh.bind("<Button-1>", lambda e: self.refresh_now())
        refresh.bind("<Enter>", lambda e: refresh.config(fg=COL["text"]))
        refresh.bind("<Leave>", lambda e: refresh.config(fg=COL["dim"]))

        for w in (bar,) + tuple(bar.winfo_children()):
            if w in (close, self.pin, refresh, hide):
                continue
            w.bind("<Button-1>", self._start_move)
            w.bind("<B1-Motion>", self._do_move)
            w.bind("<ButtonRelease-1>", lambda e: self._save_geometry())

    def _divider(self, parent):
        tk.Frame(parent, bg=COL["border"], height=1).pack(
            fill="x", padx=self.px(12), pady=(self.px(6), self.px(2)))

    def _refresh_pin(self):
        on = bool(self.attributes("-topmost"))
        self.pin.config(text="◉" if on else "○",
                        fg=COL["codex"] if on else COL["dim"])

    def _toggle_topmost(self):
        new = not bool(self.attributes("-topmost"))
        self.attributes("-topmost", new)
        self._topmost = new
        self.cfg["topmost"] = new
        save_config(self.cfg)
        self._refresh_pin()

    # -- tray / visibility -------------------------------------------------- #
    def _hide_to_tray(self):
        if self.tray is None:
            return  # never hide with no way to bring it back
        self.withdraw()
        self._visible = False

    def _show_widget(self):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", self._topmost)
        self._visible = True

    def _toggle_visible(self):
        if self._visible:
            self._hide_to_tray()
        else:
            self._show_widget()

    def _drain_cmds(self):
        try:
            while True:
                cmd = self.cmd_queue.get_nowait()
                if cmd == "toggle":
                    self._toggle_visible()
                elif cmd == "refresh":
                    self.refresh_now()
                elif cmd == "top":
                    self._toggle_topmost()
                elif cmd == "quit":
                    self._quit()
                    return
        except queue.Empty:
            pass
        self.after(100, self._drain_cmds)

    # -- movement / placement ---------------------------------------------- #
    def _start_move(self, e):
        self._mx, self._my = e.x_root, e.y_root
        self._ox, self._oy = self.winfo_x(), self.winfo_y()

    def _do_move(self, e):
        self.geometry(f"+{self._ox + e.x_root - self._mx}+{self._oy + e.y_root - self._my}")

    def _save_geometry(self):
        self.cfg["x"] = self.winfo_x()
        self.cfg["y"] = self.winfo_y()
        save_config(self.cfg)

    def _place_window(self):
        self.update_idletasks()
        x = self.cfg.get("x")
        y = self.cfg.get("y")
        if x is None or y is None:
            sw = self.winfo_screenwidth()
            x = sw - self.winfo_reqwidth() - self.px(24)
            y = self.px(24)
        # Keep it on-screen if the saved spot is now off the desktop.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, min(int(x), sw - self.px(60)))
        y = max(0, min(int(y), sh - self.px(60)))
        self.geometry(f"+{x}+{y}")

    # -- context menu ------------------------------------------------------- #
    def _context_menu(self):
        m = tk.Menu(self, tearoff=0, bg=COL["panel"], fg=COL["text"],
                    activebackground=COL["border"], activeforeground=COL["text"],
                    bd=0)
        m.add_command(label="Refresh now", command=self.refresh_now)
        m.add_command(label="Toggle always on top", command=self._toggle_topmost)
        m.add_separator()
        m.add_command(label="Quit", command=self._quit)
        self.menu = m
        self.bind("<Button-3>", self._popup_menu)

    def _popup_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    # -- worker ------------------------------------------------------------- #
    def refresh_now(self):
        self._force.set()

    def _worker_loop(self):
        next_claude = 0.0
        next_codex = 0.0
        while not self._stop.is_set():
            forced = self._force.is_set()
            if forced:
                self._force.clear()
                next_claude = next_codex = 0.0
            t = time.monotonic()

            if t >= next_claude:
                had_429 = False
                retry_after = None
                try:
                    data = fetch_claude_usage()
                    if self.claude_email is None:
                        self.claude_email = read_claude_email()
                    with self.lock:
                        self.claude_state = {"data": data, "err": None}
                except UsageError as e:
                    with self.lock:
                        self.claude_state["err"] = e
                    if e.kind == "ratelimit":
                        had_429 = True
                        retry_after = getattr(e, "retry_after", None)
                except Exception as e:
                    with self.lock:
                        self.claude_state["err"] = UsageError("error", str(e))
                if had_429:
                    self._last_429_at = t
                recent_429 = (
                    self._last_429_at is not None
                    and (t - self._last_429_at) < RECENT_429_WINDOW
                )
                self._claude_interval = plan_claude_interval(
                    base_poll=self.claude_poll,
                    prev_interval=self._claude_interval,
                    had_429=had_429,
                    retry_after=retry_after,
                    recent_429=recent_429,
                )
                next_claude = t + self._claude_interval

            if t >= next_codex:
                try:
                    data = fetch_codex_usage()
                    with self.lock:
                        self.codex_state = {"data": data, "err": None}
                except UsageError as e:
                    with self.lock:
                        self.codex_state["err"] = e
                    next_codex = t + self.codex_poll
                except Exception as e:
                    with self.lock:
                        self.codex_state["err"] = UsageError("error", str(e))
                    next_codex = t + self.codex_poll
                else:
                    next_codex = t + self.codex_poll

            self._stop.wait(0.4)

    # -- render ------------------------------------------------------------- #
    def _render_section(self, section: Section, state: dict, source: str):
        data = state.get("data")
        err = state.get("err")

        if data and data.get("windows"):
            if source == "claude":
                tag = self.claude_email or (data.get("sub") or "")
            else:
                tag = f"{data.get('plan') or ''} plan".strip()
            status = f"{tag}   {human_ago(data.get('at'))}".strip()
            # If the latest poll errored, note it but keep showing last-good data.
            if err and err.kind in ("ratelimit", "network", "stale", "http"):
                status = f"{err.message}   {human_ago(data.get('at'))}"
            section.show_windows(data["windows"], status)
        elif err:
            hint = {
                "noauth": COL["faint"],
                "nodata": COL["faint"],
                "stale": COL["amber"],
                "ratelimit": COL["amber"],
                "network": COL["amber"],
            }.get(err.kind, COL["red"])
            section.show_message(err.message, "", hint)
        else:
            section.show_message("Loading...", "", COL["faint"])

    def _tick(self):
        with self.lock:
            claude = dict(self.claude_state)
            codex = dict(self.codex_state)
        self._render_section(self.claude_section, claude, "claude")
        self._render_section(self.codex_section, codex, "codex")
        if self.tray is not None:
            tip = self._tooltip_text(claude, codex)
            if tip != self._last_tip:
                self._last_tip = tip
                self.tray.set_tooltip(tip)
            rows = self._meter_rows(claude, codex)
            if rows != self._last_meter:
                self._last_meter = rows
                self.tray.set_meter(rows)
        self.after(TICK_MS, self._tick)

    def _meter_rows(self, claude, codex):
        """Tray icon rows: Claude Weekly % on top, Codex Weekly % below (each
        falling back to its worst window when no weekly one is reported)."""
        def one(state):
            win = pick_meter_window((state.get("data") or {}).get("windows") or [])
            if win is not None:
                pct = win["pct"]
                text = "100" if pct >= 99.5 else str(int(round(pct)))
                return (text, hex_to_rgb(bar_color(pct)))
            return ("--", hex_to_rgb(COL["faint"]))
        return [one(claude), one(codex)]

    def _tooltip_text(self, claude, codex) -> str:
        def line(prefix, state):
            data = state.get("data")
            if data and data.get("windows"):
                parts = [f"{w['name']} {w['pct']:.0f}%" for w in data["windows"]]
                return f"{prefix}: " + "  ".join(parts)
            err = state.get("err")
            return f"{prefix}: {err.message}" if err else f"{prefix}: ..."
        return line("Claude", claude) + "\n" + line("Codex", codex)

    def _quit(self):
        self._save_geometry()
        self._stop.set()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    if sys.platform == "win32":
        enable_dpi_awareness()
    app = App()
    app.minsize(app.px(300), 0)
    app.mainloop()


if __name__ == "__main__":
    main()

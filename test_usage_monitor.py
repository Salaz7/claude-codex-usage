"""Unit tests for usage_monitor. Hermetic: no network, no real credentials.

Run:  uv run --python 3.12 python -m unittest -v
"""

import json
import os
import struct
import sys
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from email.message import Message
from pathlib import Path
from unittest import mock

import usage_monitor as um

# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

class TestParseReset(unittest.TestCase):
    def test_iso_with_offset(self):
        dt = um.parse_reset("2026-09-20T11:50:00.751472+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertIsNotNone(dt.tzinfo)

    def test_iso_with_z(self):
        dt = um.parse_reset("2026-09-20T07:51:34.733Z")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)

    def test_unix_int_and_float(self):
        self.assertEqual(um.parse_reset(1790483892).year, 2026)
        self.assertIsNotNone(um.parse_reset(1790483892.5))

    def test_naive_iso_gets_utc(self):
        dt = um.parse_reset("2026-09-20T11:50:00")
        self.assertIsNotNone(dt.tzinfo)

    def test_bad_values(self):
        for v in (None, "", "not-a-date", {}, []):
            self.assertIsNone(um.parse_reset(v))


class TestCountdown(unittest.TestCase):
    def test_values(self):
        self.assertEqual(um.human_countdown(0), "now")
        self.assertEqual(um.human_countdown(-5), "now")
        self.assertEqual(um.human_countdown(45), "45s")
        self.assertEqual(um.human_countdown(70), "1m")
        self.assertEqual(um.human_countdown(4321), "1h 12m")
        self.assertEqual(um.human_countdown(90061), "1d 1h")


class TestAgo(unittest.TestCase):
    def test_none(self):
        self.assertEqual(um.human_ago(None), "never")

    def test_scales(self):
        now = um.now_utc()
        self.assertTrue(um.human_ago(now).endswith("s ago"))
        self.assertEqual(um.human_ago(now - timedelta(minutes=5)), "5m ago")
        self.assertEqual(um.human_ago(now - timedelta(hours=3)), "3h ago")
        self.assertEqual(um.human_ago(now - timedelta(days=2)), "2d ago")


class TestExpectedPct(unittest.TestCase):
    def test_none_inputs(self):
        self.assertIsNone(um.expected_pct(None, um.SEVEN_DAY_S))
        self.assertIsNone(um.expected_pct(um.now_utc(), None))

    def test_halfway(self):
        # Reset is half a 5-hour window away -> expected ~50%.
        reset = um.now_utc() + timedelta(seconds=um.FIVE_HOUR_S / 2)
        val = um.expected_pct(reset, um.FIVE_HOUR_S)
        self.assertAlmostEqual(val, 50.0, delta=1.0)

    def test_clamped(self):
        past = um.now_utc() - timedelta(hours=1)
        self.assertEqual(um.expected_pct(past, um.FIVE_HOUR_S), 100.0)
        future = um.now_utc() + timedelta(days=30)
        self.assertEqual(um.expected_pct(future, um.SEVEN_DAY_S), 0.0)


class TestClaudeInterval(unittest.TestCase):
    """The adaptive 429-recovery cadence (pure policy)."""

    def _plan(self, **kw):
        kw.setdefault("base_poll", 180.0)
        kw.setdefault("prev_interval", 180.0)
        kw.setdefault("had_429", False)
        kw.setdefault("retry_after", None)
        kw.setdefault("recent_429", False)
        kw.setdefault("rng", lambda: 0.5)  # 0.5 -> no jitter
        return um.plan_claude_interval(**kw)

    def test_steady_state_is_base(self):
        self.assertEqual(self._plan(base_poll=180.0), 180.0)
        self.assertEqual(self._plan(base_poll=240.0), 240.0)

    def test_jitter_bounds(self):
        self.assertAlmostEqual(self._plan(rng=lambda: 0.0), 180.0 * 0.9)
        self.assertAlmostEqual(self._plan(rng=lambda: 1.0), 180.0 * 1.1)

    def test_first_429_floors_at_min(self):
        # No Retry-After: 180*1.5=270 would be too low, so it floors at the min.
        self.assertEqual(
            self._plan(had_429=True, prev_interval=180.0), um.POST_429_MIN_POLL
        )

    def test_429_aimd_growth(self):
        self.assertEqual(
            self._plan(had_429=True, prev_interval=360.0), 360.0 * um.POST_429_BACKOFF_MULT
        )
        # Growth is capped at the ceiling.
        self.assertEqual(
            self._plan(had_429=True, prev_interval=1500.0), um.POST_429_MAX_POLL
        )

    def test_429_retry_after_honored_with_margin(self):
        self.assertEqual(
            self._plan(had_429=True, retry_after=42), 42 + um.RETRY_AFTER_MARGIN
        )

    def test_429_retry_after_capped(self):
        self.assertEqual(self._plan(had_429=True, retry_after=9000), um.RETRY_AFTER_MAX)

    def test_recent_429_holds_floor_on_success(self):
        # A success while a 429 is still recent stays at the floor, not base.
        self.assertEqual(self._plan(recent_429=True, base_poll=180.0), um.POST_429_MIN_POLL)

    def test_recovers_to_base_after_window(self):
        self.assertEqual(self._plan(recent_429=False, base_poll=180.0), 180.0)


class TestBarColor(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(um.bar_color(10), um.COL["green"])
        self.assertEqual(um.bar_color(69.9), um.COL["green"])
        self.assertEqual(um.bar_color(70), um.COL["amber"])
        self.assertEqual(um.bar_color(89.9), um.COL["amber"])
        self.assertEqual(um.bar_color(90), um.COL["red"])
        self.assertEqual(um.bar_color(100), um.COL["red"])


class TestMeterWindow(unittest.TestCase):
    def test_prefers_weekly_over_higher_windows(self):
        wins = [
            {"name": "5-hour", "pct": 90.0},
            {"name": "Weekly", "pct": 30.0},
            {"name": "Fable", "pct": 100.0},
        ]
        chosen = um.pick_meter_window(wins)
        self.assertEqual(chosen["name"], "Weekly")
        self.assertEqual(chosen["pct"], 30.0)

    def test_falls_back_to_worst_when_no_weekly(self):
        wins = [{"name": "5-hour", "pct": 40.0}, {"name": "Daily", "pct": 75.0}]
        self.assertEqual(um.pick_meter_window(wins)["pct"], 75.0)

    def test_empty(self):
        self.assertIsNone(um.pick_meter_window([]))


class TestCodexLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(um.codex_window_label(300), "5-hour")
        self.assertEqual(um.codex_window_label(10080), "Weekly")
        self.assertEqual(um.codex_window_label(1440), "Daily")
        self.assertEqual(um.codex_window_label(60), "60m")
        self.assertEqual(um.codex_window_label(4320), "3d")
        self.assertEqual(um.codex_window_label(120), "2h")
        self.assertEqual(um.codex_window_label(None), "Limit")


# --------------------------------------------------------------------------- #
# Claude response normalization (pure)
# --------------------------------------------------------------------------- #

class TestBuildClaudeWindows(unittest.TestCase):
    def test_full_response(self):
        raw = {
            "five_hour": {"utilization": 2, "resets_at": "2026-09-20T11:50:00+00:00"},
            "seven_day": {"utilization": 85, "resets_at": "2026-09-20T09:00:00+00:00"},
            "limits": [
                {"scope": {"model": {"display_name": "Fable"}}, "percent": 100,
                 "resets_at": "2026-09-20T09:00:00+00:00"},
            ],
        }
        w = um.build_claude_windows(raw)
        names = [x["name"] for x in w]
        self.assertEqual(names, ["5-hour", "Weekly", "Fable"])
        self.assertEqual(w[0]["pct"], 2.0)
        self.assertEqual(w[1]["window_s"], um.SEVEN_DAY_S)
        self.assertIsNotNone(w[2]["reset_dt"])

    def test_missing_pieces(self):
        self.assertEqual(um.build_claude_windows({}), [])
        w = um.build_claude_windows({"seven_day": {"utilization": 50}})
        self.assertEqual(len(w), 1)
        self.assertIsNone(w[0]["reset_dt"])

    def test_ignores_bad_limits(self):
        raw = {"limits": [
            "junk",
            {"scope": {}, "percent": 10},                       # no model
            {"scope": {"model": {"display_name": "X"}}},         # no percent
            {"scope": {"model": {"display_name": "Y"}}, "percent": 33},
        ]}
        w = um.build_claude_windows(raw)
        self.assertEqual([x["name"] for x in w], ["Y"])
        self.assertEqual(w[0]["pct"], 33.0)


# --------------------------------------------------------------------------- #
# Credential reading (temp CLAUDE_CONFIG_DIR, no real secrets)
# --------------------------------------------------------------------------- #

class TestReadClaudeOauth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._prev
        self.tmp.cleanup()

    def _write(self, text):
        Path(self.tmp.name, ".credentials.json").write_text(text, encoding="utf-8")

    def test_missing_file(self):
        with self.assertRaises(um.UsageError) as cm:
            um.read_claude_oauth()
        self.assertEqual(cm.exception.kind, "noauth")

    def test_bad_json(self):
        self._write("{not json")
        with self.assertRaises(um.UsageError) as cm:
            um.read_claude_oauth()
        self.assertEqual(cm.exception.kind, "noauth")

    def test_no_token(self):
        self._write(json.dumps({"claudeAiOauth": {"refreshToken": "x"}}))
        with self.assertRaises(um.UsageError):
            um.read_claude_oauth()

    def test_valid(self):
        self._write(json.dumps({"claudeAiOauth": {
            "accessToken": "fake-token", "subscriptionType": "max"}}))
        oauth = um.read_claude_oauth()
        self.assertEqual(oauth["accessToken"], "fake-token")
        self.assertEqual(oauth["subscriptionType"], "max")


# --------------------------------------------------------------------------- #
# Claude fetch error mapping (urlopen + read_claude_oauth mocked)
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


class TestFetchClaudeErrors(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(
            um, "read_claude_oauth",
            return_value={"accessToken": "t", "subscriptionType": "max"})
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_success(self):
        payload = {"five_hour": {"utilization": 5}, "seven_day": {"utilization": 40}}
        with mock.patch.object(um.urllib.request, "urlopen",
                               return_value=_FakeResp(payload)):
            out = um.fetch_claude_usage()
        self.assertEqual(out["sub"], "max")
        self.assertEqual(len(out["windows"]), 2)

    def test_401_is_stale(self):
        err = urllib.error.HTTPError("u", 401, "no", Message(), None)
        with mock.patch.object(um.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(um.UsageError) as cm:
                um.fetch_claude_usage()
        self.assertEqual(cm.exception.kind, "stale")

    def test_429_backoff(self):
        hdrs = Message()
        hdrs["Retry-After"] = "42"
        err = urllib.error.HTTPError("u", 429, "slow", hdrs, None)
        with mock.patch.object(um.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(um.UsageError) as cm:
                um.fetch_claude_usage()
        self.assertEqual(cm.exception.kind, "ratelimit")
        self.assertEqual(getattr(cm.exception, "retry_after", None), 42)

    def test_network(self):
        err = urllib.error.URLError("offline")
        with mock.patch.object(um.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(um.UsageError) as cm:
                um.fetch_claude_usage()
        self.assertEqual(cm.exception.kind, "network")


# --------------------------------------------------------------------------- #
# Codex parsing (temp CODEX_HOME with synthetic rollout files)
# --------------------------------------------------------------------------- #

def _rl_line(ts, primary=None, secondary=None, plan="pro"):
    rl = {"limit_id": "codex", "primary": primary, "secondary": secondary,
          "plan_type": plan}
    return json.dumps({"timestamp": ts, "type": "event_msg",
                       "payload": {"type": "token_count", "rate_limits": rl}})


class TestCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = self.tmp.name
        self.daydir = Path(self.tmp.name, "sessions", "2026", "09", "20")
        self.daydir.mkdir(parents=True)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._prev
        self.tmp.cleanup()

    def _file(self, name, lines, mtime=None):
        p = self.daydir / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_no_sessions_dir(self):
        # Point somewhere with no sessions/ at all.
        with mock.patch.dict(os.environ, {"CODEX_HOME": tempfile.gettempdir() + "/nope_xyz"}):
            with self.assertRaises(um.UsageError) as cm:
                um.fetch_codex_usage()
            self.assertEqual(cm.exception.kind, "noauth")

    def test_no_rate_limits(self):
        self._file("rollout-a.jsonl", [json.dumps({"timestamp": "2026-09-20T10:00:00Z",
                                                    "payload": {"type": "x"}})])
        with self.assertRaises(um.UsageError) as cm:
            um.fetch_codex_usage()
        self.assertEqual(cm.exception.kind, "nodata")

    def test_weekly_only(self):
        line = _rl_line("2026-09-20T10:00:00Z",
                        primary={"used_percent": 21.0, "window_minutes": 10080,
                                 "resets_at": 1790483892},
                        secondary=None)
        self._file("rollout-a.jsonl", [line])
        out = um.fetch_codex_usage()
        self.assertEqual(out["plan"], "pro")
        self.assertEqual(len(out["windows"]), 1)
        self.assertEqual(out["windows"][0]["name"], "Weekly")
        self.assertEqual(out["windows"][0]["pct"], 21.0)

    def test_primary_and_secondary_sorted(self):
        line = _rl_line("2026-09-20T10:00:00Z",
                        primary={"used_percent": 80.0, "window_minutes": 10080,
                                 "resets_at": 1790483892},
                        secondary={"used_percent": 30.0, "window_minutes": 300,
                                   "resets_at": 1790000000})
        self._file("rollout-a.jsonl", [line])
        out = um.fetch_codex_usage()
        names = [w["name"] for w in out["windows"]]
        self.assertEqual(names, ["5-hour", "Weekly"])  # 300 min sorts before 10080

    def test_newest_timestamp_wins_across_files(self):
        older_mtime, newer_mtime = 1_000_000, 2_000_000
        # File with NEWER mtime but OLDER internal timestamp / lower percent.
        self._file("rollout-newmtime.jsonl", [
            _rl_line("2026-09-20T08:00:00Z",
                     primary={"used_percent": 10.0, "window_minutes": 10080,
                              "resets_at": 1790483892})],
            mtime=newer_mtime)
        # File with OLDER mtime but NEWER internal timestamp / higher percent.
        self._file("rollout-oldmtime.jsonl", [
            _rl_line("2026-09-20T12:00:00Z",
                     primary={"used_percent": 55.0, "window_minutes": 10080,
                              "resets_at": 1790483892})],
            mtime=older_mtime)
        out = um.fetch_codex_usage()
        self.assertEqual(out["windows"][0]["pct"], 55.0)

    def test_skips_null_used_percent(self):
        line = _rl_line("2026-09-20T10:00:00Z",
                        primary={"used_percent": None, "window_minutes": 300,
                                 "resets_at": 1790000000},
                        secondary={"used_percent": 12.0, "window_minutes": 10080,
                                   "resets_at": 1790483892})
        self._file("rollout-a.jsonl", [line])
        out = um.fetch_codex_usage()
        self.assertEqual(len(out["windows"]), 1)
        self.assertEqual(out["windows"][0]["name"], "Weekly")


# --------------------------------------------------------------------------- #
# Tray icon generator
# --------------------------------------------------------------------------- #

class TestIco(unittest.TestCase):
    def test_valid_ico_header(self):
        data = um.make_tray_ico(32)
        reserved, typ, count = struct.unpack("<HHH", data[:6])
        self.assertEqual(reserved, 0)
        self.assertEqual(typ, 1)      # 1 = icon
        self.assertEqual(count, 1)
        # 22-byte header (6 dir + 16 entry) + 40 BIH + 32*32*4 XOR + 32*4 AND
        self.assertEqual(len(data), 22 + 40 + 32 * 32 * 4 + 32 * 4)

    def test_meter_dib_size(self):
        dib = um.meter_dib(32, [("85", (210, 153, 34)), ("24", (63, 185, 80))])
        # DIB only (no ICONDIR): 40 BIH + XOR + AND mask
        self.assertEqual(len(dib), 40 + 32 * 32 * 4 + 32 * 4)

    def test_meter_differs_from_bars(self):
        bars = um.bars_dib(32)
        meter = um.meter_dib(32, [("99", (248, 81, 73)), ("--", (110, 118, 129))])
        self.assertNotEqual(bars, meter)

    @unittest.skipUnless(sys.platform == "win32", "Win32 icon API")
    def test_hicon_creation(self):
        import ctypes
        dib = um.meter_dib(32, [("85", (210, 153, 34)), ("24", (63, 185, 80))])
        h = um._hicon_from_dib(dib)
        self.assertTrue(h, "CreateIconFromResourceEx returned NULL")
        ctypes.windll.user32.DestroyIcon(h)


if __name__ == "__main__":
    unittest.main(verbosity=2)

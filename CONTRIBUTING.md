# Contributing

Thanks for your interest in improving Usage Monitor. This is a small,
single-file app, so contributing is meant to be quick.

## Ground rules

- **Runtime stays standard-library only.** The app must run from the bundled
  Python runtime with no third-party packages, so nothing you add to
  `usage_monitor.py` may import an external dependency. Build-time and dev-time
  tools (PyInstaller, ruff) are fine because they never ship inside the app.
- **Windows only, for now.** The tray icon and DPI handling are Windows
  specific. Cross-platform work is welcome but should be discussed in an issue
  first so the single-file shape stays manageable.
- **Keep it a monitor.** It must never write a credential file, switch accounts,
  or make any network call other than the single Anthropic usage request. Any
  change to that boundary will be declined.
- **No em dashes in any file** (code, comments, docs). Use commas, parentheses,
  colons, semicolons, or a single hyphen instead.

## Development setup

You only need [uv](https://docs.astral.sh/uv/). It provides Python 3.12 and
Tkinter automatically.

```powershell
# Run straight from source (no build step)
uv run --python 3.12 python usage_monitor.py
# or
.\run-from-source.bat
```

## Before you open a pull request

Run the linter and the tests. Both must pass; CI runs the same checks.

```powershell
# Lint (auto-fix with --fix)
uv run --python 3.12 --with ruff ruff check .

# Tests (hermetic: no network, no real credentials, temp dirs only)
uv run --python 3.12 python -m unittest -v
```

If you change parsing, credential reading, error handling, the Codex log
scanner, or the tray icon generator, add or update a test in
`test_usage_monitor.py`.

## Building the executable

```powershell
.\build.ps1
```

The result is `dist\UsageMonitor.exe`, a single self-contained file. It is not
committed to git; official binaries are attached to GitHub Releases.

## Pull request checklist

- [ ] `ruff check .` is clean
- [ ] `python -m unittest` passes
- [ ] New behavior has a test
- [ ] No new runtime dependency
- [ ] No em dashes
- [ ] `CHANGELOG.md` updated under "Unreleased" if the change is user visible

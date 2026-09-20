# Rebuilds dist\UsageMonitor.exe from usage_monitor.py.
# Requires uv (already installed). No other setup needed: the app is stdlib-only,
# and PyInstaller is pulled in just for the build.

uv run --python 3.12 --with pyinstaller pyinstaller `
  --noconfirm --onefile --windowed `
  --name UsageMonitor `
  --distpath dist --workpath build --specpath build `
  usage_monitor.py

Write-Host ""
Write-Host "Done. The app is at: dist\UsageMonitor.exe" -ForegroundColor Green

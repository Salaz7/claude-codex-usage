@echo off
REM Runs the monitor straight from source (for quick edits, no build step).
REM A console window stays open so you can see any errors; close it to quit.
uv run --python 3.12 python "%~dp0usage_monitor.py"

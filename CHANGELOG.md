# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.2] - 2026-09-20

### Added

- Start minimized to the tray via a `start_hidden` config key or a `--minimized`
  command-line flag (also `--tray` / `--hidden`), handy for a Windows-startup
  entry.

## [1.0.1] - 2026-09-20

### Changed

- Raised the default Claude usage poll interval from 60s to 180s. The endpoint
  budgets non-first-party clients to roughly 28-30 requests per rolling hour, so
  60s (60/hour) was over the cap while 180s (20/hour) stays under it. Existing
  `config.json` files are unaffected; set `claude_poll` to override.
- Enlarged the system-tray icon (rendered at 64px with tighter padding) so the
  stacked Claude and Codex numbers stay legible at 150-200% display scaling.
- The tray icon now shows each service's Weekly percentage (falling back to the
  worst window when no weekly one is reported) instead of always the worst.

### Added

- Adaptive 429 recovery for the usage endpoint: after a rate-limit the poll
  interval floors at 6 minutes and grows while 429s persist, a positive
  `Retry-After` is honored with a margin (retrying on the server's deadline
  tends to re-block), and every interval is jittered so independent pollers do
  not fetch in lockstep.

## [1.0.0] - 2026-09-20

First public release.

### Added

- Always-on-top Windows widget showing Claude Code and Codex usage at a glance.
- Claude Code section: 5-hour window, weekly window, and per-model Fable limit,
  each with a percent, a live reset countdown, and a pace marker.
- Codex section: whichever windows Codex reports (5-hour and/or weekly), with
  percent, reset countdown, and plan type.
- System tray icon that renders your worst Claude and Codex percentages as a
  live number, colored green/amber/red by severity, with a full-breakdown
  tooltip and a right-click menu.
- Hide-to-tray, always-on-top toggle, manual refresh, and remembered window
  position.
- Reads Claude usage from the existing `~/.claude/.credentials.json` OAuth token
  and Codex usage from local `~/.codex/sessions` logs. Honors
  `CLAUDE_CONFIG_DIR` and `CODEX_HOME`.
- Automatic rate-limit backoff for the Anthropic usage endpoint.
- Hermetic test suite (no network, no real credentials).

[Unreleased]: https://github.com/Salaz7/claude-codex-usage/compare/v1.0.2...HEAD
[1.0.2]: https://github.com/Salaz7/claude-codex-usage/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/Salaz7/claude-codex-usage/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/Salaz7/claude-codex-usage/releases/tag/v1.0.0

# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Salaz7/claude-codex-usage/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Salaz7/claude-codex-usage/releases/tag/v1.0.0

# Security Policy

## How this app treats your credentials

Usage Monitor is a read-only monitor. It is designed so that it cannot leak,
change, or store your credentials:

- **Claude Code:** it reads the OAuth access token that Claude Code already
  stores at `%USERPROFILE%\.claude\.credentials.json` and sends it in exactly
  one request, to `https://api.anthropic.com/api/oauth/usage`, which is the same
  endpoint Claude Code itself calls. The token is never written anywhere and
  never sent to any other host.
- **Codex:** it reads the most recent `rate_limits` snapshot that Codex writes
  into its own local session logs under `%USERPROFILE%\.codex\sessions\`. Nothing
  is transmitted.
- No credential is ever logged, copied, cached, or uploaded. The only network
  call the app makes is the single Anthropic usage request above.

If you build the binary yourself with `.\build.ps1`, or run from source, you can
confirm all of this against the single-file source in `usage_monitor.py`.

## Supported versions

Only the latest release receives fixes. Please reproduce any issue on the most
recent version before reporting.

| Version | Supported |
| ------- | --------- |
| latest  | yes       |
| older   | no        |

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Use GitHub's private reporting instead:

1. Go to the **Security** tab of this repository.
2. Click **Report a vulnerability** (GitHub Private Vulnerability Reporting).
3. Describe the issue, the impact, and steps to reproduce.

You can expect an initial response within a few days. Once a fix is available it
will ship in a new release and the advisory will be published with credit to the
reporter, unless you ask to stay anonymous.

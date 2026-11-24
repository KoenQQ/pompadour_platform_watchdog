# Coolify Watchdog

On-command broad system check for a Coolify instance. One command answers
"is everything still fine?" from anywhere with internet:

```
$ python3 scripts/coolify_watchdog/check.py

Coolify system check — https://apps.example.com — 2026-11-24T07:42:35+00:00

✓ Instance reachable (Coolify v4.0.0-beta.420)
✓ Server hetzner-1 (reachable=yes, usable=yes)

Applications (2)
  ✓ backend  running:healthy
  ✗ worker   exited:unhealthy

Log errors (last 26h)
  [NEW ] 0a4abc77dc  backend  traceback  3x  last 2026-11-24T06:12:44+00:00
         ValueError: boom

Summary: 2 problem(s)
  - application 'worker' is exited:unhealthy
  - 1 new error fingerprint(s) in application logs
```

What one run checks, via the Coolify API only (no SSH, nothing installed on
the servers):

1. **Instance** — reachable, token valid, Coolify version.
2. **Servers** — `is_reachable` / `is_usable` flags.
3. **Applications, services, databases** — live status of everything on the
   instance (auto-discovered; no per-app configuration).
4. **Deployments** — anything currently stuck deploying.
5. **Application logs** — error events (Python tracebacks, `ERROR`/`CRITICAL`
   lines, HTTP 5xx) extracted and fingerprinted, with **NEW** vs *seen*
   marking against your previous run — so you immediately see what broke
   since you last looked.

Exit codes: `0` all healthy · `1` problems found · `2` check could not run.

## Setup (laptop)

Needs only Python 3.10+ — the script is stdlib-only, nothing to install.

```bash
git clone https://github.com/KoenQQ/coolify-watchdog.git
mkdir -p ~/.coolify-watchdog
cat > ~/.coolify-watchdog/env <<'EOF'
COOLIFY_API_URL=https://apps.example.com
COOLIFY_API_TOKEN=<Coolify UI → Keys & Tokens → API tokens>
EOF
chmod 600 ~/.coolify-watchdog/env

python3 coolify-watchdog/scripts/coolify_watchdog/check.py
```

Useful flags:

| Flag | Effect |
|---|---|
| `--since-hours 72` | log error window (default: since your last check, else 26h) |
| `--log-lines 10000` | log lines scanned per application (default 3000) |
| `--apps backend,worker` | only scan logs of matching applications |
| `--no-logs` | status-only check (fast, tiny bandwidth) |
| `--json` | full machine-readable report (includes full log excerpts) |
| `--all` | also show fingerprints silenced via `ignore.txt` |

State lives in `~/.coolify-watchdog/state.json`: seen fingerprints (for the
NEW marker) and the time of your last run (the default log window). To
silence a recurring, accepted error permanently, add its fingerprint to
`scripts/coolify_watchdog/ignore.txt`.

## Run it from your phone (GitHub Actions)

The same check runs as an on-demand GitHub Actions workflow in this repo —
handy when you only have your phone:

1. Configure this repo (Settings → Secrets and variables → Actions):
   - Secret `COOLIFY_API_TOKEN`
   - Variable `COOLIFY_API_URL`
2. GitHub app/web → Actions → **Coolify System Check** → Run workflow.

Green run = everything healthy. Red run = problems found — open the job
summary for the list and the log excerpts. (The runner is stateless, so every
error in the window is marked NEW there.) Uncomment the `schedule:` block in
`.github/workflows/coolify-watchdog.yml` to also run it automatically.

## Push alerts when something breaks

This tool is pull-based by design. For push alerts, use Coolify's built-in
notifications (Coolify UI → **Notifications**): connect Telegram, Discord,
Slack, Email or Pushover and Coolify messages you on failed deployments,
containers that stop or turn unhealthy, unreachable servers and failed
backups. The two complement each other: Coolify notifications tell you *that*
something visibly broke; this check is the on-command deep look, including
log errors that never flip a container to "unhealthy".

## Morning report in plain language

The `coolify-morning-report` Claude Code skill (`skills/coolify-morning-report/`)
triggers the system check workflow, reads the result, and explains any
problems conversationally — handy as the prompt of a scheduled daily chat.
Copy it to `~/.claude/skills/` and run `/coolify-morning-report`.

## Development

```bash
pytest scripts/coolify_watchdog/   # parser + check unit tests
```

`fetch_errors.py` holds the log parsing and fingerprinting (an error class
keeps the same 10-char fingerprint across nights, line-number changes and
request ids); `check.py` is the CLI on top. Both are stdlib-only on purpose.
CI runs the suite on every push.

> The previous architecture — a nightly reusable workflow that each project
> repo called, with Claude opening draft fix-PRs — was replaced by this
> on-demand design in Nov 2026 and lives on in git history if ever needed.

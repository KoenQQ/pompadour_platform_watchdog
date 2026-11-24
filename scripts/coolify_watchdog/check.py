#!/usr/bin/env python3
"""On-demand broad system check for a Coolify instance.

One run checks:
  - instance reachability + version (also validates the API token)
  - every server: reachable / usable flags
  - every application, service and database: live status (auto-discovered)
  - deployments currently in progress
  - application container logs: error events (tracebacks, ERROR/CRITICAL
    lines, HTTP 5xx), fingerprinted and compared against the previous run

Designed to run from anywhere with internet access and a Python 3.10+
interpreter — stdlib only, nothing to install:

    python3 scripts/coolify_watchdog/check.py

Configuration — environment variables, or an env file (KEY=VALUE lines) at
~/.coolify-watchdog/env (override the path with --env-file):

    COOLIFY_API_URL      e.g. https://apps.example.com
    COOLIFY_API_TOKEN    Coolify UI -> Keys & Tokens -> API tokens

State (which error fingerprints you have already seen, when the last check
ran) is kept in ~/.coolify-watchdog/state.json so a holiday laptop can tell
"new since yesterday" from "same old error". ignore.txt next to this script
silences fingerprints permanently.

In GitHub Actions the report is also written to the job summary.

Exit codes: 0 = all healthy, 1 = problems found, 2 = check could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_errors import (  # noqa: E402
    extract_events,
    fetch_app_logs,
    filter_recent,
    group_errors,
    load_ignored,
)

DEFAULT_ENV_FILE = "~/.coolify-watchdog/env"
DEFAULT_STATE_FILE = "~/.coolify-watchdog/state.json"
DEFAULT_SINCE_HOURS = 26.0
MAX_ERRORS_SHOWN = 20


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file ('export ' prefixes, quotes and
    #-comments allowed). Returns {} if the file does not exist."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_config(env_file: str) -> tuple[str, str]:
    file_values = parse_env_file(Path(env_file).expanduser())
    url = os.getenv("COOLIFY_API_URL") or file_values.get("COOLIFY_API_URL", "")
    token = os.getenv("COOLIFY_API_TOKEN") or file_values.get("COOLIFY_API_TOKEN", "")
    return url.strip().rstrip("/"), token.strip()


# --------------------------------------------------------------------------
# Coolify API
# --------------------------------------------------------------------------

def api_get(api_url: str, token: str, path: str, timeout: int = 30):
    """GET an API path; return parsed JSON, or the raw text if not JSON."""
    request = urllib.request.Request(
        f"{api_url}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.strip()


def server_flags(server: dict) -> tuple[bool | None, bool | None]:
    """Extract (is_reachable, is_usable); Coolify has moved these between the
    top level and the nested settings object across versions."""
    settings = server.get("settings") or {}

    def flag(name: str) -> bool | None:
        value = server.get(name, settings.get(name))
        return None if value is None else bool(value)

    return flag("is_reachable"), flag("is_usable")


def classify_status(status: str | None) -> tuple[str, bool]:
    """Map a Coolify status string ('running:healthy', 'exited', ...) to
    (display string, is_problem)."""
    text = (status or "").strip().lower()
    if not text:
        return "unknown", False
    state = text.split(":", 1)[0]
    problem = state != "running" or "unhealthy" in text
    return text, problem


# --------------------------------------------------------------------------
# State (fingerprints seen on previous runs)
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            state.setdefault("fingerprints", {})
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"fingerprints": {}, "last_run": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def mark_new(errors: list[dict], state: dict) -> None:
    """Set record['new'] from the state file's previously seen fingerprints."""
    seen = state.get("fingerprints", {})
    for record in errors:
        record["new"] = record["fingerprint"] not in seen


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------

def run_check(api_url: str, token: str, args, ignored: set[str], state: dict) -> dict:
    now = datetime.now(timezone.utc)
    report: dict = {
        "checked_at": now.isoformat(timespec="seconds"),
        "instance": {"url": api_url, "ok": False, "version": None, "error": None},
        "servers": [],
        "resources": {},
        "deployments_in_progress": None,
        "since_hours": None,
        "errors": [],
        "ignored_errors": 0,
        "log_failures": [],
        "problems": [],
        "exit_code": 0,
    }
    problems = report["problems"]

    # 1. Instance + token.
    try:
        version = api_get(api_url, token, "/api/v1/version")
    except urllib.error.HTTPError as exc:
        hint = " (invalid API token?)" if exc.code in (401, 403) else ""
        report["instance"]["error"] = f"HTTP {exc.code}{hint}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        report["instance"]["error"] = str(exc)
    else:
        report["instance"]["ok"] = True
        report["instance"]["version"] = version if isinstance(version, str) else json.dumps(version)
    if not report["instance"]["ok"]:
        problems.append(f"Coolify instance unreachable: {report['instance']['error']}")
        report["exit_code"] = 2
        return report

    # 2. Servers.
    try:
        servers = api_get(api_url, token, "/api/v1/servers")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        problems.append(f"could not list servers: {exc}")
        servers = []
    for server in servers if isinstance(servers, list) else []:
        reachable, usable = server_flags(server)
        name = server.get("name") or server.get("uuid") or "server"
        report["servers"].append({"name": name, "reachable": reachable, "usable": usable})
        if reachable is False:
            problems.append(f"server '{name}' is not reachable")
        elif usable is False:
            problems.append(f"server '{name}' is not usable")

    # 3. Applications / services / databases.
    applications: list[dict] = []
    for kind, path in (
        ("applications", "/api/v1/applications"),
        ("services", "/api/v1/services"),
        ("databases", "/api/v1/databases"),
    ):
        try:
            items = api_get(api_url, token, path)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            problems.append(f"could not list {kind}: {exc}")
            report["resources"][kind] = []
            continue
        rows = []
        for item in items if isinstance(items, list) else []:
            name = item.get("name") or item.get("uuid") or kind[:-1]
            status, is_problem = classify_status(item.get("status"))
            rows.append({"name": name, "uuid": item.get("uuid", ""), "status": status,
                         "problem": is_problem})
            if is_problem:
                problems.append(f"{kind[:-1]} '{name}' is {status}")
        report["resources"][kind] = rows
        if kind == "applications":
            applications = rows

    # 4. Deployments currently in progress (informational).
    try:
        deployments = api_get(api_url, token, "/api/v1/deployments")
        if isinstance(deployments, dict):
            deployments = deployments.get("deployments", [])
        report["deployments_in_progress"] = len(deployments) if isinstance(deployments, list) else None
    except (urllib.error.URLError, OSError, ValueError):
        report["deployments_in_progress"] = None

    # 5. Application logs -> fingerprinted errors.
    if not args.no_logs:
        since_hours = args.since_hours
        if since_hours is None:
            since_hours = hours_since_last_run(state, now)
        report["since_hours"] = since_hours
        wanted = [f.strip().lower() for f in (args.apps or "").split(",") if f.strip()]
        all_groups: list[dict] = []
        for app in applications:
            if wanted and not any(f in app["name"].lower() for f in wanted):
                continue
            try:
                text = fetch_app_logs(api_url, token, app["uuid"], args.log_lines)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                report["log_failures"].append(app["name"])
                print(f"WARNING: failed to fetch logs for {app['name']}: {exc}", file=sys.stderr)
                continue
            events = filter_recent(extract_events(text), now, since_hours)
            all_groups.extend(group_errors(events, app["name"]))
        if report["log_failures"]:
            problems.append("log fetch failed for: " + ", ".join(report["log_failures"]))

        kept = [g for g in all_groups if args.all or g["fingerprint"] not in ignored]
        report["ignored_errors"] = len(all_groups) - len(kept)
        mark_new(kept, state)
        kept.sort(key=lambda r: (not r["new"], -r["count"]))
        report["errors"] = kept
        new_count = sum(1 for r in kept if r["new"])
        if new_count:
            problems.append(f"{new_count} new error fingerprint(s) in application logs")

    if problems:
        report["exit_code"] = 1
    return report


def hours_since_last_run(state: dict, now: datetime) -> float:
    """Default log window: everything since the last check (clamped to
    1h..7days), or DEFAULT_SINCE_HOURS on the first run."""
    last_run = state.get("last_run")
    if not last_run:
        return DEFAULT_SINCE_HOURS
    try:
        then = datetime.fromisoformat(last_run)
    except ValueError:
        return DEFAULT_SINCE_HOURS
    hours = (now - then).total_seconds() / 3600
    return round(min(max(hours, 1.0), 24.0 * 7), 1)


def update_state(state: dict, report: dict) -> None:
    fingerprints = state.setdefault("fingerprints", {})
    for record in report["errors"]:
        fingerprints[record["fingerprint"]] = record["last_seen"] or report["checked_at"]
    state["last_run"] = report["checked_at"]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _sym(ok: bool | None, color: bool) -> str:
    if ok is None:
        mark, code = "?", "33"
    elif ok:
        mark, code = "✓", "32"
    else:
        mark, code = "✗", "31"
    return f"\033[{code}m{mark}\033[0m" if color else mark


def render_text(report: dict, color: bool) -> str:
    out: list[str] = []
    instance = report["instance"]
    out.append(f"Coolify system check — {instance['url']} — {report['checked_at']}")
    out.append("")
    if not instance["ok"]:
        out.append(f"{_sym(False, color)} Instance unreachable: {instance['error']}")
        return "\n".join(out) + "\n"
    out.append(f"{_sym(True, color)} Instance reachable (Coolify v{instance['version']})")

    for server in report["servers"]:
        ok = server["reachable"] is not False and server["usable"] is not False
        detail = ", ".join(
            f"{label}={_fmt_flag(value)}"
            for label, value in (("reachable", server["reachable"]), ("usable", server["usable"]))
        )
        out.append(f"{_sym(ok if (server['reachable'] is not None or server['usable'] is not None) else None, color)} Server {server['name']} ({detail})")

    for kind in ("applications", "services", "databases"):
        rows = report["resources"].get(kind, [])
        if not rows:
            continue
        out.append("")
        out.append(f"{kind.capitalize()} ({len(rows)})")
        width = max(len(r["name"]) for r in rows)
        for row in rows:
            out.append(f"  {_sym(not row['problem'] if row['status'] != 'unknown' else None, color)} {row['name']:<{width}}  {row['status']}")

    if report["deployments_in_progress"]:
        out.append("")
        out.append(f"Deployments in progress: {report['deployments_in_progress']}")

    if report["since_hours"] is not None:
        out.append("")
        errors = report["errors"]
        header = f"Log errors (last {report['since_hours']:g}h)"
        if not errors:
            out.append(f"{header}: none 🎉")
        else:
            out.append(header)
            for record in errors[:MAX_ERRORS_SHOWN]:
                tag = "NEW " if record["new"] else "seen"
                first_line = (record["excerpt"].splitlines() or [""])[0][:100]
                out.append(
                    f"  [{tag}] {record['fingerprint']}  {record['app']}  {record['kind']}  "
                    f"{record['count']}x  last {record['last_seen'] or 'unknown'}"
                )
                out.append(f"         {first_line}")
            if len(errors) > MAX_ERRORS_SHOWN:
                out.append(f"  … and {len(errors) - MAX_ERRORS_SHOWN} more (use --json for all)")
        if report["ignored_errors"]:
            out.append(f"  ({report['ignored_errors']} ignored via ignore.txt)")

    out.append("")
    if report["problems"]:
        out.append(f"Summary: {len(report['problems'])} problem(s)")
        for problem in report["problems"]:
            out.append(f"  - {problem}")
    else:
        out.append("Summary: all healthy ✓")
    return "\n".join(out) + "\n"


def _fmt_flag(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "NO")


def render_markdown(report: dict) -> str:
    """Compact report for the GitHub Actions job summary."""
    lines = ["## Coolify system check", ""]
    instance = report["instance"]
    if not instance["ok"]:
        lines.append(f"🔴 **Instance unreachable:** {instance['error']}")
        return "\n".join(lines) + "\n"
    icon = "🟢" if not report["problems"] else "🔴"
    lines.append(f"{icon} `{instance['url']}` — Coolify v{instance['version']} — {report['checked_at']}")
    lines.append("")
    if report["problems"]:
        lines.append("**Problems:**")
        lines.extend(f"- {p}" for p in report["problems"])
    else:
        lines.append("All servers, resources and logs healthy. 🎉")
    lines.append("")
    rows = [r for kind in ("applications", "services", "databases")
            for r in report["resources"].get(kind, [])]
    if rows:
        lines.append("| Resource | Status |")
        lines.append("|---|---|")
        lines.extend(f"| {r['name']} | {'🟢' if not r['problem'] else '🔴'} `{r['status']}` |" for r in rows)
        lines.append("")
    for record in report["errors"][:MAX_ERRORS_SHOWN]:
        tag = "**NEW**" if record["new"] else "seen"
        lines.append(f"<details><summary>{tag} <code>{record['fingerprint']}</code> "
                     f"{record['app']} — {record['kind']}, {record['count']}x</summary>")
        lines.append("")
        lines.append("```")
        lines.append(record["excerpt"])
        lines.append("```")
        lines.append("</details>")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="On-demand broad system check for a Coolify instance.")
    parser.add_argument("--since-hours", type=float, default=None,
                        help="log error window in hours (default: since the last check, else 26)")
    parser.add_argument("--log-lines", type=int, default=3000,
                        help="log lines to scan per application (default: 3000)")
    parser.add_argument("--apps", default="",
                        help="only scan logs of applications whose name contains one of these comma-separated strings")
    parser.add_argument("--no-logs", action="store_true", help="skip the log scan entirely")
    parser.add_argument("--all", action="store_true",
                        help="also show fingerprints listed in ignore.txt")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print the full report as JSON instead of text")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help=f"env file with COOLIFY_API_URL/COOLIFY_API_TOKEN (default: {DEFAULT_ENV_FILE})")
    parser.add_argument("--state-file", default=os.getenv("COOLIFY_WATCHDOG_STATE", DEFAULT_STATE_FILE),
                        help=f"where seen fingerprints are remembered (default: {DEFAULT_STATE_FILE})")
    parser.add_argument("--no-state", action="store_true",
                        help="do not read or update the state file (everything counts as new)")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    args = parser.parse_args(argv)

    api_url, token = load_config(args.env_file)
    if not api_url or not token:
        print("ERROR: COOLIFY_API_URL and COOLIFY_API_TOKEN are required "
              f"(set them as environment variables or in {args.env_file})", file=sys.stderr)
        return 2

    ignore_file = os.getenv(
        "IGNORE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ignore.txt")
    )
    ignored = load_ignored(ignore_file)
    state_path = Path(args.state_file).expanduser()
    state = {"fingerprints": {}, "last_run": None} if args.no_state else load_state(state_path)

    report = run_check(api_url, token, args, ignored, state)

    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        color = sys.stdout.isatty() and not args.no_color
        print(render_text(report, color), end="")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(render_markdown(report))

    if not args.no_state and report["instance"]["ok"]:
        update_state(state, report)
        try:
            save_state(state_path, state)
        except OSError as exc:
            print(f"WARNING: could not save state to {state_path}: {exc}", file=sys.stderr)

    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())

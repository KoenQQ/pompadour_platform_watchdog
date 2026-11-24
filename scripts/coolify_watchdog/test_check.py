"""Unit tests for check.py (config, classification, state, rendering)."""
from datetime import datetime, timezone
from pathlib import Path

import check


def test_parse_env_file(tmp_path: Path):
    env = tmp_path / "env"
    env.write_text(
        "# comment\n"
        "export COOLIFY_API_URL='https://apps.example.com'\n"
        'COOLIFY_API_TOKEN="secret-token"\n'
        "MALFORMED LINE\n"
    )
    values = check.parse_env_file(env)
    assert values == {
        "COOLIFY_API_URL": "https://apps.example.com",
        "COOLIFY_API_TOKEN": "secret-token",
    }


def test_parse_env_file_missing(tmp_path: Path):
    assert check.parse_env_file(tmp_path / "nope") == {}


def test_classify_status():
    assert check.classify_status("running:healthy") == ("running:healthy", False)
    assert check.classify_status("running") == ("running", False)
    assert check.classify_status("running:unhealthy") == ("running:unhealthy", True)
    assert check.classify_status("exited:unhealthy") == ("exited:unhealthy", True)
    assert check.classify_status("exited") == ("exited", True)
    assert check.classify_status(None) == ("unknown", False)
    assert check.classify_status("") == ("unknown", False)


def test_server_flags_top_level_and_settings():
    assert check.server_flags({"is_reachable": True, "is_usable": False}) == (True, False)
    assert check.server_flags({"settings": {"is_reachable": False, "is_usable": True}}) == (False, True)
    assert check.server_flags({}) == (None, None)


def test_mark_new():
    errors = [{"fingerprint": "aaaa"}, {"fingerprint": "bbbb"}]
    check.mark_new(errors, {"fingerprints": {"bbbb": "2026-11-20T00:00:00+00:00"}})
    assert [e["new"] for e in errors] == [True, False]


def test_hours_since_last_run():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    assert check.hours_since_last_run({}, now) == check.DEFAULT_SINCE_HOURS
    state = {"last_run": "2026-11-23T12:00:00+00:00"}
    assert check.hours_since_last_run(state, now) == 24.0
    # Clamped: 5 minutes ago -> 1h minimum; a month ago -> 7 days maximum.
    assert check.hours_since_last_run({"last_run": "2026-11-24T11:55:00+00:00"}, now) == 1.0
    assert check.hours_since_last_run({"last_run": "2026-10-01T00:00:00+00:00"}, now) == 24.0 * 7


def test_state_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    state = check.load_state(path)
    report = {
        "checked_at": "2026-11-24T12:00:00+00:00",
        "errors": [{"fingerprint": "cafe001122", "last_seen": "2026-11-24T10:00:00+00:00"}],
    }
    check.update_state(state, report)
    check.save_state(path, state)
    reloaded = check.load_state(path)
    assert reloaded["fingerprints"] == {"cafe001122": "2026-11-24T10:00:00+00:00"}
    assert reloaded["last_run"] == "2026-11-24T12:00:00+00:00"


def _sample_report(problems):
    return {
        "checked_at": "2026-11-24T12:00:00+00:00",
        "instance": {"url": "https://apps.example.com", "ok": True, "version": "4.0.0", "error": None},
        "servers": [{"name": "hetzner-1", "reachable": True, "usable": True}],
        "resources": {
            "applications": [
                {"name": "backend", "uuid": "u1", "status": "running:healthy", "problem": False},
                {"name": "worker", "uuid": "u2", "status": "exited:unhealthy", "problem": True},
            ],
            "services": [],
            "databases": [],
        },
        "deployments_in_progress": 0,
        "since_hours": 26.0,
        "errors": [
            {"fingerprint": "deadbeef00", "app": "backend", "kind": "traceback", "count": 3,
             "first_seen": None, "last_seen": "2026-11-24T09:00:00+00:00",
             "excerpt": "Traceback (most recent call last):\n  ...\nValueError: boom", "new": True},
        ],
        "ignored_errors": 1,
        "log_failures": [],
        "problems": problems,
        "exit_code": 1 if problems else 0,
    }


def test_render_text_lists_problems():
    report = _sample_report(["application 'worker' is exited:unhealthy",
                             "1 new error fingerprint(s) in application logs"])
    text = check.render_text(report, color=False)
    assert "2 problem(s)" in text
    assert "worker" in text and "exited:unhealthy" in text
    assert "[NEW ] deadbeef00" in text
    assert "ValueError" not in text.splitlines()[0]  # excerpt shown, but not in the header


def test_render_text_all_healthy():
    report = _sample_report([])
    report["errors"] = []
    report["ignored_errors"] = 0
    text = check.render_text(report, color=False)
    assert "all healthy" in text


def test_render_markdown():
    report = _sample_report(["application 'worker' is exited:unhealthy"])
    md = check.render_markdown(report)
    assert "🔴" in md
    assert "| worker | 🔴 `exited:unhealthy` |" in md
    assert "deadbeef00" in md

"""Slack failure-alert tests."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import httpx
import pytest

from orbit_data.alerts import (
    Alert,
    cli_send_slack_alert,
    read_webhook,
    send_slack_alert,
    summarize_journal,
)


def _alert() -> Alert:
    return Alert(
        source="orbit-data",
        event="unit-failed",
        severity="critical",
        unit="orbit-data-gp.service",
        host="orbit-host",
        occurred_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )


def test_send_slack_alert_posts_structured_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, text="ok")

    send_slack_alert(
        _alert(),
        webhook="https://hooks.slack.com/services/example",
        transport=httpx.MockTransport(handler),
    )

    assert len(requests) == 1
    assert requests[0].url == "https://hooks.slack.com/services/example"
    assert b'"text"' in requests[0].content
    assert b"orbit-data-gp.service" in requests[0].content
    assert b"journalctl" in requests[0].content


def test_send_slack_alert_rejects_non_slack_webhook() -> None:
    with pytest.raises(ValueError, match=r"hooks\.slack\.com"):
        send_slack_alert(_alert(), webhook="https://example.test/webhook")


def test_read_webhook_strips_a_credential_file(tmp_path: Path) -> None:
    credential = tmp_path / "slack-webhook"
    credential.write_text(" https://hooks.slack.com/services/example\n", encoding="utf-8")

    assert (
        read_webhook(path=credential, stdin=StringIO())
        == "https://hooks.slack.com/services/example"
    )


def test_read_webhook_supports_standard_input() -> None:
    assert (
        read_webhook(path=None, stdin=StringIO("https://hooks.slack.com/services/example\n"))
        == "https://hooks.slack.com/services/example"
    )


def test_cli_send_slack_alert_returns_failure_without_leaking_a_credential(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    credential = tmp_path / "slack-webhook"
    credential.write_text("not-a-webhook", encoding="utf-8")

    assert (
        cli_send_slack_alert(
            _alert(),
            webhook_file=credential,
        )
        == 1
    )
    assert "not-a-webhook" not in caplog.text


def test_cli_send_slack_alert_redacts_a_webhook_from_http_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    webhook = "https://hooks.slack.com/services/secret-token"
    credential = tmp_path / "slack-webhook"
    credential.write_text(webhook, encoding="utf-8")

    def failed_send(*_args: object, **_kwargs: object) -> None:
        request = httpx.Request("POST", webhook)
        httpx.Response(500, request=request).raise_for_status()

    monkeypatch.setattr("orbit_data.alerts.send_slack_alert", failed_send)

    assert cli_send_slack_alert(_alert(), webhook_file=credential) == 1
    assert webhook not in caplog.text
    assert "Slack returned HTTP 500" in caplog.text


def _record(level: str, check: str, severity: str, detail: str) -> str:
    return (
        f'{{"timestamp":"2026-08-15T12:00:00+00:00","level":"{level}","logger":"orbit_data",'
        f'"message":"health check","check":"{check}","severity":"{severity}","detail":"{detail}"}}'
    )


def test_summarize_journal_keeps_only_the_records_that_failed_the_unit() -> None:
    text = "\n".join(
        (
            _record("info", "storage", "ok", "9000 MiB free"),
            _record("warning", "gp-run", "warning", "daily byte budget spent"),
            _record("error", "gp:active", "critical", "41.2h old; last error: HTTP 503"),
            '{"level":"info","message":"health summary","severity":"critical"}',
        )
    )

    assert summarize_journal(text) == (
        "health check check=gp:active severity=critical detail=41.2h old; last error: HTTP 503",
    )


def test_summarize_journal_ignores_pull_progress_beside_a_structured_failure() -> None:
    """`Pull=newer` writes progress lines on every run, failed or not."""

    text = "\n".join(
        (
            "Trying to pull ghcr.io/darkflib/orbit-data:latest...",
            "Getting image source signatures",
            _record("error", "public-tree", "critical", "manifest reports no records"),
        )
    )

    assert summarize_journal(text) == (
        "health check check=public-tree severity=critical detail=manifest reports no records",
    )


def test_summarize_journal_falls_back_to_output_from_a_container_that_never_ran() -> None:
    """An unreachable registry leaves podman's message and nothing else."""

    text = (
        "Trying to pull ghcr.io/darkflib/orbit-data:latest...\n"
        "Error: initializing source docker://ghcr.io/...: reading manifest: connection refused\n"
    )

    assert summarize_journal(text) == (
        "Trying to pull ghcr.io/darkflib/orbit-data:latest...",
        "Error: initializing source docker://ghcr.io/...: reading manifest: connection refused",
    )


def test_summarize_journal_reports_warnings_only_when_nothing_failed() -> None:
    text = _record("warning", "gp-run", "warning", "daily byte budget spent")

    assert summarize_journal(text) == (
        "health check check=gp-run severity=warning detail=daily byte budget spent",
    )


def test_summarize_journal_keeps_the_last_lines_within_the_limit() -> None:
    text = "\n".join(_record("error", f"gp:{index}", "critical", "stale") for index in range(12))

    summary = summarize_journal(text, limit=3)

    assert len(summary) == 3
    assert summary[-1].startswith("health check check=gp:11")


def test_summarize_journal_of_empty_output_is_empty() -> None:
    assert not summarize_journal("")


def test_slack_text_reports_the_systemd_result_and_the_failing_checks() -> None:
    alert = replace(
        _alert(),
        unit="orbit-data-check.service",
        result="exit-code",
        exit_status="1",
        cause=("health check check=gp:active severity=critical detail=41.2h old",),
    )

    text = alert.slack_text()

    assert "*Result:* `exit-code (status 1)`" in text
    assert (
        "*Cause:*\n```\nhealth check check=gp:active severity=critical detail=41.2h old\n```"
    ) in text


def test_slack_text_omits_a_cause_it_was_not_given() -> None:
    text = _alert().slack_text()

    assert "*Cause:*" not in text
    assert "*Result:*" not in text


def test_slack_text_keeps_journal_text_literal_in_slack_markup() -> None:
    alert = replace(_alert(), cause=("cannot stat </srv/orbit-data> & retry",))

    assert "cannot stat &lt;/srv/orbit-data&gt; &amp; retry" in alert.slack_text()


def test_result_without_an_exit_status_is_reported_alone() -> None:
    assert replace(_alert(), result="oom-kill").outcome() == "oom-kill"

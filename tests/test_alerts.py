"""Slack failure-alert tests."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import httpx
import pytest

from orbit_data.alerts import Alert, cli_send_slack_alert, read_webhook, send_slack_alert


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

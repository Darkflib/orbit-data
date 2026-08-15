"""Structured, best-effort Slack failure notifications."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

import httpx

LOGGER = logging.getLogger("orbit_data.alerts")

_SLACK_WEBHOOK_HOSTS = {"hooks.slack.com", "hooks.slack-gov.com"}


@dataclass(frozen=True, slots=True)
class Alert:
    """The stable event shape used by the first alert delivery path."""

    source: str
    event: str
    severity: str
    unit: str
    host: str
    occurred_at: datetime

    def slack_text(self) -> str:
        """Render a compact, copyable alert for Slack."""

        return "\n".join(
            (
                ":rotating_light: *Orbit Data alert*",
                f"*Severity:* {self.severity.upper()}",
                f"*Event:* `{_escape_slack(self.event)}`",
                f"*Unit:* `{_escape_slack(self.unit)}`",
                f"*Host:* `{_escape_slack(self.host)}`",
                f"*Time:* {self.occurred_at.astimezone(UTC).isoformat()}",
                f"Inspect with: `journalctl -u {_escape_slack(self.unit)} -n 100 --no-pager`",
            )
        )


def send_slack_alert(
    alert: Alert,
    *,
    webhook: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Deliver one alert to a configured Slack incoming webhook.

    There are deliberately no delivery retries here. Retrying an unknown Slack
    outcome can create duplicate pages; the failed alert service remains visible
    to systemd and can be retried by an operator once the destination is known
    to be healthy.
    """

    _validate_webhook(webhook)
    with httpx.Client(timeout=10, transport=transport) as client:
        response = client.post(webhook, json={"text": alert.slack_text()})
        response.raise_for_status()
    LOGGER.info(
        "Slack alert delivered",
        extra={
            "source": alert.source,
            "event": alert.event,
            "severity": alert.severity,
            "unit": alert.unit,
            "host": alert.host,
        },
    )


def read_webhook(*, path: Path | None, stdin: TextIO) -> str:
    """Read a webhook from one private input without ever logging it."""

    value = path.read_text(encoding="utf-8") if path is not None else stdin.read()
    webhook = value.strip()
    if not webhook:
        raise ValueError("Slack webhook credential is empty")
    return webhook


def cli_send_slack_alert(alert: Alert, *, webhook_file: Path | None) -> int:
    """Read a systemd credential and deliver the CLI's alert event."""

    try:
        webhook = read_webhook(path=webhook_file, stdin=sys.stdin)
        send_slack_alert(
            alert,
            webhook=webhook,
        )
    except (OSError, ValueError, httpx.HTTPError) as exc:
        # Never include `webhook` in this event: journal access is intentionally
        # broader than access to the systemd credential that supplied it.
        LOGGER.error(
            "Slack alert delivery failed: %s",
            _safe_error_detail(exc),
            extra={
                "source": alert.source,
                "event": alert.event,
                "unit": alert.unit,
            },
        )
        return 1
    return 0


def _validate_webhook(webhook: str) -> None:
    """Reject accidental non-Slack endpoints before sending an alert."""

    parsed = urlsplit(webhook)
    if parsed.scheme != "https" or parsed.hostname not in _SLACK_WEBHOOK_HOSTS or not parsed.path:
        raise ValueError("Slack webhook must be an HTTPS hooks.slack.com URL")


def _safe_error_detail(exc: OSError | ValueError | httpx.HTTPError) -> str:
    """Describe a failed delivery without exposing the webhook URL."""

    if isinstance(exc, httpx.HTTPStatusError):
        return f"Slack returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return f"Slack request failed: {type(exc).__name__}"
    return str(exc)


def _escape_slack(value: str) -> str:
    """Keep systemd-derived text literal in Slack's mrkdwn rendering."""

    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

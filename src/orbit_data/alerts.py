"""Structured, best-effort Slack failure notifications."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

import httpx
import orjson

LOGGER = logging.getLogger("orbit_data.alerts")

_SLACK_WEBHOOK_HOSTS = {"hooks.slack.com", "hooks.slack-gov.com"}

# Slack accepts far more than this, but an alert is read on a phone. Eight
# lines is enough for every critical check in one pass and short enough that
# the unit, host, and result stay on the first screen.
_CAUSE_LINES = 8
_CAUSE_LINE_CHARS = 200
_FAILURE_LEVELS = {"error", "critical"}
# Rendered separately by `_describe_record`, or noise beside the unit and the
# alert's own timestamp.
_RENDERED_FIELDS = {"timestamp", "level", "logger", "message"}


# Every field is one thing the notification has to carry to be actionable.
# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True, slots=True)
class Alert:
    """The stable event shape used by the first alert delivery path."""

    source: str
    event: str
    severity: str
    unit: str
    host: str
    occurred_at: datetime
    # systemd's own verdict, from $MONITOR_SERVICE_RESULT and
    # $MONITOR_EXIT_STATUS. Empty when the manager did not supply it, which is
    # the case on a manual `systemctl start orbit-data-alert@…` and on systemd
    # older than v251.
    result: str = ""
    exit_status: str = ""
    # Lines from the failed invocation's journal, already reduced by
    # `summarize_journal`.
    cause: tuple[str, ...] = field(default_factory=tuple)

    def outcome(self) -> str:
        """How systemd decided the unit failed, in one phrase."""

        if not self.result:
            return ""
        if self.exit_status:
            return f"{self.result} (status {self.exit_status})"
        return self.result

    def slack_text(self) -> str:
        """Render a compact, copyable alert for Slack."""

        lines = [
            ":rotating_light: *Orbit Data alert*",
            f"*Severity:* {self.severity.upper()}",
            f"*Event:* `{_escape_slack(self.event)}`",
            f"*Unit:* `{_escape_slack(self.unit)}`",
            f"*Host:* `{_escape_slack(self.host)}`",
        ]
        outcome = self.outcome()
        if outcome:
            lines.append(f"*Result:* `{_escape_slack(outcome)}`")
        lines.append(f"*Time:* {self.occurred_at.astimezone(UTC).isoformat()}")
        # The cause is what makes the alert actionable rather than merely
        # timely: "exit-code (status 1)" is true of every failed check run,
        # and only these lines say which check went critical.
        if self.cause:
            body = "\n".join(_escape_slack(line) for line in self.cause)
            lines.append(f"*Cause:*\n```\n{body}\n```")
        lines.append(f"Inspect with: `journalctl -u {_escape_slack(self.unit)} -n 100 --no-pager`")
        return "\n".join(lines)


def summarize_journal(text: str, *, limit: int = _CAUSE_LINES) -> tuple[str, ...]:
    """Reduce a failed unit's journal output to the lines that explain it.

    Every job here logs one JSON object per line, so a failing run is usually a
    few error records among the routine ones — for the health check, exactly
    the checks that reached critical.

    Warnings come next, and they must outrank podman's own output rather than
    the other way around: a GP dataset cut off at the daily byte budget counts
    as failed, and so exits the unit non-zero, while logging at warning level.
    A run that fails on warnings alone is not a hypothetical.

    What unstructured output means depends on where it falls. Before the
    application's first record it is podman's preamble — pull progress on a
    `Pull=newer` start, and, if the pull never reached GHCR, the message that
    is then the whole story. After that first record the application has
    stopped logging through its own logger, which means a traceback or a
    runtime kill, and that explains the failure as surely as an error record
    does.
    """

    failures: list[str] = []
    warnings: list[str] = []
    preamble: list[str] = []
    routine: list[str] = []
    logging_started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        document = _json_object(line)
        if document is None:
            (failures if logging_started else preamble).append(_clip(line))
            continue
        logging_started = True
        level = str(document.get("level", "")).lower()
        rendered = _describe_record(document)
        if level in _FAILURE_LEVELS:
            failures.append(rendered)
        elif level == "warning":
            warnings.append(rendered)
        else:
            routine.append(rendered)
    selected = failures or warnings or preamble or routine
    return tuple(selected[-limit:])


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
            "result": alert.outcome(),
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


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        document = orjson.loads(line)
    except orjson.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _describe_record(document: dict[str, Any]) -> str:
    """Flatten one structured log record onto a single readable line.

    Deliberately generic rather than keyed on the health check's own field
    names: the same alert path carries the GP and catalogue units, and a
    renderer that only understands one job's fields silently drops the others.
    """

    message = str(document.get("message", "")).strip()
    fields = " ".join(
        f"{key}={value}" for key, value in document.items() if key not in _RENDERED_FIELDS
    )
    return _clip(f"{message} {fields}".strip())


def _clip(value: str, *, limit: int = _CAUSE_LINE_CHARS) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _escape_slack(value: str) -> str:
    """Keep systemd-derived text literal in Slack's mrkdwn rendering."""

    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

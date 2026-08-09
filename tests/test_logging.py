"""Structured logging tests."""

# pylint: disable=missing-function-docstring

import json
import logging

from orbit_data.logging import JsonFormatter


def test_json_formatter_includes_context() -> None:
    record = logging.LogRecord(
        name="orbit_data.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="updated %s",
        args=("catalog",),
        exc_info=None,
    )
    record.dataset = "active"

    event = json.loads(JsonFormatter().format(record))

    assert event["level"] == "info"
    assert event["message"] == "updated catalog"
    assert event["dataset"] == "active"
    assert event["timestamp"].endswith("+00:00")

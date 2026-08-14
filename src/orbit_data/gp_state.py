"""Persistent state and the failure vocabulary shared by the GP updater.

Separated from the updater itself because the two answer different questions.
This module is about what survives a restart — how far through the rolling byte
allowance we are, when each dataset may next be asked for, what was last
published — and about naming the ways a fetch can fail. `gp.py` is about
deciding what to do next. Keeping them apart also keeps the dependency one-way:
nothing here knows about HTTP, and so nothing here has to be mocked to test it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson

from orbit_data.publishing import atomic_write_json

LOGGER = logging.getLogger("orbit_data.gp")


class GpUpdateError(RuntimeError):
    """A GP update failed and the last-known-good file was retained."""


class StopGpRunError(GpUpdateError):
    """An upstream response requires stopping all remaining queries."""


class BudgetExceededError(GpUpdateError):
    """The rolling daily allowance ran out mid-stream.

    Not a `StopGpRunError`: hitting the backstop is the design working, not an
    upstream fault. The dataset fails, the run continues, and every remaining
    dataset is skipped by the same budget check that would have caught this one
    had its size been knowable in advance.
    """


class DatasetCapExceededError(GpUpdateError):
    """One dataset outgrew its own `maximum_bytes` ceiling mid-stream.

    Neither a stop nor a budget condition. `maximum_bytes` is our own policy
    about one GROUP, so it fails that GROUP and leaves the run — and the rest of
    the shared allowance, which is the whole point of having the cap — alone.
    """

    def __init__(self, message: str, consumed: int) -> None:
        super().__init__(message)
        # A lower bound on the true response size: the stream was cut at the
        # cap, so it is at least this big. Carried on the exception because
        # persisting it is the only thing that stops the next run re-learning
        # the same lesson at CelesTrak's expense.
        self.consumed = consumed


class NetworkGpError(GpUpdateError):
    """The request never reached CelesTrak, so no upstream budget was spent.

    Distinct from `StopGpRunError` because the two demand opposite handling. An
    HTTP response is CelesTrak telling us something, and their guidance is to
    stop the whole run at the first non-200. A connect timeout is not a
    response at all: it costs CelesTrak nothing, carries no instruction, and
    must not be allowed to abort the twelve queries queued behind it.
    """


# The state mirrors the public status document; keeping the fields explicit
# makes migrations and corruption checks safer than an untyped dictionary.
# pylint: disable=too-many-instance-attributes
@dataclass(slots=True)
class DatasetState:
    """Persistent retrieval and publication state for one query."""

    last_attempt: str | None = None
    last_success: str | None = None
    last_http_status: int | None = None
    last_result: str = "never-attempted"
    error: str | None = None
    record_count: int | None = None
    sha256: str | None = None
    earliest_epoch: str | None = None
    latest_epoch: str | None = None
    # The earliest instant this dataset may be requested again. Written before
    # every request so that the floor survives a crash, and shortened only when
    # we know CelesTrak never received the request.
    retry_after: str | None = None
    # Size of the last complete 200 response, on the persistent volume for the
    # same reason `retry_after` is: it is what lets the *next* run decline a
    # request it already knows cannot finish inside the allowance. `None` means
    # never observed, which includes every dataset whose state was written by a
    # release predating this field — `load` fills it from the default.
    last_response_bytes: int | None = None

    @classmethod
    def load(cls, path: Path) -> DatasetState:
        """Load state, refusing to guess if a persisted file is corrupt."""

        if not path.exists():
            return cls()
        try:
            document = orjson.loads(path.read_bytes())
            if not isinstance(document, dict):
                raise ValueError("state is not an object")
            state = cls(**document)
            for field_name in ("last_attempt", "last_success", "retry_after"):
                raw = getattr(state, field_name)
                if raw is not None and datetime.fromisoformat(raw).tzinfo is None:
                    raise ValueError(f"{field_name} has no timezone")
            # Checked here rather than left to the arithmetic in
            # `_expected_bytes`: a string or a list in this field would raise
            # `TypeError` there, which is not a `GpUpdateError` and so escapes
            # the per-dataset handling in `run` — taking down the whole updater,
            # the run summary and every later dataset over one corrupt file.
            # Corruption has to stay isolated to the dataset that owns it.
            # `record_count` has exactly the same exposure by way of
            # `validate_omm_json`, which multiplies it by the drop fraction: a
            # string there raises `TypeError` from inside the validator, which
            # is not an `OmmValidationError` either, so it escapes twice over.
            for count_field in ("last_response_bytes", "record_count"):
                count = getattr(state, count_field)
                if count is not None and (
                    not isinstance(count, int) or isinstance(count, bool) or count < 0
                ):
                    raise ValueError(f"{count_field} must be a non-negative integer")
            return state
        except (OSError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise GpUpdateError(f"invalid persistent state for {path.stem}: {exc}") from exc


# pylint: enable=too-many-instance-attributes


class BandwidthLedger:
    """Rolling 24-hour record of bytes fetched from CelesTrak.

    CelesTrak firewalls IP addresses that pull more than 100 MB/day, and gp.php
    serves no compression, so the allowance is spent in whole uncompressed
    responses. The ledger lives on the persistent volume beside the request
    floor, for the same reason: a restart, or moving the volume to the failover
    host, must not hand the process a fresh allowance it has already spent.
    """

    _WINDOW = timedelta(days=1)

    def __init__(self, path: Path, now: datetime) -> None:
        self.path = path
        self._entries = self._load(path, now)

    @staticmethod
    def _load(path: Path, now: datetime) -> list[tuple[datetime, int]]:
        try:
            document = orjson.loads(path.read_bytes())
        except FileNotFoundError:
            return []
        except (OSError, orjson.JSONDecodeError) as exc:
            # Deliberately fail open, unlike DatasetState. Corrupt dataset state
            # blocks one query; refusing to run without a readable ledger would
            # block all of them, and this is the backstop rather than the
            # primary control. Losing a window of accounting is the smaller harm
            # — but it is not silent.
            LOGGER.warning("discarding unreadable bandwidth ledger", extra={"error": str(exc)})
            return []
        raw = document.get("entries") if isinstance(document, dict) else None
        if not isinstance(raw, list):
            return []
        entries: list[tuple[datetime, int]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            at, count = item.get("at"), item.get("bytes")
            if not isinstance(at, str) or not isinstance(count, int) or isinstance(count, bool):
                continue
            try:
                moment = datetime.fromisoformat(at)
            except ValueError:
                continue
            if moment.tzinfo is not None and now - moment < BandwidthLedger._WINDOW:
                entries.append((moment, count))
        return entries

    def used(self) -> int:
        """Bytes fetched inside the trailing 24-hour window."""

        return sum(count for _, count in self._entries)

    def record(self, now: datetime, count: int) -> None:
        """Add one response to the window and persist it immediately.

        Persisted per response rather than per run so that a run killed by
        `TimeoutStartSec=` still accounts for what it already pulled.
        """

        self._entries = [entry for entry in self._entries if now - entry[0] < self._WINDOW]
        self._entries.append((now.astimezone(UTC), count))
        atomic_write_json(
            self.path,
            {
                "schemaVersion": 1,
                "entries": [
                    {"at": moment.isoformat(), "bytes": size} for moment, size in self._entries
                ],
            },
        )

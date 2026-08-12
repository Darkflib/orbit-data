"""Scheduled CelesTrak GP/OMM cache updater."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import orjson

from orbit_data.config import AppConfig, GpDatasetConfig
from orbit_data.locking import job_lock
from orbit_data.omm import OmmMetadata, OmmValidationError, validate_omm_json
from orbit_data.publishing import atomic_write_bytes, atomic_write_json, ensure_storage

LOGGER = logging.getLogger("orbit_data.gp")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# CelesTrak answers HTTP 403 for two unrelated reasons, and the response body is
# the only thing that separates them. "GP data has not updated since your last
# successful download" is benign: gp.php serves no ETag and no Last-Modified, so
# this refusal *is* the conditional-request mechanism, and under the
# one-download-per-update policy it is the ordinary steady state for the larger
# GROUPs. Any other 403 means we are being refused, and CelesTrak is explicit
# that repeating such a request is what gets an IP firewalled.
_UNCHANGED_MARKER = "has not updated since your last successful"

# Enough of a refusal to identify it in the journal, capped so an upstream error
# page cannot land wholesale in a published status document.
_ERROR_BODY_BYTES = 4096
_ERROR_DETAIL_CHARS = 500

# One dropped connection is weather; two in a row across different queries is a
# pattern, and if we are in CelesTrak's firewall every remaining query will time
# out too. Stop on the second rather than spending a connect timeout per
# dataset, but never on the first — that is what turned a blip into a wedge.
_NETWORK_FAILURE_STOP_THRESHOLD = 2


class GpUpdateError(RuntimeError):
    """A GP update failed and the last-known-good file was retained."""


class StopGpRunError(GpUpdateError):
    """An upstream response requires stopping all remaining queries."""


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
            return state
        except (OSError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise GpUpdateError(f"invalid persistent state for {path.stem}: {exc}") from exc


# pylint: enable=too-many-instance-attributes


# Counters plus the two things an operator actually needs on a bad day: whether
# CelesTrak is refusing us, and how much of the daily allowance is gone.
# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True, slots=True)
class GpRunResult:
    """Summary returned to the CLI and tests."""

    attempted: int
    published: int
    skipped: int
    failed: int
    stopped: bool
    downloaded_bytes: int = 0
    daily_bytes: int = 0
    blocked: bool = False
    stop_reason: str | None = None

    @property
    def successful(self) -> bool:
        """Return whether the job completed without a dataset failure."""

        return self.failed == 0 and not self.stopped


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


class GpUpdater:
    """Fetch configured datasets sequentially and publish validated snapshots."""

    def __init__(
        self,
        config: AppConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self._downloaded = 0
        ensure_storage(config.storage.root)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise GpUpdateError("clock returned a naive datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _instant(now: datetime, offset_seconds: int) -> str:
        return (now + timedelta(seconds=offset_seconds)).astimezone(UTC).isoformat()

    # The loop is a small state machine over per-dataset outcomes; splitting it
    # further would spread the stop conditions across methods that each only
    # make sense together.
    # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    def run(self) -> GpRunResult:
        """Run every due query while holding the single-writer GP lock."""

        attempted = published = skipped = failed = 0
        stopped = blocked = False
        stop_reason: str | None = None
        network_failures = 0
        # Per-run, not per-updater: `sync-gp` builds one updater, but tests and
        # any future caller may reuse one across runs.
        self._downloaded = 0
        started = self._now()
        with job_lock(self.config.storage.root / "locks" / "gp.lock"):
            ledger = BandwidthLedger(self._ledger_path(), started)
            budget = self.config.gp.maximum_daily_bytes
            timeout = httpx.Timeout(
                connect=self.config.gp.connect_timeout_seconds,
                read=self.config.gp.read_timeout_seconds,
                write=self.config.gp.connect_timeout_seconds,
                pool=self.config.gp.connect_timeout_seconds,
            )
            with httpx.Client(
                timeout=timeout,
                transport=self.transport,
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": self.config.gp.user_agent,
                },
            ) as client:
                for dataset in self._ordered_datasets():
                    if ledger.used() >= budget:
                        skipped += 1
                        LOGGER.warning(
                            "skipping GP dataset: daily byte budget spent",
                            extra={
                                "dataset": dataset.name,
                                "used_bytes": ledger.used(),
                                "budget_bytes": budget,
                            },
                        )
                        continue
                    try:
                        outcome = self._update_dataset(client, dataset, ledger)
                    except NetworkGpError as exc:
                        attempted += 1
                        failed += 1
                        network_failures += 1
                        LOGGER.error(
                            "GP dataset unreachable",
                            extra={
                                "dataset": dataset.name,
                                "error": str(exc),
                                "consecutive": network_failures,
                            },
                        )
                        if network_failures >= _NETWORK_FAILURE_STOP_THRESHOLD:
                            stopped = True
                            blocked = True
                            stop_reason = "unreachable"
                            LOGGER.error(
                                "stopping GP update: CelesTrak is not reachable",
                                extra={"consecutive": network_failures},
                            )
                            break
                        continue
                    except StopGpRunError as exc:
                        attempted += 1
                        failed += 1
                        stopped = True
                        stop_reason = str(exc)
                        LOGGER.error(
                            "stopping GP update",
                            extra={"dataset": dataset.name, "error": str(exc)},
                        )
                        break
                    except GpUpdateError as exc:
                        attempted += 1
                        failed += 1
                        LOGGER.error(
                            "GP dataset update failed",
                            extra={"dataset": dataset.name, "error": str(exc)},
                        )
                        continue
                    if outcome == "skipped":
                        skipped += 1
                        continue
                    attempted += 1
                    # Any HTTP response proves CelesTrak is reachable, so a
                    # network failure earlier in this run was genuinely a blip.
                    network_failures = 0
                    if outcome == "published":
                        published += 1
                    elif outcome == "forbidden":
                        failed += 1
                        stopped = True
                        blocked = True
                        stop_reason = "forbidden"
                        break

            result = GpRunResult(
                attempted=attempted,
                published=published,
                skipped=skipped,
                failed=failed,
                stopped=stopped,
                downloaded_bytes=self._downloaded,
                daily_bytes=ledger.used(),
                blocked=blocked,
                stop_reason=stop_reason,
            )
            self._write_summary(result, now=started)
        return result

    # pylint: enable=too-many-branches,too-many-statements,too-many-locals

    def _ordered_datasets(self) -> list[GpDatasetConfig]:
        """Least recently attempted dataset first.

        Configuration order is a fixed queue, so a dataset that fails at
        position zero holds up everything behind it — and for the failures that
        stop the whole run (a 5xx, an unexplained 403, an invalid body) it holds
        them up on every subsequent run too, indefinitely. That is how one
        unreachable GROUP took the other twelve down with it.

        Ordering by `last_attempt` breaks the cycle: a dataset that is tried on
        every run necessarily has the newest attempt, so it sinks to the back
        and the queue behind it drains first. Never-attempted datasets sort
        ahead of everything, and ties break on configuration order, so a fresh
        deployment still runs the list exactly top to bottom.
        """

        def key(item: tuple[int, GpDatasetConfig]) -> tuple[datetime, int]:
            index, dataset = item
            try:
                state = DatasetState.load(self._state_path(dataset))
            except GpUpdateError:
                # Corrupt state sorts first so `_update_dataset` reports it
                # promptly rather than leaving it unexplained at the back.
                return (_EPOCH, index)
            if state.last_attempt is None:
                return (_EPOCH, index)
            return (datetime.fromisoformat(state.last_attempt), index)

        return [dataset for _, dataset in sorted(enumerate(self.config.gp.datasets), key=key)]

    def _update_dataset(
        self, client: httpx.Client, dataset: GpDatasetConfig, ledger: BandwidthLedger
    ) -> str:
        state_path = self._state_path(dataset)
        state = DatasetState.load(state_path)
        now = self._now()
        due_at = self._next_attempt_at(state)
        if due_at is not None and now < due_at:
            LOGGER.info(
                "GP dataset not due",
                extra={"dataset": dataset.name, "due_at": due_at.isoformat()},
            )
            return "skipped"

        attempted_at = now.astimezone(UTC).isoformat()
        state.last_attempt = attempted_at
        state.last_result = "attempting"
        state.error = None
        # Assume the full floor is spent before going near the network: a crash
        # between here and the response must never licence an early retry.
        state.retry_after = self._instant(now, self.config.gp.minimum_interval_seconds)
        self._save_state(dataset, state)

        url = self._url(dataset)
        try:
            status, payload = self._download(client, url)
        except StopGpRunError as exc:
            self._record_failure(dataset, state, "response-too-large", str(exc))
            raise
        except httpx.HTTPError as exc:
            # CelesTrak's application never saw this request, so none of their
            # budget was spent and the two-hour floor does not apply. Surrender
            # minutes to a dropped packet, not a whole cycle.
            state.retry_after = self._instant(now, self.config.gp.network_retry_interval_seconds)
            self._record_failure(dataset, state, "network-error", str(exc))
            raise NetworkGpError(f"network failure: {exc}") from exc

        self._downloaded += len(payload)
        ledger.record(now, len(payload))
        state.last_http_status = status
        if status == 403:
            return self._handle_forbidden(dataset, state, payload)
        if status >= 500:
            self._record_failure(dataset, state, "upstream-error", self._describe(status, payload))
            raise StopGpRunError(f"CelesTrak returned HTTP {status}")
        if status != 200:
            self._record_failure(dataset, state, "http-error", self._describe(status, payload))
            raise StopGpRunError(f"unexpected CelesTrak response HTTP {status}")

        try:
            metadata = validate_omm_json(
                payload,
                dataset,
                previous_record_count=state.record_count,
            )
        except OmmValidationError as exc:
            self._record_failure(dataset, state, "validation-error", str(exc))
            raise StopGpRunError(str(exc)) from exc

        self._publish(dataset, payload, metadata, state)
        LOGGER.info(
            "published GP dataset",
            extra={
                "dataset": dataset.name,
                "records": metadata.record_count,
                "sha256": metadata.sha256,
            },
        )
        return "published"

    def _handle_forbidden(self, dataset: GpDatasetConfig, state: DatasetState, body: bytes) -> str:
        """Separate "you already have this" from "you are being refused"."""

        detail = self._describe(403, body)
        if _UNCHANGED_MARKER in detail.lower():
            state.last_result = "not-updated"
            state.error = None
            self._save_state(dataset, state)
            LOGGER.info(
                "CelesTrak dataset unchanged",
                extra={"dataset": dataset.name, "detail": detail},
            )
            return "not-updated"
        # Anything else is a refusal, and CelesTrak's guidance on a 403 is
        # unambiguous: the answer will not change by asking again, and asking
        # again is what puts an IP in the firewall. Stop the run here and let
        # the recorded body tell whoever reads the journal exactly why.
        self._record_failure(dataset, state, "forbidden", detail)
        LOGGER.error(
            "CelesTrak refused the request",
            extra={"dataset": dataset.name, "detail": detail},
        )
        return "forbidden"

    def _download(self, client: httpx.Client, url: str) -> tuple[int, bytes]:
        chunks: list[bytes] = []
        length = 0
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                # CelesTrak states its reason — which GROUP, which limit, which
                # IP — in the refusal body. Discarding it, as this used to, is
                # why a firewall block and a routine "you already have it" were
                # indistinguishable in the journal.
                return response.status_code, self._read_capped(response, _ERROR_BODY_BYTES)
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > self.config.gp.maximum_response_bytes:
                    raise StopGpRunError(
                        f"response exceeded {self.config.gp.maximum_response_bytes} bytes"
                    )
                chunks.append(chunk)
        return response.status_code, b"".join(chunks)

    @staticmethod
    def _read_capped(response: httpx.Response, limit: int) -> bytes:
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) >= limit:
                break
        return bytes(body[:limit])

    @staticmethod
    def _describe(status: int, body: bytes) -> str:
        """Collapse a refusal into one line safe to log and to publish."""

        text = body.decode("utf-8", errors="replace")
        if "<" in text and ">" in text:
            text = re.sub(r"<[^>]+>", " ", text)
        collapsed = " ".join(text.split())[:_ERROR_DETAIL_CHARS]
        return f"HTTP {status}: {collapsed}" if collapsed else f"HTTP {status}"

    def _publish(
        self,
        dataset: GpDatasetConfig,
        payload: bytes,
        metadata: OmmMetadata,
        state: DatasetState,
    ) -> None:
        target = self.config.storage.root / "public" / "v1" / "gp" / f"{dataset.name}.json"
        atomic_write_bytes(target, payload)
        state.last_success = state.last_attempt
        state.last_result = "published"
        state.error = None
        state.record_count = metadata.record_count
        state.sha256 = metadata.sha256
        state.earliest_epoch = metadata.earliest_epoch
        state.latest_epoch = metadata.latest_epoch
        self._save_state(dataset, state)

    def _record_failure(
        self,
        dataset: GpDatasetConfig,
        state: DatasetState,
        result: str,
        error: str,
    ) -> None:
        state.last_result = result
        state.error = error
        self._save_state(dataset, state)

    def _save_state(self, dataset: GpDatasetConfig, state: DatasetState) -> None:
        document = asdict(state)
        atomic_write_json(self._state_path(dataset), document)
        public = {
            "schemaVersion": 1,
            "dataset": dataset.name,
            "query": dataset.query,
            "value": dataset.value,
            **document,
        }
        atomic_write_json(self._status_path(dataset), public)

    def _write_summary(self, result: GpRunResult, *, now: datetime) -> None:
        atomic_write_json(
            self.config.storage.root / "public" / "v1" / "status" / "gp.json",
            # `checked_at` is what separates "the job ran and found nothing new"
            # from "the timer has not fired in two days"; every other field here
            # looks identical in both cases.
            {"schemaVersion": 1, "checked_at": now.isoformat(), **asdict(result)},
        )

    def _next_attempt_at(self, state: DatasetState) -> datetime | None:
        """The earliest instant this dataset may be requested again."""

        if state.retry_after:
            return datetime.fromisoformat(state.retry_after)
        # State written before `retry_after` existed. Fall back to the floor the
        # previous release enforced, so upgrading never licences an immediate
        # re-request against a dataset already fetched minutes ago.
        if state.last_attempt:
            return datetime.fromisoformat(state.last_attempt) + timedelta(
                seconds=self.config.gp.minimum_interval_seconds
            )
        return None

    def _ledger_path(self) -> Path:
        # Beside the per-dataset directory rather than inside it: dataset names
        # are lowercase-alphanumeric, so any file in `state/gp/` could legally
        # be claimed by a dataset of the same name.
        return self.config.storage.root / "state" / "gp-bandwidth.json"

    def _url(self, dataset: GpDatasetConfig) -> str:
        separator = "&" if "?" in self.config.gp.base_url else "?"
        query = urlencode({dataset.query: dataset.value, "FORMAT": "JSON"})
        return f"{self.config.gp.base_url}{separator}{query}"

    def _state_path(self, dataset: GpDatasetConfig) -> Path:
        return self.config.storage.root / "state" / "gp" / f"{dataset.name}.json"

    def _status_path(self, dataset: GpDatasetConfig) -> Path:
        return self.config.storage.root / "public" / "v1" / "status" / "gp" / f"{dataset.name}.json"

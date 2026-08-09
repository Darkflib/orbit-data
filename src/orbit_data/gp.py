"""Scheduled CelesTrak GP/OMM cache updater."""

from __future__ import annotations

import logging
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


class GpUpdateError(RuntimeError):
    """A GP update failed and the last-known-good file was retained."""


class StopGpRunError(GpUpdateError):
    """An upstream response requires stopping all remaining queries."""


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
            if state.last_attempt is not None:
                parsed_attempt = datetime.fromisoformat(state.last_attempt)
                if parsed_attempt.tzinfo is None:
                    raise ValueError("last_attempt has no timezone")
            if state.last_success is not None:
                parsed_success = datetime.fromisoformat(state.last_success)
                if parsed_success.tzinfo is None:
                    raise ValueError("last_success has no timezone")
            return state
        except (OSError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise GpUpdateError(f"invalid persistent state for {path.stem}: {exc}") from exc


# pylint: enable=too-many-instance-attributes


@dataclass(frozen=True, slots=True)
class GpRunResult:
    """Summary returned to the CLI and tests."""

    attempted: int
    published: int
    skipped: int
    failed: int
    stopped: bool

    @property
    def successful(self) -> bool:
        """Return whether the job completed without a dataset failure."""

        return self.failed == 0 and not self.stopped


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
        ensure_storage(config.storage.root)

    def run(self) -> GpRunResult:
        """Run every due query while holding the single-writer GP lock."""

        attempted = published = skipped = failed = 0
        stopped = False
        consecutive_forbidden = 0
        with job_lock(self.config.storage.root / "locks" / "gp.lock"):
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
                for dataset in self.config.gp.datasets:
                    try:
                        outcome = self._update_dataset(client, dataset)
                    except StopGpRunError as exc:
                        attempted += 1
                        failed += 1
                        stopped = True
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
                    else:
                        attempted += 1
                        if outcome == "published":
                            published += 1
                            consecutive_forbidden = 0
                        elif outcome == "not-updated":
                            consecutive_forbidden += 1
                            if consecutive_forbidden >= 2:
                                failed += 1
                                stopped = True
                                LOGGER.error(
                                    "stopping after consecutive HTTP 403 responses",
                                    extra={"dataset": dataset.name},
                                )
                                break

            result = GpRunResult(
                attempted=attempted,
                published=published,
                skipped=skipped,
                failed=failed,
                stopped=stopped,
            )
            self._write_summary(result)
        return result

    def _update_dataset(self, client: httpx.Client, dataset: GpDatasetConfig) -> str:
        state_path = self._state_path(dataset)
        state = DatasetState.load(state_path)
        now = self.clock()
        if now.tzinfo is None:
            raise GpUpdateError("clock returned a naive datetime")
        if self._inside_minimum_interval(state, now):
            LOGGER.info("GP dataset not due", extra={"dataset": dataset.name})
            return "skipped"

        attempted_at = now.astimezone(UTC).isoformat()
        state.last_attempt = attempted_at
        state.last_result = "attempting"
        state.error = None
        self._save_state(dataset, state)

        url = self._url(dataset)
        try:
            status, payload = self._download(client, url)
        except StopGpRunError as exc:
            self._record_failure(dataset, state, "response-too-large", str(exc))
            raise
        except httpx.HTTPError as exc:
            self._record_failure(dataset, state, "network-error", str(exc))
            raise StopGpRunError(f"network failure: {exc}") from exc

        state.last_http_status = status
        if status == 403:
            state.last_result = "not-updated"
            state.error = None
            self._save_state(dataset, state)
            LOGGER.info("CelesTrak dataset unchanged", extra={"dataset": dataset.name})
            return "not-updated"
        if status >= 500:
            self._record_failure(dataset, state, "upstream-error", f"HTTP {status}")
            raise StopGpRunError(f"CelesTrak returned HTTP {status}")
        if status != 200:
            self._record_failure(dataset, state, "http-error", f"HTTP {status}")
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

    def _download(self, client: httpx.Client, url: str) -> tuple[int, bytes]:
        chunks: list[bytes] = []
        length = 0
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                return response.status_code, b""
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > self.config.gp.maximum_response_bytes:
                    raise StopGpRunError(
                        f"response exceeded {self.config.gp.maximum_response_bytes} bytes"
                    )
                chunks.append(chunk)
        return response.status_code, b"".join(chunks)

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

    def _write_summary(self, result: GpRunResult) -> None:
        atomic_write_json(
            self.config.storage.root / "public" / "v1" / "status" / "gp.json",
            {"schemaVersion": 1, **asdict(result)},
        )

    def _inside_minimum_interval(self, state: DatasetState, now: datetime) -> bool:
        if state.last_attempt is None:
            return False
        last_attempt = datetime.fromisoformat(state.last_attempt)
        return now < last_attempt + timedelta(seconds=self.config.gp.minimum_interval_seconds)

    def _url(self, dataset: GpDatasetConfig) -> str:
        separator = "&" if "?" in self.config.gp.base_url else "?"
        query = urlencode({dataset.query: dataset.value, "FORMAT": "JSON"})
        return f"{self.config.gp.base_url}{separator}{query}"

    def _state_path(self, dataset: GpDatasetConfig) -> Path:
        return self.config.storage.root / "state" / "gp" / f"{dataset.name}.json"

    def _status_path(self, dataset: GpDatasetConfig) -> Path:
        return self.config.storage.root / "public" / "v1" / "status" / "gp" / f"{dataset.name}.json"

"""Scheduled CelesTrak GP/OMM cache updater."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import orjson

from orbit_data.config import AppConfig, GpDatasetConfig, GpDerivedConfig
from orbit_data.gp_state import (
    BandwidthLedger,
    BudgetExceededError,
    DatasetCapExceededError,
    DatasetState,
    GpUpdateError,
    NetworkGpError,
    StopGpRunError,
)
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

# A dataset's last response size is the only forecast available — gp.php sends
# no Content-Length ahead of the body and answers no HEAD — but catalogues grow
# between runs, so an estimate equal to the last size is systematically low. A
# dataset whose previous size only just fits would be admitted, cut off at the
# ceiling, and waste every byte it pulled: precisely the outcome the pre-flight
# check exists to avoid. Pad it rather than rediscover the growth at CelesTrak's
# expense; 5% is far more than a few hours of catalogue churn.
_SIZE_ESTIMATE_MARGIN_PERCENT = 5


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
    # The allowance and what is left of it, published rather than left to be
    # derived. `daily_bytes` alone answers "how much have we spent" but not "of
    # what", which is the question anyone comparing this service against
    # CelesTrak's 100 MB/day threshold is actually asking. The headroom is not
    # quite `budget_bytes - daily_bytes` either: a stream is cut *after* the
    # chunk that crosses the ceiling, so the window can end fractionally over
    # and the difference can go negative. It is clamped at zero here once,
    # rather than in every consumer.
    budget_bytes: int = 0
    budget_remaining_bytes: int = 0
    blocked: bool = False
    # Distinct from `skipped`, which also counts datasets that were simply not
    # due. Without this a spent allowance is indistinguishable from a quiet,
    # healthy run until the per-dataset ages age out many hours later.
    budget_exhausted: bool = False
    stop_reason: str | None = None
    # Counted apart from `attempted`/`published`/`failed`, which exist to
    # describe what this service asked of CelesTrak. A derived dataset makes no
    # request at all, so folding it into those totals would misreport the one
    # number the one-download-per-update policy is measured against.
    derived_published: int = 0
    derived_failed: int = 0

    @property
    def successful(self) -> bool:
        """Return whether the job completed without a dataset failure."""

        return self.failed == 0 and self.derived_failed == 0 and not self.stopped


# pylint: enable=too-many-instance-attributes


@dataclass(frozen=True, slots=True)
class _StreamLimit:
    """The lowest ceiling one response must stay under, and whose it is.

    Three independent limits bound the same stream — the shared daily
    allowance, the global response limit, and the dataset's own optional cap —
    and hitting each one means something different. Reducing them to a bare
    minimum() loses the only thing that decides whether the run stops, the
    dataset fails, or the backstop simply worked.
    """

    limit: int
    source: str


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
        self._derived_published = 0
        self._derived_failed = 0
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
        stopped = blocked = budget_exhausted = False
        stop_reason: str | None = None
        network_failures = 0
        # Per-run, not per-updater: `sync-gp` builds one updater, but tests and
        # any future caller may reuse one across runs.
        self._downloaded = 0
        self._derived_published = self._derived_failed = 0
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
                    remaining = budget - ledger.used()
                    if remaining <= 0:
                        skipped += 1
                        budget_exhausted = True
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
                        outcome = self._update_dataset(client, dataset, ledger, remaining)
                    except BudgetExceededError as exc:
                        # The stream was cut at the ceiling rather than allowed
                        # to overshoot it. Not a stop: the loop's own budget
                        # check skips whatever is left.
                        attempted += 1
                        failed += 1
                        budget_exhausted = True
                        LOGGER.warning(
                            "GP dataset aborted at the daily byte budget",
                            extra={"dataset": dataset.name, "error": str(exc)},
                        )
                        continue
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
                    if outcome == "budget-skipped":
                        # Declined before the connection was opened, so nothing
                        # was spent and nothing was wasted — but datasets are
                        # being dropped, and that has to reach `check-health`
                        # exactly as a mid-stream abort would.
                        skipped += 1
                        budget_exhausted = True
                        continue
                    if outcome in {"skipped", "over-cap"}:
                        # An over-cap dataset is deliberately not
                        # `budget_exhausted`: the shared allowance is fine, one
                        # dataset's own ceiling is not, and conflating them
                        # points whoever is paged at the wrong knob.
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
                budget_bytes=budget,
                budget_remaining_bytes=max(budget - ledger.used(), 0),
                blocked=blocked,
                budget_exhausted=budget_exhausted,
                stop_reason=stop_reason,
                derived_published=self._derived_published,
                derived_failed=self._derived_failed,
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

        A dataset skipped by `_preflight` deliberately leaves `last_attempt`
        alone and therefore stays at the head of this queue — see that method
        for why that is the safe direction rather than the starving one.
        """

        def key(item: tuple[int, GpDatasetConfig]) -> tuple[datetime, int]:
            index, dataset = item
            try:
                state = DatasetState.load(self._state_path(dataset.name))
            except GpUpdateError:
                # Corrupt state sorts first so `_update_dataset` reports it
                # promptly rather than leaving it unexplained at the back.
                return (_EPOCH, index)
            if state.last_attempt is None:
                return (_EPOCH, index)
            return (datetime.fromisoformat(state.last_attempt), index)

        return [dataset for _, dataset in sorted(enumerate(self.config.gp.datasets), key=key)]

    def _update_dataset(
        self,
        client: httpx.Client,
        dataset: GpDatasetConfig,
        ledger: BandwidthLedger,
        budget_remaining: int,
    ) -> str:
        state_path = self._state_path(dataset.name)
        state = DatasetState.load(state_path)
        now = self._now()
        due_at = self._next_attempt_at(state)
        if due_at is not None and now < due_at:
            LOGGER.info(
                "GP dataset not due",
                extra={"dataset": dataset.name, "due_at": due_at.isoformat()},
            )
            return "skipped"

        # Ordered after the due check on purpose: a dataset that is not due yet
        # has no pending request to decline, and recording a budget refusal
        # against it would overwrite a perfectly good `last_result` with a
        # verdict about a request nobody was going to make.
        declined = self._preflight(dataset, state, budget_remaining)
        if declined is not None:
            return declined

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
            status, payload = self._download(
                client,
                url,
                ledger=ledger,
                now=now,
                bound=self._stream_limit(dataset, budget_remaining),
            )
        except BudgetExceededError as exc:
            self._record_failure(dataset, state, "budget-exceeded", str(exc))
            raise
        except DatasetCapExceededError as exc:
            # Persist the lower bound before recording the failure. Without it
            # `last_response_bytes` keeps its old below-cap value — or stays
            # `None` on a fresh deployment — so `_preflight` admits this dataset
            # again on the next run, and every run after that re-downloads up to
            # the cap out of the shared allowance. That is precisely the spend
            # the cap exists to prevent, and it would recur silently until an
            # operator noticed the daily figure.
            state.last_response_bytes = exc.consumed
            self._record_failure(dataset, state, "over-dataset-cap", str(exc))
            raise
        except StopGpRunError as exc:
            self._record_failure(dataset, state, "response-too-large", str(exc))
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Failed before the request went out, so CelesTrak's application
            # never saw it, none of their budget was spent, and the two-hour
            # floor does not apply. Surrender minutes to a dropped packet rather
            # than a whole cycle. This is the failure the production incident hit.
            state.retry_after = self._instant(now, self.config.gp.network_retry_interval_seconds)
            self._record_failure(dataset, state, "network-error", str(exc))
            raise NetworkGpError(f"network failure: {exc}") from exc
        except httpx.HTTPError as exc:
            # Everything else — a read timeout, a reset part-way through the
            # body, a protocol error — happened *after* the request was
            # transmitted. CelesTrak received it and may well have served it in
            # full, so it cost them a response and counts against their
            # one-download-per-update policy. Keep the full floor: re-asking in
            # fifteen minutes for a dataset they already sent is exactly the
            # behaviour that earns a firewall entry.
            self._record_failure(dataset, state, "network-error", str(exc))
            raise NetworkGpError(f"network failure after request: {exc}") from exc

        state.last_http_status = status
        if status == 200:
            # Only a complete 200 body is a usable forecast. A "not updated" 403
            # is a couple of hundred bytes and an aborted stream stopped at
            # whichever ceiling it hit, so recording either would teach the
            # pre-flight check that the largest GROUP is tiny — and hand back
            # exactly the mid-stream cut-off it exists to prevent. Recorded
            # before validation because an unparseable body still crossed the
            # wire at full size.
            state.last_response_bytes = len(payload)
        return self._handle_response(dataset, state, status, payload)

    def _handle_response(
        self,
        dataset: GpDatasetConfig,
        state: DatasetState,
        status: int,
        payload: bytes,
    ) -> str:
        """Classify a response CelesTrak actually served, and publish if valid."""

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
        # Strictly after the source is on disk. A derived file is a view of what
        # was published, so publishing one from a payload that then failed to
        # land would leave the tree self-inconsistent — subsets present, the set
        # they came from stale.
        self._derive_from(dataset, payload)
        return "published"

    def _derive_from(self, source: GpDatasetConfig, payload: bytes) -> None:
        """Publish every dataset derived from this source's records.

        A derived failure is contained here rather than raised. The source has
        already been published successfully and no CelesTrak request is at
        stake, so aborting the run would punish a healthy fetch for a local
        filtering fault — and, because `_ordered_datasets` sorts on
        `last_attempt`, would keep re-punishing the same one. The counters and
        the stale public file are what surface it.
        """

        rules = [rule for rule in self.config.gp.derived if rule.source == source.name]
        if not rules:
            return
        # Safe to parse unchecked: `validate_omm_json` has already accepted this
        # exact payload as a JSON array of well-formed records.
        records = orjson.loads(payload)
        for rule in rules:
            try:
                self._publish_derived(rule, records)
            except GpUpdateError as exc:
                self._derived_failed += 1
                LOGGER.error(
                    "derived GP dataset failed",
                    extra={"dataset": rule.name, "source": source.name, "error": str(exc)},
                )
            else:
                self._derived_published += 1

    def _publish_derived(self, rule: GpDerivedConfig, records: list[Any]) -> None:
        """Filter, validate and publish one derived dataset."""

        state = DatasetState.load(self._state_path(rule.name))
        selected = [record for record in records if self._selects(rule, record)]
        payload = orjson.dumps(selected)
        try:
            metadata = validate_omm_json(payload, rule, previous_record_count=state.record_count)
        except OmmValidationError as exc:
            # The same guard a fetched dataset gets, and it earns its place here
            # for a different reason: upstream renaming a family of objects
            # would silently empty a layer, and the count floor is the only
            # thing that notices before a user does.
            state.last_result = "validation-error"
            state.error = str(exc)
            self._save_derived_state(rule, state)
            raise GpUpdateError(str(exc)) from exc

        now = self._now().isoformat()
        atomic_write_bytes(
            self.config.storage.root / "public" / "v1" / "gp" / f"{rule.name}.json", payload
        )
        state.last_attempt = state.last_success = now
        state.last_result = "published"
        state.error = None
        state.record_count = metadata.record_count
        state.sha256 = metadata.sha256
        state.earliest_epoch = metadata.earliest_epoch
        state.latest_epoch = metadata.latest_epoch
        # Deliberately left alone: `retry_after` and `last_http_status` describe
        # a request, and this dataset never makes one. `last_response_bytes`
        # likewise — `_preflight` forecasts a download that will never happen.
        self._save_derived_state(rule, state)
        LOGGER.info(
            "published derived GP dataset",
            extra={
                "dataset": rule.name,
                "source": rule.source,
                "records": metadata.record_count,
                "sha256": metadata.sha256,
            },
        )

    @staticmethod
    def _selects(rule: GpDerivedConfig, record: Any) -> bool:
        """Whether one source record belongs to a derived dataset.

        Every predicate configured must hold. A record that cannot be tested —
        a name that is not a string, a mean motion that will not parse — is
        excluded rather than admitted: a derived dataset publishing a record it
        could not classify is worse than one that is short by it, and the count
        guards catch the case where that starts happening at scale.
        """

        if not isinstance(record, dict):
            return False
        if rule.pattern is not None:
            name = record.get("OBJECT_NAME")
            if not isinstance(name, str) or not rule.pattern.search(name):
                return False
        if rule.minimum_mean_motion is None and rule.maximum_mean_motion is None:
            return True
        try:
            mean_motion = float(record["MEAN_MOTION"])
        except (KeyError, TypeError, ValueError):
            return False
        if rule.minimum_mean_motion is not None and mean_motion < rule.minimum_mean_motion:
            return False
        return not (rule.maximum_mean_motion is not None and mean_motion > rule.maximum_mean_motion)

    def _preflight(
        self,
        dataset: GpDatasetConfig,
        state: DatasetState,
        budget_remaining: int,
    ) -> str | None:
        """Decline a request already known not to fit, without opening it.

        The budget check used to be purely reactive: a dataset opened its
        connection and was cut off at the ceiling, spending every byte it had
        already pulled from a service that is rationing us. `last_response_bytes`
        turns that into a decision made before the connection exists.

        A dataset with no recorded size is attempted rather than refused.
        Refusing the unknown would deadlock a fresh deployment — nothing records
        a size without a fetch, so nothing would ever be fetched — and it is safe
        because `_download` still bounds an unknown response to exactly the
        allowance that is left. The worst case for an unknown dataset is
        therefore the old reactive behaviour, not an overshoot.

        A declined dataset deliberately does **not** advance `last_attempt`. It
        never reached the network, and on state written before `retry_after`
        existed `_next_attempt_at` still falls back to `last_attempt` plus the
        two-hour floor — so recording an attempt here would push a genuine
        request out by a full cycle to pay for one that never happened. The
        consequence is that a declined dataset keeps its place at the front of
        `_ordered_datasets`, and that is the direction that does not starve:
        skipping is a `continue` and costs no network, so it cannot wedge the
        queue the way a stopping failure did, and holding the front means the
        dataset that has waited longest gets first claim on the allowance when
        the 24-hour window rolls — rather than watching the small datasets
        behind it nibble every refill away.
        """

        expected = self._expected_bytes(state)
        if expected is None:
            return None
        if dataset.maximum_bytes is not None and expected > dataset.maximum_bytes:
            # Not a budget condition, and not self-clearing either: the daily
            # window rolls, a per-dataset cap does not. This dataset stays
            # skipped until an operator raises the cap or upstream shrinks, so
            # say so loudly — the only other symptom is its own staleness many
            # hours later.
            detail = f"expected {expected} bytes exceeds maximum_bytes {dataset.maximum_bytes}"
            self._record_failure(dataset, state, "over-dataset-cap", detail)
            LOGGER.warning(
                "skipping GP dataset: larger than its configured cap",
                extra={
                    "dataset": dataset.name,
                    "expected_bytes": expected,
                    "maximum_bytes": dataset.maximum_bytes,
                },
            )
            return "over-cap"
        if expected > budget_remaining:
            detail = f"expected {expected} bytes exceeds {budget_remaining} bytes of allowance"
            self._record_failure(dataset, state, "budget-skipped", detail)
            LOGGER.warning(
                "skipping GP dataset: will not fit in the remaining daily allowance",
                extra={
                    "dataset": dataset.name,
                    "expected_bytes": expected,
                    "remaining_bytes": budget_remaining,
                },
            )
            return "budget-skipped"
        return None

    @staticmethod
    def _expected_bytes(state: DatasetState) -> int | None:
        """Forecast this dataset's next response, or None if never observed."""

        last = state.last_response_bytes
        if last is None:
            return None
        return last + last * _SIZE_ESTIMATE_MARGIN_PERCENT // 100

    def _stream_limit(self, dataset: GpDatasetConfig, budget_remaining: int) -> _StreamLimit:
        """The lowest of the three ceilings bounding one response.

        Ties resolve towards the shared limits, because `min` keeps the first
        of equal keys and both run-wide bounds are listed ahead of the
        dataset's own. Reporting a shared ceiling as one GROUP's problem would
        send whoever reads the journal to the wrong knob.
        """

        limits = [
            _StreamLimit(budget_remaining, "budget"),
            _StreamLimit(self.config.gp.maximum_response_bytes, "response"),
        ]
        if dataset.maximum_bytes is not None:
            limits.append(_StreamLimit(dataset.maximum_bytes, "dataset"))
        return min(limits, key=lambda bound: bound.limit)

    def _handle_forbidden(self, dataset: GpDatasetConfig, state: DatasetState, body: bytes) -> str:
        """Separate "you already have this" from "you are being refused"."""

        detail = self._describe(403, body)
        # Classify against the whole captured body, not the 500-character
        # summary. `_ERROR_DETAIL_CHARS` is a presentation limit; letting it
        # decide the outcome means a longer preamble around the same sentence
        # turns the quietest possible response into a stopped run and a critical
        # health check.
        # Whitespace is collapsed first: CelesTrak hard-wraps this message, so a
        # line break landing inside the marker must not change the verdict.
        full = " ".join(body.decode("utf-8", errors="replace").split()).lower()
        if _UNCHANGED_MARKER in full:
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

    # The three keyword arguments are the run's byte accounting, which has to
    # reach the one place that actually consumes the stream.
    # pylint: disable-next=too-many-arguments
    def _download(
        self,
        client: httpx.Client,
        url: str,
        *,
        ledger: BandwidthLedger,
        now: datetime,
        bound: _StreamLimit,
    ) -> tuple[int, bytes]:
        """Stream one response, accounting every byte that crosses the wire.

        The ledger is updated in `finally`, not on the way out of the success
        path. An oversized body or a mid-stream read error has already cost
        CelesTrak everything it sent, and a backstop that under-counts precisely
        in the heaviest cases is not a backstop: two aborted 64 MiB responses
        would exceed the daily threshold while `daily_bytes` still read zero.

        The bound is whichever ceiling is lowest, carried with its name because
        the three mean different things when hit. Checking the budget only
        between datasets leaves it overshootable by one whole response, which at
        the configured 64 MiB response limit is most of a day's allowance.
        """

        limit = bound.limit
        consumed = 0
        try:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    # CelesTrak states its reason — which GROUP, which limit,
                    # which IP — in the refusal body. Discarding it, as this used
                    # to, is why a firewall block and a routine "you already have
                    # it" were indistinguishable in the journal.
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        consumed += len(chunk)
                        body.extend(chunk)
                        if len(body) >= _ERROR_BODY_BYTES:
                            break
                    return response.status_code, bytes(body[:_ERROR_BODY_BYTES])
                chunks: list[bytes] = []
                for chunk in response.iter_bytes():
                    consumed += len(chunk)
                    if consumed > limit:
                        raise self._oversized(bound, consumed)
                    chunks.append(chunk)
                return response.status_code, b"".join(chunks)
        finally:
            self._downloaded += consumed
            if consumed:
                ledger.record(now, consumed)

    @staticmethod
    def _oversized(bound: _StreamLimit, consumed: int) -> GpUpdateError:
        """Name whichever ceiling the stream actually hit.

        All three abort the same stream and mean entirely different things: an
        exhausted allowance is the backstop working as designed and the
        remaining datasets simply have nothing left to spend; a breached
        per-dataset cap is one GROUP outgrowing its own ration; only a body over
        the global response limit is unexplained enough to stop the run.
        """

        if bound.source == "budget":
            return BudgetExceededError(f"daily byte budget exhausted after {consumed} bytes")
        if bound.source == "dataset":
            return DatasetCapExceededError(
                f"response exceeded this dataset's {bound.limit}-byte cap", consumed
            )
        return StopGpRunError(f"response exceeded {bound.limit} bytes")

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

    def _save_derived_state(self, rule: GpDerivedConfig, state: DatasetState) -> None:
        """Persist a derived dataset's state and its public status document.

        The status document says how this dataset was produced rather than what
        was requested for it, so that a reader of the served tree can tell a
        filtered view from a fetched one without access to the configuration —
        which is the difference between "CelesTrak is stale" and "our own rule
        stopped matching". Additive: `schemaVersion` stays at 1, and the keys a
        fetched dataset carries about its request are simply absent.
        """

        document = asdict(state)
        atomic_write_json(self._state_path(rule.name), document)
        atomic_write_json(
            self._status_path(rule.name),
            {
                "schemaVersion": 1,
                "dataset": rule.name,
                "derived_from": rule.source,
                "pattern": rule.pattern.pattern if rule.pattern is not None else None,
                "minimum_mean_motion": rule.minimum_mean_motion,
                "maximum_mean_motion": rule.maximum_mean_motion,
                **document,
            },
        )

    def _save_state(self, dataset: GpDatasetConfig, state: DatasetState) -> None:
        document = asdict(state)
        atomic_write_json(self._state_path(dataset.name), document)
        public = {
            "schemaVersion": 1,
            "dataset": dataset.name,
            "query": dataset.query,
            "value": dataset.value,
            # `last_response_bytes` arrives with the state document; the cap is
            # configuration, and publishing it beside the size is what makes
            # "which GROUP is eating the allowance, and was it rationed" an
            # answerable question from the served tree alone, with no journal
            # access. Additive: `schemaVersion` deliberately stays at 1.
            "maximum_bytes": dataset.maximum_bytes,
            **document,
        }
        atomic_write_json(self._status_path(dataset.name), public)

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

    # Keyed on the name rather than the dataset because fetched and derived
    # datasets publish into the same two directories; `config` rejects a name
    # used by both, so a name is enough to identify either.
    def _state_path(self, name: str) -> Path:
        return self.config.storage.root / "state" / "gp" / f"{name}.json"

    def _status_path(self, name: str) -> Path:
        return self.config.storage.root / "public" / "v1" / "status" / "gp" / f"{name}.json"

"""Datasets published by filtering another, with no request of their own.

Separate from the updater because the two have nothing in common but the volume
they write to. Nothing here knows about HTTP: derivation consumes records that
were already fetched, validated and published, so it needs no transport, no
budget and no request floor — and needs none of them mocked to be tested.

The direction of the dependency is what keeps that true. `gp.py` reaches in here
once per run; nothing here reaches back.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

import orjson

from orbit_data.config import AppConfig, GpDerivedConfig
from orbit_data.gp_state import DatasetState, GpUpdateError, state_path, status_path
from orbit_data.omm import OmmValidationError, validate_omm_json
from orbit_data.publishing import atomic_write_bytes, atomic_write_json

LOGGER = logging.getLogger("orbit_data.gp")


class GpDeriver:
    """Reconcile every configured derived dataset against its published source.

    One instance per run. The counters are read back by the updater for the run
    summary, and are deliberately kept apart from the fetch totals: a derived
    dataset makes no request, so folding it into `attempted`/`published` would
    misreport the one number CelesTrak's one-download-per-update policy is
    measured against.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.published = 0
        self.failed = 0

    def _state_path(self, name: str) -> Path:
        return state_path(self.config.storage.root, name)

    def _status_path(self, name: str) -> Path:
        return status_path(self.config.storage.root, name)

    def sync(self) -> None:
        """Bring every derived dataset up to date with its published source.

        Deliberately a reconciliation against what is on the volume rather than
        a step hung off a successful fetch. Derivation needs the *records*, not
        the response that carried them, and the three commonest states of a
        healthy run supply no response at all: a source inside its request floor
        is skipped, a routine "not updated" 403 is the steady state under
        one-download-per-update, and a fresh deployment starts with neither. In
        every one of those a perfectly good `active.json` is sitting on disk. A
        derive-on-response path would leave its subsets missing until CelesTrak
        next happened to update — on a new volume, missing entirely, which
        `check-health` reports as critical.

        Reading the published file back also collapses the two paths into one:
        the file is written atomically before its state records the success, so
        a source published seconds ago in this very run is picked up here by the
        same code that recovers one published days ago.

        A derived failure never propagates. No CelesTrak request is at stake, so
        failing the run would punish a healthy fetch for a local filtering fault
        — and, because `_ordered_datasets` sorts on `last_attempt`, would keep
        re-punishing the same one. The counters and the retained file surface it.
        """

        for source_name, rules in self._derived_by_source().items():
            self._sync_source(source_name, rules)

    def _sync_source(self, source_name: str, rules: list[GpDerivedConfig]) -> None:
        """Reconcile one source's derived datasets against its published file."""

        source_success = self._derived_state(source_name).last_success
        if source_success is None:
            # Nothing has ever been published under this name, so there is no
            # cached payload to filter. Not a failure: the first successful
            # fetch brings every rule below it along.
            return
        # Keyed on the source's publication instant rather than a timestamp of
        # our own, so a derived dataset is exactly as fresh as what it was
        # filtered from — which makes this both an idempotency check and an
        # honest answer for the age check in `check-health`.
        stale: list[tuple[GpDerivedConfig, DatasetState]] = []
        for rule in rules:
            state = self._derived_state(rule.name)
            if state.last_success == source_success:
                self._repair_derived_state(rule, state)
            else:
                stale.append((rule, state))
        if not stale:
            return
        try:
            records = self._published_records(source_name)
        except GpUpdateError as exc:
            for rule, state in stale:
                self._fail_derived(rule, state, "source-unreadable", str(exc))
            return
        for rule, state in stale:
            try:
                self._publish_derived(rule, state, records, source_success)
            except OmmValidationError as exc:
                self._fail_derived(rule, state, "validation-error", str(exc))
            else:
                self.published += 1

    def _derived_state(self, name: str) -> DatasetState:
        """Load persistent state, discarding it rather than failing the run.

        Two reasons this does not propagate the way a fetched dataset's
        corruption does. The narrow one is that `_sync_derived` sits outside the
        per-dataset handling `run` gives a query, so a raised `GpUpdateError`
        here would take down the whole updater over one unreadable file — the
        exact blast radius the guards in `DatasetState.load` exist to prevent.

        The broader one is that there is nothing here worth protecting. A
        derived dataset's state is entirely reconstructible from its source, so
        a discarded file costs one re-derivation and no upstream request at all,
        where discarding a fetched dataset's state would forfeit its request
        floor. Losing it is the smaller harm — but it is not silent.
        """

        try:
            return DatasetState.load(self._state_path(name))
        except GpUpdateError as exc:
            LOGGER.warning(
                "discarding unreadable derived state",
                extra={"dataset": name, "error": str(exc)},
            )
            return DatasetState()

    def _repair_derived_state(self, rule: GpDerivedConfig, state: DatasetState) -> None:
        """Strip request-shaped fields from a dataset that is already current.

        A dataset converted from fetched to derived arrives carrying the state
        of its last real request, and on a volume that has already run once the
        conversion is *complete*: `last_success` matches its source, so nothing
        below rebuilds it and no later pass would ever revisit it. The fields
        would then sit in the published status document until the source
        happened to publish again — indefinitely, if it never did.

        Rewrites the state and status documents only. The published records are
        unchanged, so the file keeps its `Last-Modified`; the browser derives
        its own idea of freshness from that header, and moving it for a metadata
        repair would report data as newer than the epochs inside it.
        """

        if (state.last_http_status, state.retry_after, state.last_response_bytes) == (
            None,
            None,
            None,
        ):
            return
        self._save_derived_state(rule, state)
        LOGGER.info(
            "cleared inherited request fields from derived dataset",
            extra={"dataset": rule.name, "source": rule.source},
        )

    def _derived_by_source(self) -> dict[str, list[GpDerivedConfig]]:
        """Group the rules so one source is read and parsed at most once."""

        grouped: dict[str, list[GpDerivedConfig]] = {}
        for rule in self.config.gp.derived:
            grouped.setdefault(rule.source, []).append(rule)
        return grouped

    def _published_records(self, source_name: str) -> list[Any]:
        """Re-read a source this service already published and validated."""

        path = self.config.storage.root / "public" / "v1" / "gp" / f"{source_name}.json"
        try:
            records = orjson.loads(path.read_bytes())
        except (OSError, orjson.JSONDecodeError) as exc:
            raise GpUpdateError(f"cannot read published source {source_name}: {exc}") from exc
        if not isinstance(records, list):
            raise GpUpdateError(f"published source {source_name} is not a JSON array")
        return records

    def _fail_derived(
        self, rule: GpDerivedConfig, state: DatasetState, result: str, error: str
    ) -> None:
        """Record a derived failure without disturbing its last-known-good file."""

        state.last_result = result
        state.error = error
        self._save_derived_state(rule, state)
        self.failed += 1
        LOGGER.error(
            "derived GP dataset failed",
            extra={"dataset": rule.name, "source": rule.source, "error": error},
        )

    def _publish_derived(
        self,
        rule: GpDerivedConfig,
        state: DatasetState,
        records: list[Any],
        source_success: str,
    ) -> None:
        """Filter, validate and publish one derived dataset."""

        selected = [record for record in records if self._selects(rule, record)]
        payload = orjson.dumps(selected)
        # The same guard a fetched dataset gets, and it earns its place here for
        # a different reason: upstream renaming a family of objects would
        # silently empty a layer, and the count floor is the only thing that
        # notices before a user does.
        metadata = validate_omm_json(payload, rule, previous_record_count=state.record_count)

        atomic_write_bytes(
            self.config.storage.root / "public" / "v1" / "gp" / f"{rule.name}.json", payload
        )
        state.last_attempt = state.last_success = source_success
        state.last_result = "published"
        state.error = None
        state.record_count = metadata.record_count
        state.sha256 = metadata.sha256
        state.earliest_epoch = metadata.earliest_epoch
        state.latest_epoch = metadata.latest_epoch
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

    def _save_derived_state(self, rule: GpDerivedConfig, state: DatasetState) -> None:
        """Persist a derived dataset's state and its public status document.

        Every field describing a request is cleared on the way out, because this
        dataset never makes one. Leaving them merely unset is not enough: a
        dataset converted from fetched to derived inherits the state file it
        already had, so its last real response would sit in the status document
        indefinitely — nothing writes that field for a derived dataset again.
        The nine groups this service converted all carried a `last_http_status`
        of 200, which reads as a request that never happened and undercuts the
        one question this document exists to answer.

        The document then says how this dataset was produced rather than what
        was requested for it, so that a reader of the served tree can tell a
        filtered view from a fetched one without access to the configuration —
        which is the difference between "CelesTrak is stale" and "our own rule
        stopped matching". Additive: `schemaVersion` stays at 1.
        """

        state.last_http_status = None
        state.retry_after = None
        # `_preflight` forecasts a download this dataset will never make, so a
        # size recorded against one is not a stale fact so much as an
        # inapplicable one.
        state.last_response_bytes = None
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

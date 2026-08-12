"""Freshness and capacity checks over the published data tree.

This job never fetches anything and never writes to the public tree. It reads
the status documents the updaters already publish, plus the filesystem, and
reports whether the service is still doing its job. It exists because every
other failure signal here is a *negative*: the GP updater deliberately stops and
reuses last-known-good data on an upstream 5xx, and the catalogue job
deliberately reports `unchanged` when nothing moved. Both are correct
behaviours, both exit zero, and both look identical to a service that quietly
stopped updating days ago. Age is the only thing that separates them.

Severity maps onto the process exit status so systemd can drive alerting:
critical fails the unit (wire `OnFailure=` to a notifier), warning does not.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from orbit_data.config import AppConfig

OK = "ok"
WARNING = "warning"
CRITICAL = "critical"

_SEVERITY_ORDER = {OK: 0, WARNING: 1, CRITICAL: 2}


@dataclass(frozen=True, slots=True)
class Check:
    """One evaluated condition."""

    name: str
    severity: str
    detail: str


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The outcome of a full check pass."""

    checks: tuple[Check, ...]

    @property
    def severity(self) -> str:
        """The worst severity across every check."""

        return max((check.severity for check in self.checks), key=_SEVERITY_ORDER.__getitem__)

    @property
    def successful(self) -> bool:
        """True when nothing reached critical."""

        return self.severity != CRITICAL

    def counts(self) -> dict[str, int]:
        """Per-severity totals, for a single summary log line."""

        return {
            level: sum(1 for check in self.checks if check.severity == level)
            for level in (OK, WARNING, CRITICAL)
        }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        document = orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _age_seconds(raw: object, now: datetime) -> float | None:
    """Seconds between an ISO-8601 instant and ``now``, or None if unusable."""

    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # A naive timestamp cannot be compared against an aware clock without
    # inventing a zone, and guessing UTC could hide a genuinely stale file.
    if parsed.tzinfo is None:
        return None
    return (now - parsed).total_seconds()


def _age_severity(age: float, *, warning: int, critical: int) -> str:
    if age >= critical:
        return CRITICAL
    if age >= warning:
        return WARNING
    return OK


def _describe_age(age: float) -> str:
    return f"{age / 3600:.1f}h old"


def _check_gp(config: AppConfig, now: datetime) -> list[Check]:
    """One check per configured dataset, keyed on its last successful publish."""

    status_root = config.storage.root / "public" / "v1" / "status" / "gp"
    checks: list[Check] = []
    for dataset in config.gp.datasets:
        name = f"gp:{dataset.name}"
        document = _read_json(status_root / f"{dataset.name}.json")
        if document is None:
            checks.append(Check(name, CRITICAL, "status document missing or unreadable"))
            continue
        age = _age_seconds(document.get("last_success"), now)
        if age is None:
            checks.append(Check(name, CRITICAL, "no usable last_success timestamp"))
            continue
        severity = _age_severity(
            age,
            warning=config.health.gp_warning_age_seconds,
            critical=config.health.gp_critical_age_seconds,
        )
        detail = _describe_age(age)
        # Surface why it is stale when the last attempt failed; a bare age tells
        # whoever is woken up nothing about whether upstream or the host is at
        # fault.
        if severity != OK and document.get("error"):
            detail = f"{detail}; last error: {document['error']}"
        checks.append(Check(name, severity, detail))
    return checks


def _check_gp_run(config: AppConfig, now: datetime) -> Check:
    """The GP job itself, as opposed to the freshness of what it published.

    Per-dataset ages answer "is the data stale". They cannot answer "is
    CelesTrak refusing us", because a refused run leaves every last-known-good
    file exactly where it was and only ages into a warning many hours later.
    `blocked` says so on the first run that hits it — which, for a firewall
    block that clears itself in two hours if you stop asking, is the difference
    between a fix and a manual unblock request.
    """

    document = _read_json(config.storage.root / "public" / "v1" / "status" / "gp.json")
    if document is None:
        return Check("gp-run", CRITICAL, "run summary missing or unreadable")
    used_mib = _as_int(document.get("daily_bytes")) / 1024**2
    detail = f"{_as_int(document.get('published'))} published, {used_mib:.1f} MiB/24h"
    # Quote the allowance beside the usage now that the run summary carries it.
    # "33.0 MiB/24h" needs the reader to already know the budget to mean
    # anything, and the one thing an operator woken by this check will not have
    # to hand is the contents of a TOML on the failover host.
    budget = _as_int(document.get("budget_bytes"))
    if budget:
        detail = f"{detail} of {budget / 1024**2:.0f} MiB"
    if document.get("blocked"):
        reason = document.get("stop_reason") or "refused"
        return Check("gp-run", CRITICAL, f"CelesTrak is refusing requests ({reason}); {detail}")
    if document.get("budget_exhausted"):
        # A warning, not a critical: last-known-good data is still being served
        # and the allowance refills as the 24-hour window rolls. But it must not
        # read as healthy — datasets are being skipped, and the only other
        # symptom is staleness that appears many hours later.
        return Check("gp-run", WARNING, f"daily byte budget spent; {detail}")
    age = _age_seconds(document.get("checked_at"), now)
    if age is None:
        # A summary written by a release predating `checked_at`. The per-dataset
        # age checks still cover staleness, so this is not worth paging on.
        return Check("gp-run", OK, detail)
    return Check(
        "gp-run",
        _age_severity(
            age,
            warning=config.health.gp_warning_age_seconds,
            critical=config.health.gp_critical_age_seconds,
        ),
        f"last run {_describe_age(age)}; {detail}",
    )


def _as_int(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _check_catalog(config: AppConfig, now: datetime) -> Check:
    """Catalogue freshness, measured by when the job last *ran*.

    Deliberately not `manifest.generatedAt`: the catalogue only republishes when
    the normalized content changes, so a healthy service can sit on the same
    `generatedAt` for weeks. `checkedAt` moves on every run.
    """

    document = _read_json(config.storage.root / "public" / "v1" / "status" / "catalog.json")
    if document is None:
        return Check("catalog", CRITICAL, "status document missing or unreadable")
    age = _age_seconds(document.get("checkedAt"), now)
    if age is None:
        return Check("catalog", CRITICAL, "no usable checkedAt timestamp")
    severity = _age_severity(
        age,
        warning=config.health.catalog_warning_age_seconds,
        critical=config.health.catalog_critical_age_seconds,
    )
    detail = _describe_age(age)
    # A failed run still serves last-known-good, so it is not critical on its
    # own — but it must not read as healthy, and if it keeps failing the age
    # gate above escalates it on its own.
    if not document.get("successful", False):
        severity = max(severity, WARNING, key=_SEVERITY_ORDER.__getitem__)
        detail = f"{detail}; last run {document.get('result', 'failed')}"
        if document.get("error"):
            detail = f"{detail}: {document['error']}"
    return Check("catalog", severity, detail)


def _check_public_tree(config: AppConfig) -> Check:
    """The published catalogue actually resolves and parses.

    `public/v1/data` is a relative symlink into `releases/`. Retention pruning,
    a half-finished failover, or a volume remounted at the wrong path all leave
    the status documents looking perfectly healthy while the thing clients
    fetch is a dangling link.
    """

    manifest = config.storage.root / "public" / "v1" / "data" / "manifest.json"
    document = _read_json(manifest)
    if document is None:
        return Check("public-tree", CRITICAL, f"{manifest} missing or unreadable")
    counts = document.get("counts")
    records = counts.get("records") if isinstance(counts, dict) else None
    if not isinstance(records, int) or isinstance(records, bool) or records < 1:
        return Check("public-tree", CRITICAL, "manifest reports no records")
    return Check("public-tree", OK, f"{records} records published")


def _check_storage(config: AppConfig) -> Check:
    """Free space on the volume the updaters write to."""

    try:
        usage = shutil.disk_usage(config.storage.root)
    except OSError as exc:
        return Check("storage", CRITICAL, f"cannot stat {config.storage.root}: {exc}")
    free_mib = usage.free / 1024**2
    if usage.free < config.health.free_bytes_critical:
        return Check("storage", CRITICAL, f"{free_mib:.0f} MiB free")
    if usage.free < config.health.free_bytes_warning:
        return Check("storage", WARNING, f"{free_mib:.0f} MiB free")
    return Check("storage", OK, f"{free_mib:.0f} MiB free")


def evaluate(config: AppConfig, *, now: datetime | None = None) -> HealthReport:
    """Run every check and collect the results."""

    moment = now or datetime.now(tz=UTC)
    checks: list[Check] = [
        _check_storage(config),
        _check_public_tree(config),
        _check_catalog(config, moment),
        _check_gp_run(config, moment),
    ]
    checks.extend(_check_gp(config, moment))
    return HealthReport(checks=tuple(checks))

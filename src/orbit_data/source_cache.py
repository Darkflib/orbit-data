"""Conditional HTTP retrieval with persistent last-good source bodies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import orjson

from orbit_data.publishing import atomic_write_bytes, atomic_write_json


class SourceFetchError(RuntimeError):
    """Raised when a source has neither a usable response nor cached body."""


@dataclass(frozen=True, slots=True)
class SourcePayload:
    """A source body and its retrieval provenance."""

    body: bytes
    fetched_at: str
    from_cache: bool
    stale: bool
    error: str | None = None


class ConditionalSourceCache:
    """Persist ETag/Last-Modified metadata and last-good source payloads."""

    def __init__(self, root: Path, *, maximum_response_bytes: int) -> None:
        self.root = root
        self.maximum_response_bytes = maximum_response_bytes
        root.mkdir(parents=True, exist_ok=True)

    def get(self, client: httpx.Client, *, name: str, url: str) -> SourcePayload:
        """Return a current body or fall back to a persisted last-good body."""

        body_path = self.root / f"{name}.body"
        meta_path = self.root / f"{name}.json"
        metadata = self._metadata(meta_path)
        headers: dict[str, str] = {}
        if etag := metadata.get("etag"):
            headers["If-None-Match"] = str(etag)
        if modified := metadata.get("last_modified"):
            headers["If-Modified-Since"] = str(modified)
        try:
            status, body, response_headers = self._download(client, url, headers)
        except (httpx.HTTPError, SourceFetchError) as exc:
            return self._fallback(body_path, metadata, str(exc))

        if status == 304:
            return self._fallback(body_path, metadata, None, stale=False)
        if status != 200:
            return self._fallback(body_path, metadata, f"HTTP {status}")

        fetched_at = datetime.now(tz=UTC).isoformat()
        atomic_write_bytes(body_path, body)
        atomic_write_json(
            meta_path,
            {
                "etag": response_headers.get("etag"),
                "last_modified": response_headers.get("last-modified"),
                "fetched_at": fetched_at,
            },
        )
        return SourcePayload(body=body, fetched_at=fetched_at, from_cache=False, stale=False)

    def _download(
        self,
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
    ) -> tuple[int, bytes, httpx.Headers]:
        chunks: list[bytes] = []
        length = 0
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code != 200:
                return response.status_code, b"", response.headers
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > self.maximum_response_bytes:
                    raise SourceFetchError(f"response exceeded {self.maximum_response_bytes} bytes")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks), response.headers

    @staticmethod
    def _metadata(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            value = orjson.loads(path.read_bytes())
            return value if isinstance(value, dict) else {}
        except (OSError, orjson.JSONDecodeError):
            return {}

    @staticmethod
    def _fallback(
        body_path: Path,
        metadata: dict[str, Any],
        error: str | None,
        *,
        stale: bool = True,
    ) -> SourcePayload:
        try:
            body = body_path.read_bytes()
        except OSError as exc:
            reason = error or str(exc)
            raise SourceFetchError(f"source unavailable and no cached body: {reason}") from exc
        fetched_at = metadata.get("fetched_at")
        if not isinstance(fetched_at, str):
            raise SourceFetchError("cached source has no valid fetched_at metadata")
        return SourcePayload(
            body=body,
            fetched_at=fetched_at,
            from_cache=True,
            stale=stale,
            error=error,
        )

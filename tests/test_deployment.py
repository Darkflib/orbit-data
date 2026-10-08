"""Static-hosting contract tests for the Caddy configuration.

The units that run it live in wwff-tech/gitops (quadlet/apps/orbit/), along with the copy of
this Caddyfile that hosts install; this one is what CI validates and smoke-tests.
"""

# pylint: disable=missing-function-docstring

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_caddyfile_serves_the_release_tree_the_site_and_the_status_documents() -> None:
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert "root * /srv/orbit-data/public" in caddyfile
    assert "root * /srv/orbit-data-site" in caddyfile
    assert "@site path / /site.css /favicon.svg" in caddyfile
    assert 'Access-Control-Allow-Origin "*"' in caddyfile
    assert "@status path /v1/status/*" in caddyfile

"""Google Antigravity's stable channel.

Each promoted release is announced by one write-once link file on
dl.google.com: `next/<from>/acp_next.json` names the version promoted after
`<from>` and its per-platform archives. Starting from the registry's current
version, follow links until a 404; the last version reached is the newest.

    {"version": "1.4.0",
     "platforms": {"darwin-aarch64": {"archive": "https://dl.google.com/...",
                                      "sha256": "...", "size": 111456962}, ...}}
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

from common import LatestRelease, ResolvedAsset, UpdateError
from registry_utils import version_tuple

AGENT_ID = "antigravity-acp"

# Overridable only so a fork can point at a test feed.
FEED_BASE = os.environ.get(
    "ANTIGRAVITY_FEED_BASE", "https://dl.google.com/agy-extensions/releases/acp/feed/v1"
)
ARCHIVE_PREFIXES = tuple(
    f"https://dl.google.com/agy-extensions/releases/{os_dir}/"
    for os_dir in ("macos", "linux", "windows")
)
PLATFORMS = frozenset(
    {
        "darwin-aarch64",
        "darwin-x86_64",
        "linux-x86_64",
        "linux-aarch64",
        "windows-x86_64",
        "windows-aarch64",
    }
)
MAX_HOPS = 50
_SHA256 = re.compile(r"[0-9a-f]{64}")


class _Transient(Exception):
    """dl.google.com is unreachable or erroring; try again next run."""


class _BadFeed(Exception):
    """The feed itself is wrong; a person needs to look."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could lead outside the allowlist, so surface 3xx as errors.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_request_count = 0


def _get(url: str) -> tuple[int, bytes]:
    """GET bypassing caches; returns (200, body) or (404, b"")."""
    global _request_count
    _request_count += 1
    busted = f"{url}?r={os.environ.get('GITHUB_RUN_ID', 'local')}-{_request_count}"
    req = urllib.request.Request(
        busted,
        headers={
            "User-Agent": "ACP-Registry-Version-Checker/1.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with _OPENER.open(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, b""
        if e.code >= 500:
            raise _Transient(f"HTTP {e.code} for {url}") from e
        raise _BadFeed(f"HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _Transient(f"{url}: {e}") from e


def _parse_link(from_version: str, body: bytes) -> tuple[str, dict[str, ResolvedAsset]]:
    try:
        data = json.loads(body)
        version = data["version"]
        platforms = data["platforms"]
        if version_tuple(version) <= version_tuple(from_version):
            raise _BadFeed(f"next/{from_version} names {version}, which is not newer")
    except (ValueError, KeyError, TypeError) as e:
        raise _BadFeed(f"next/{from_version}: malformed link: {e}") from e
    if not isinstance(platforms, dict) or set(platforms) != PLATFORMS:
        raise _BadFeed(f"next/{from_version}: expected platforms {sorted(PLATFORMS)}")
    assets = {}
    for platform, entry in platforms.items():
        archive = entry.get("archive") if isinstance(entry, dict) else None
        sha256 = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(archive, str) or not archive.startswith(ARCHIVE_PREFIXES):
            raise _BadFeed(f"next/{from_version}: {platform} archive outside allowlist")
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise _BadFeed(f"next/{from_version}: {platform} sha256 is not 64 hex chars")
        assets[platform] = ResolvedAsset(archive_url=archive, sha256=sha256)
    return version, assets


def get_stable_release(agent_data: dict) -> tuple[LatestRelease | None, UpdateError | None]:
    """Return the newest promoted release after the registry's version, if any."""
    version = agent_data.get("version", "")
    newest: tuple[str, dict[str, ResolvedAsset], str] | None = None
    try:
        for _ in range(MAX_HOPS):
            link_url = f"{FEED_BASE}/next/{version}/acp_next.json"
            status, body = _get(link_url)
            if status == 404:
                break
            version, assets = _parse_link(version, body)
            newest = (version, assets, link_url)
        else:
            raise _BadFeed(f"chain longer than {MAX_HOPS} links")
    except _Transient as e:
        # Raising here would block every agent's commit this run; skip instead.
        print(f"::warning::{AGENT_ID}: feed unavailable, skipping this run: {e}", file=sys.stderr)
        return None, None
    except _BadFeed as e:
        return None, UpdateError(AGENT_ID, f"feed: {e}")

    if newest is None:
        return None, None
    latest_version, assets, source_url = newest
    return LatestRelease(
        version=latest_version,
        distribution_type="binary",
        source_url=source_url,
        repository="",
        resolved_assets=assets,
    ), None

"""MCP server exposing the duplicacy-web API as tools.

The duplicacy-web API is the same internal HTTP API the web UI uses
(endpoints like ``/add_storage``, ``/save_repository``, ``/save_schedule``).
This server wraps those endpoints as MCP tools so AI clients can inspect and
administer Duplicacy backups.

Configuration (environment variables):
    DUPLICACY_WEB_URL   Base URL of duplicacy-web. Default: http://localhost:3875
    DUPLICACY_COMPUTER  Computer name as known to duplicacy-web. Default: localhost

Run: ``uv run duplicacy-mcp`` (stdio transport; never write to stdout).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from mcp.server import MCPServer

log = logging.getLogger("duplicacy_mcp")

WEB_URL = os.environ.get("DUPLICACY_WEB_URL", "http://localhost:3875").rstrip("/")
COMPUTER = os.environ.get("DUPLICACY_COMPUTER", "localhost")

mcp = MCPServer(
    "duplicacy-mcp",
    version="0.1.0",
    description="Inspect and administer Duplicacy backups via the duplicacy-web API.",
)


async def _post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON to a duplicacy-web endpoint; raise on API-level errors."""
    import httpx2

    async with httpx2.AsyncClient(base_url=WEB_URL, timeout=60.0) as client:
        response = await client.post(endpoint, json=payload)
        response.raise_for_status()
        data = response.json()
    message = data.get("message")
    if message not in (None, "ok"):
        raise RuntimeError(f"duplicacy-web {endpoint}: {message}")
    return data


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, default=str)


# --- Status ---------------------------------------------------------------


@mcp.tool()
async def get_schedule_status() -> str:
    """Return schedule status: each schedule's next run, pause state, and running jobs.

    Use this to see whether a backup is running or when the next one is due.
    """
    return _dump(await _post("/get_schedule_status", {}))


@mcp.tool()
async def get_backup_status() -> str:
    """Return per-repository backup status (last run outcome, timestamps, sizes)."""
    return _dump(await _post("/get_backup_status", {}))


# --- Storages -------------------------------------------------------------


@mcp.tool()
async def lookup_storage(name: str) -> str:
    """Look up a storage by name; returns whether it exists and its details.

    Args:
        name: Storage name as configured in duplicacy-web.
    """
    return _dump(await _post("/lookup_storage", {"Name": name}))


@mcp.tool()
async def create_storage(
    name: str,
    url: str,
    password: str,
    compression: str = "lz4",
    copy_from: str = "",
) -> str:
    """Create a new backup storage (destination).

    Args:
        name: Storage name, unique in duplicacy-web.
        url: Storage URL. Local path (e.g. /backups/plex), or scheme URLs
            like b2://bucket, s3://..., sftp://user@host/path.
        password: Encryption password for the repository. Keep it safe;
            all backups in this storage are encrypted with it.
        compression: Chunk compression. One of lz4, zstd_default,
            zstd_fastest, zstd_better, zstd_best. lz4 suits already-compressed
            media.
        copy_from: Name of an existing storage to copy chunk files from
            (dedupe seeding). Empty string for a fresh storage.
    """
    return _dump(
        await _post(
            "/add_storage",
            {
                "storage_url": url,
                "credentials": {"password": password},
                "storage_name": name,
                "copy_from": copy_from,
                "compression": compression,
                "erasure_coding": "",
                "rsa_key_file": "",
            },
        )
    )


@mcp.tool()
async def delete_storage(name: str) -> str:
    """Delete a storage configuration. Existing backups in it are NOT deleted.

    Args:
        name: Storage name to remove.
    """
    return _dump(await _post("/delete_storage", {"storage": name}))


# --- Repositories ---------------------------------------------------------


@mcp.tool()
async def list_repositories(storage: str) -> str:
    """List backup repository IDs that exist in a storage.

    Args:
        storage: Storage name.
    """
    return _dump(await _post("/list_repositories", {"storage": storage}))


@mcp.tool()
async def create_repository(id: str, path: str, storage: str) -> str:
    """Create a backup repository: one source directory backed up to one storage.

    Args:
        id: Repository ID, unique per computer (e.g. Plex-media).
        path: Source directory inside the duplicacy-web container (e.g. /source/plex).
        storage: Destination storage name.
    """
    return _dump(
        await _post(
            "/save_repository",
            {"index": -1, "id": id, "path": path, "storage": storage, "computer": COMPUTER},
        )
    )


@mcp.tool()
async def delete_repository(index: int) -> str:
    """Delete a repository configuration. Existing backups remain in storage.

    Args:
        index: Repository index (0-based, as shown by get_backup_status).
    """
    return _dump(await _post("/delete_repository", {"index": index, "computer": COMPUTER}))


@mcp.tool()
async def get_filters(index: int) -> str:
    """Return the include/exclude filter patterns of a repository.

    Args:
        index: Repository index (0-based).
    """
    return _dump(await _post("/get_patterns", {"computer": COMPUTER, "index": index}))


@mcp.tool()
async def set_filters(index: int, patterns: list[str]) -> str:
    """Set the include/exclude filter patterns of a repository.

    Patterns are duplicacy filter rules, one per entry, first match wins:
    ``+dir/*`` includes, ``-*`` excludes everything else. E.g.
    ["+movies/*", "+tv/*", "-*"] backs up only movies and tv.

    Args:
        index: Repository index (0-based).
        patterns: Filter rules in order.
    """
    return _dump(
        await _post("/save_patterns", {"computer": COMPUTER, "index": index, "patterns": patterns})
    )


# --- Schedules and jobs ---------------------------------------------------


@mcp.tool()
async def create_schedule(
    name: str,
    start_time: str,
    frequency: int = 86400,
    days: str = "1111111",
    max_time: str = "00:00",
) -> str:
    """Create a backup schedule (container for one or more jobs).

    Args:
        name: Schedule name, unique in duplicacy-web.
        start_time: Daily start time as HH:MMAM/PM, e.g. "02:00AM" or "10:35AM".
        frequency: Interval in seconds (86400 = daily).
        days: 7-char bitmask of active weekdays, built Sunday-first,
            "1111111" = every day, "0000000" = never (disabled).
        max_time: Latest time of day the schedule may start, HH:MMAM/PM.
            "00:00" = no limit.
    """
    return _dump(
        await _post(
            "/save_schedule",
            {
                "scheduleIndex": -1,
                "name": name,
                "startTime": start_time,
                "maxTime": max_time,
                "frequency": frequency,
                "days": days,
            },
        )
    )


@mcp.tool()
async def delete_schedule(schedule_index: int) -> str:
    """Delete a schedule and its jobs.

    Args:
        schedule_index: Schedule index (0-based, as shown by get_schedule_status).
    """
    return _dump(await _post("/delete_schedule", {"scheduleIndex": schedule_index}))


@mcp.tool()
async def add_job(
    schedule_index: int,
    type: str,
    repository: int,
    destination: str,
    source: str = "",
    options: str = "",
    parallel: bool = False,
) -> str:
    """Add a job (backup, prune, or check) to a schedule.

    Args:
        schedule_index: Schedule index (0-based).
        type: Job type: "backup", "prune", or "check".
        repository: Repository index (0-based).
        destination: Storage name the job writes to (or reads from for
            prune/check).
        source: Source path for backup jobs (e.g. /source/plex); empty for
            prune/check.
        options: Extra duplicacy CLI options. Backup example: "-threads 4".
            Prune example: "-keep 0:30 -a" (keep the 30 newest snapshots).
            Check example: "-fossils".
        parallel: Whether the job may run in parallel with others.
    """
    return _dump(
        await _post(
            "/add_job",
            {
                "scheduleIndex": schedule_index,
                "type": type,
                "computer": COMPUTER,
                "repository": repository,
                "source": source,
                "destination": destination,
                "options": options,
                "parallel": parallel,
            },
        )
    )


@mcp.tool()
async def delete_job(schedule_index: int, job_index: int) -> str:
    """Remove a job from a schedule.

    Args:
        schedule_index: Schedule index (0-based).
        job_index: Job index within the schedule (0-based).
    """
    return _dump(
        await _post("/delete_job", {"scheduleIndex": schedule_index, "jobIndex": job_index})
    )


@mcp.tool()
async def start_stop_schedule(schedule_index: int, is_start: bool) -> str:
    """Start (enable) or stop (disable) a schedule.

    Args:
        schedule_index: Schedule index (0-based).
        is_start: True to enable the schedule, False to disable it.
    """
    return _dump(
        await _post("/start_stop_schedule", {"scheduleIndex": schedule_index, "isStart": is_start})
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    log.info("duplicacy-mcp %s -> %s (computer %s)", "0.1.0", WEB_URL, COMPUTER)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

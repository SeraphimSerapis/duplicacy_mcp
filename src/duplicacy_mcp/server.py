"""MCP server exposing the duplicacy-web API as structured tools.

The duplicacy-web API is the internal HTTP JSON API the web UI uses
(endpoints like ``/add_storage``, ``/save_repository``, ``/save_schedule``).
This server wraps those endpoints as MCP tools so AI clients can inspect and
administer Duplicacy backups.

Configuration (environment variables):
    DUPLICACY_WEB_URL   Base URL of duplicacy-web. Default: http://localhost:3875
    DUPLICACY_COMPUTER  Computer name as known to duplicacy-web. Default: localhost
    DUPLICACY_TIMEOUT   HTTP read timeout in seconds. Default: 60
    DUPLICACY_LOG_LEVEL Logging level (DEBUG..CRITICAL). Default: WARNING

Run: ``uv run duplicacy-mcp`` (stdio transport; never write to stdout).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from duplicacy_mcp import __version__
from duplicacy_mcp.client import DuplicacyClient, Settings

log = logging.getLogger("duplicacy_mcp")

# Module-level aliases so MCPServer's inspect.signature(..., eval_str=True) resolves them.
AppContext = Context[DuplicacyClient, Any]
Compression = Literal["lz4", "zstd_default", "zstd_fastest", "zstd_better", "zstd_best"]
JobType = Literal["backup", "prune", "check"]
NonEmptyStr = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
WeekdayMask = Annotated[str, Field(pattern=r"^[01]{7}$")]

SERVER_INSTRUCTIONS = (
    "Inspect get_backup_status or get_schedule_status before using 0-based repository, "
    "schedule, or job indices; deleting an item shifts subsequent indices. Filter patterns "
    "are evaluated first-match-wins ('+' includes, '-' excludes). Storage passwords are "
    "encryption keys and cannot be recovered by duplicacy-web; never log or discard them."
)

_READ_ONLY_LOCAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_READ_ONLY_REMOTE = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
_CREATE_LOCAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_CREATE_REMOTE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
_UPDATE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_REPLACE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)
_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)


def _client(ctx: AppContext) -> DuplicacyClient:
    return ctx.request_context.lifespan_context


def create_server(
    settings: Settings | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> MCPServer[DuplicacyClient]:
    """Build and configure an MCPServer bound to a DuplicacyClient lifespan."""
    resolved = settings if settings is not None else Settings.from_env()

    @asynccontextmanager
    async def _lifespan(_server: MCPServer[DuplicacyClient]) -> AsyncIterator[DuplicacyClient]:
        async with DuplicacyClient(resolved, transport=transport) as client:
            yield client

    server: MCPServer[DuplicacyClient] = MCPServer(
        "duplicacy-mcp",
        title="Duplicacy Web",
        version=__version__,
        description="Inspect and administer Duplicacy backups via the duplicacy-web API.",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=_lifespan,
        log_level=resolved.log_level,
    )

    # --- Status ---------------------------------------------------------------

    @server.tool(annotations=_READ_ONLY_LOCAL)
    async def get_schedule_status(ctx: AppContext) -> dict[str, Any]:
        """Return schedule status: each schedule's next run, pause state, and running jobs.

        Use this to see whether a backup is running or when the next one is due.
        """
        return await _client(ctx).post("/get_schedule_status", {})

    @server.tool(annotations=_READ_ONLY_LOCAL)
    async def get_backup_status(ctx: AppContext) -> dict[str, Any]:
        """Return per-repository backup status (last run outcome, timestamps, sizes)."""
        return await _client(ctx).post("/get_backup_status", {})

    # --- Storages -------------------------------------------------------------

    @server.tool(annotations=_READ_ONLY_LOCAL)
    async def lookup_storage(name: NonEmptyStr, ctx: AppContext) -> dict[str, Any]:
        """Look up a storage by name; returns whether it exists and its details.

        Args:
            name: Storage name as configured in duplicacy-web.
        """
        return await _client(ctx).post("/lookup_storage", {"Name": name})

    @server.tool(annotations=_CREATE_REMOTE)
    async def create_storage(
        name: NonEmptyStr,
        url: NonEmptyStr,
        password: NonEmptyStr,
        compression: Compression = "lz4",
        copy_from: str = "",
        *,
        ctx: AppContext,
    ) -> dict[str, Any]:
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
        return await _client(ctx).post(
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

    @server.tool(annotations=_DESTRUCTIVE)
    async def delete_storage(name: NonEmptyStr, ctx: AppContext) -> dict[str, Any]:
        """Delete a storage configuration. Existing backups in it are NOT deleted.

        Args:
            name: Storage name to remove.
        """
        return await _client(ctx).post("/delete_storage", {"storage": name})

    # --- Repositories ---------------------------------------------------------

    @server.tool(annotations=_READ_ONLY_REMOTE)
    async def list_repositories(storage: NonEmptyStr, ctx: AppContext) -> dict[str, Any]:
        """List backup repository IDs that exist in a storage.

        Args:
            storage: Storage name.
        """
        return await _client(ctx).post("/list_repositories", {"storage": storage})

    @server.tool(annotations=_CREATE_LOCAL)
    async def create_repository(
        id: NonEmptyStr,
        path: NonEmptyStr,
        storage: NonEmptyStr,
        ctx: AppContext,
    ) -> dict[str, Any]:
        """Create a backup repository: one source directory backed up to one storage.

        Args:
            id: Repository ID, unique per computer (e.g. Plex-media).
            path: Source directory inside the duplicacy-web container (e.g. /source/plex).
            storage: Destination storage name.
        """
        client = _client(ctx)
        return await client.post(
            "/save_repository",
            {"index": -1, "id": id, "path": path, "storage": storage, "computer": client.computer},
        )

    @server.tool(annotations=_DESTRUCTIVE)
    async def delete_repository(index: NonNegativeInt, ctx: AppContext) -> dict[str, Any]:
        """Delete a repository configuration. Existing backups remain in storage.

        Args:
            index: Repository index (0-based, as shown by get_backup_status).
        """
        client = _client(ctx)
        return await client.post(
            "/delete_repository",
            {"index": index, "computer": client.computer},
        )

    @server.tool(annotations=_READ_ONLY_LOCAL)
    async def get_filters(index: NonNegativeInt, ctx: AppContext) -> dict[str, Any]:
        """Return the include/exclude filter patterns of a repository.

        Args:
            index: Repository index (0-based).
        """
        client = _client(ctx)
        return await client.post("/get_patterns", {"computer": client.computer, "index": index})

    @server.tool(annotations=_REPLACE_IDEMPOTENT)
    async def set_filters(
        index: NonNegativeInt,
        patterns: list[str],
        ctx: AppContext,
    ) -> dict[str, Any]:
        """Set the include/exclude filter patterns of a repository.

        Patterns are duplicacy filter rules, one per entry, first match wins:
        ``+dir/*`` includes, ``-*`` excludes everything else. E.g.
        ["+movies/*", "+tv/*", "-*"] backs up only movies and tv.

        Args:
            index: Repository index (0-based).
            patterns: Filter rules in order.
        """
        client = _client(ctx)
        return await client.post(
            "/save_patterns",
            {"computer": client.computer, "index": index, "patterns": patterns},
        )

    # --- Schedules and jobs ---------------------------------------------------

    @server.tool(annotations=_CREATE_LOCAL)
    async def create_schedule(
        name: NonEmptyStr,
        start_time: NonEmptyStr,
        frequency: PositiveInt = 86400,
        days: WeekdayMask = "1111111",
        max_time: NonEmptyStr = "00:00",
        *,
        ctx: AppContext,
    ) -> dict[str, Any]:
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
        return await _client(ctx).post(
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

    @server.tool(annotations=_DESTRUCTIVE)
    async def delete_schedule(schedule_index: NonNegativeInt, ctx: AppContext) -> dict[str, Any]:
        """Delete a schedule and its jobs.

        Args:
            schedule_index: Schedule index (0-based, as shown by get_schedule_status).
        """
        return await _client(ctx).post("/delete_schedule", {"scheduleIndex": schedule_index})

    @server.tool(annotations=_CREATE_LOCAL)
    async def add_job(
        schedule_index: NonNegativeInt,
        type: JobType,
        repository: NonNegativeInt,
        destination: NonEmptyStr,
        source: str = "",
        options: str = "",
        parallel: bool = False,
        *,
        ctx: AppContext,
    ) -> dict[str, Any]:
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
        if type == "backup" and not source.strip():
            raise ValueError("source path is required for backup jobs")
        client = _client(ctx)
        return await client.post(
            "/add_job",
            {
                "scheduleIndex": schedule_index,
                "type": type,
                "computer": client.computer,
                "repository": repository,
                "source": source,
                "destination": destination,
                "options": options,
                "parallel": parallel,
            },
        )

    @server.tool(annotations=_DESTRUCTIVE)
    async def delete_job(
        schedule_index: NonNegativeInt,
        job_index: NonNegativeInt,
        ctx: AppContext,
    ) -> dict[str, Any]:
        """Remove a job from a schedule.

        Args:
            schedule_index: Schedule index (0-based).
            job_index: Job index within the schedule (0-based).
        """
        return await _client(ctx).post(
            "/delete_job",
            {"scheduleIndex": schedule_index, "jobIndex": job_index},
        )

    @server.tool(annotations=_UPDATE_IDEMPOTENT)
    async def start_stop_schedule(
        schedule_index: NonNegativeInt,
        is_start: bool,
        ctx: AppContext,
    ) -> dict[str, Any]:
        """Start (enable) or stop (disable) a schedule.

        Args:
            schedule_index: Schedule index (0-based).
            is_start: True to enable the schedule, False to disable it.
        """
        return await _client(ctx).post(
            "/start_stop_schedule",
            {"scheduleIndex": schedule_index, "isStart": is_start},
        )

    return server


_default_server: MCPServer[DuplicacyClient] | None = None


def __getattr__(name: str) -> Any:
    global _default_server
    if name == "mcp":
        if _default_server is None:
            _default_server = create_server()
        return _default_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    """Validate configuration from the environment and run the stdio MCP server."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    server = create_server(settings)
    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    log.info(
        "duplicacy-mcp %s -> %s (computer %s)",
        __version__,
        settings.safe_base_url,
        settings.computer,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

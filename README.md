# duplicacy-mcp

MCP server for administering [Duplicacy](https://duplicacy.com) backups through
the duplicacy-web API (the same internal HTTP API the web UI uses). Lets AI
clients inspect storages, repositories, schedules and jobs, and create or
delete them.

Built with the official Python MCP SDK 2.x (`MCPServer`, stdio transport).

## Tools

| Tool | Purpose |
| --- | --- |
| `get_schedule_status` | Schedules, next runs, running jobs |
| `get_backup_status` | Per-repository last-run status |
| `lookup_storage` | Storage existence + details |
| `create_storage` | New destination (local path or `b2://`, `s3://`, …) with encryption password |
| `delete_storage` | Remove a storage config (backups kept) |
| `list_repositories` | Repository IDs in a storage |
| `create_repository` | New source → storage pair |
| `delete_repository` | Remove a repository config (backups kept) |
| `get_filters` / `set_filters` | Repository include/exclude patterns |
| `create_schedule` / `delete_schedule` | Daily/weekly schedule containers |
| `add_job` / `delete_job` | backup / prune / check jobs within a schedule |
| `start_stop_schedule` | Enable / disable a schedule |

## Setup

```bash
uv sync
uv run duplicacy-mcp
```

Configuration via environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DUPLICACY_WEB_URL` | `http://localhost:3875` | duplicacy-web base URL |
| `DUPLICACY_COMPUTER` | `localhost` | Computer name in duplicacy-web |

## Client configuration

```json
{
  "mcpServers": {
    "duplicacy": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/duplicacy-mcp", "run", "duplicacy-mcp"]
    }
  }
}
```

## Notes

* The duplicacy-web API is unauthenticated unless an administration token is
  set in the web UI. Keep the server on a trusted network.
* Storage passwords are the repository encryption keys — callers must supply
  them when creating a storage and store them somewhere safe (e.g. Infisical).

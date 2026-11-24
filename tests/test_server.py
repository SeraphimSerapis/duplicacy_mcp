from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from mcp.client import Client

from duplicacy_mcp.client import DuplicacyClient, Settings
from duplicacy_mcp.server import AppContext, create_server, main

EXPECTED_TOOLS = {
    "get_schedule_status",
    "get_backup_status",
    "lookup_storage",
    "create_storage",
    "delete_storage",
    "list_repositories",
    "create_repository",
    "delete_repository",
    "get_filters",
    "set_filters",
    "create_schedule",
    "delete_schedule",
    "add_job",
    "delete_job",
    "start_stop_schedule",
}


def _recording_transport(
    calls: list[tuple[str, dict[str, Any]]],
    response_body: dict[str, Any] | None = None,
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, payload))
        return httpx2.Response(
            200, json=response_body or {"message": "ok", "endpoint": request.url.path}
        )

    return httpx2.MockTransport(handler)


@pytest.mark.anyio
async def test_list_tools_schemas_and_annotations() -> None:
    server = create_server(Settings(), transport=_recording_transport([]))
    async with Client(server) as client:
        listed = await client.list_tools()

    by_name = {tool.name: tool for tool in listed.tools}
    assert set(by_name) == EXPECTED_TOOLS

    for tool in listed.tools:
        assert tool.output_schema is not None
        assert tool.output_schema.get("type") == "object"
        assert "result" not in tool.output_schema.get("properties", {})
        assert tool.annotations is not None

    assert by_name["get_schedule_status"].annotations.read_only_hint is True
    assert by_name["get_schedule_status"].annotations.destructive_hint is False
    assert by_name["list_repositories"].annotations.open_world_hint is True
    assert by_name["create_storage"].annotations.destructive_hint is False
    assert by_name["create_storage"].annotations.open_world_hint is True
    assert by_name["set_filters"].annotations.idempotent_hint is True
    assert by_name["set_filters"].annotations.destructive_hint is True
    assert by_name["start_stop_schedule"].annotations.idempotent_hint is True
    assert by_name["start_stop_schedule"].annotations.destructive_hint is False
    assert by_name["delete_storage"].annotations.destructive_hint is True
    assert by_name["delete_storage"].annotations.idempotent_hint is False

    create_storage_props = by_name["create_storage"].input_schema["properties"]
    assert create_storage_props["compression"]["enum"] == [
        "lz4",
        "zstd_default",
        "zstd_fastest",
        "zstd_better",
        "zstd_best",
    ]
    add_job_props = by_name["add_job"].input_schema["properties"]
    assert add_job_props["type"]["enum"] == ["backup", "prune", "check"]
    assert add_job_props["repository"]["minimum"] == 0
    schedule_props = by_name["create_schedule"].input_schema["properties"]
    assert schedule_props["days"]["pattern"] == r"^[01]{7}$"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "args", "expected_path", "expected_payload"),
    [
        ("get_schedule_status", {}, "/get_schedule_status", {}),
        ("get_backup_status", {}, "/get_backup_status", {}),
        ("lookup_storage", {"name": "nas"}, "/lookup_storage", {"Name": "nas"}),
        (
            "create_storage",
            {
                "name": "nas",
                "url": "/backups/nas",
                "password": "pw",
                "compression": "zstd_default",
                "copy_from": "seed",
            },
            "/add_storage",
            {
                "storage_url": "/backups/nas",
                "credentials": {"password": "pw"},
                "storage_name": "nas",
                "copy_from": "seed",
                "compression": "zstd_default",
                "erasure_coding": "",
                "rsa_key_file": "",
            },
        ),
        ("delete_storage", {"name": "nas"}, "/delete_storage", {"storage": "nas"}),
        ("list_repositories", {"storage": "nas"}, "/list_repositories", {"storage": "nas"}),
        (
            "create_repository",
            {"id": "media", "path": "/source/media", "storage": "nas"},
            "/save_repository",
            {
                "index": -1,
                "id": "media",
                "path": "/source/media",
                "storage": "nas",
                "computer": "test-host",
            },
        ),
        (
            "delete_repository",
            {"index": 2},
            "/delete_repository",
            {"index": 2, "computer": "test-host"},
        ),
        ("get_filters", {"index": 1}, "/get_patterns", {"computer": "test-host", "index": 1}),
        (
            "set_filters",
            {"index": 1, "patterns": ["+movies/*", "-*"]},
            "/save_patterns",
            {"computer": "test-host", "index": 1, "patterns": ["+movies/*", "-*"]},
        ),
        (
            "create_schedule",
            {
                "name": "nightly",
                "start_time": "02:00AM",
                "frequency": 86400,
                "days": "1010101",
                "max_time": "05:00AM",
            },
            "/save_schedule",
            {
                "scheduleIndex": -1,
                "name": "nightly",
                "startTime": "02:00AM",
                "maxTime": "05:00AM",
                "frequency": 86400,
                "days": "1010101",
            },
        ),
        ("delete_schedule", {"schedule_index": 0}, "/delete_schedule", {"scheduleIndex": 0}),
        (
            "add_job",
            {
                "schedule_index": 0,
                "type": "backup",
                "repository": 1,
                "destination": "nas",
                "source": "/source/media",
                "options": "-threads 4",
                "parallel": True,
            },
            "/add_job",
            {
                "scheduleIndex": 0,
                "type": "backup",
                "computer": "test-host",
                "repository": 1,
                "source": "/source/media",
                "destination": "nas",
                "options": "-threads 4",
                "parallel": True,
            },
        ),
        (
            "delete_job",
            {"schedule_index": 0, "job_index": 1},
            "/delete_job",
            {"scheduleIndex": 0, "jobIndex": 1},
        ),
        (
            "start_stop_schedule",
            {"schedule_index": 0, "is_start": False},
            "/start_stop_schedule",
            {"scheduleIndex": 0, "isStart": False},
        ),
    ],
)
async def test_all_tools_wire_contract(
    tool_name: str,
    args: dict[str, Any],
    expected_path: str,
    expected_payload: dict[str, Any],
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    server = create_server(Settings(computer="test-host"), transport=_recording_transport(calls))

    async with Client(server) as client:
        result = await client.call_tool(tool_name, args)

    assert not result.is_error
    assert calls == [(expected_path, expected_payload)]
    assert result.structured_content == {"message": "ok", "endpoint": expected_path}
    assert json.loads(result.content[0].text) == result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "bad_args"),
    [
        (
            "create_storage",
            {"name": "nas", "url": "/backups", "password": "pw", "compression": "gzip"},
        ),
        (
            "add_job",
            {"schedule_index": 0, "type": "sync", "repository": 0, "destination": "nas"},
        ),
        (
            "add_job",
            {"schedule_index": 0, "type": "backup", "repository": 0, "destination": "nas"},
        ),
        ("delete_repository", {"index": -1}),
        (
            "create_schedule",
            {"name": "nightly", "start_time": "02:00AM", "days": "10101"},
        ),
    ],
)
async def test_invalid_inputs_fail_without_http_calls(
    tool_name: str,
    bad_args: dict[str, Any],
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    server = create_server(Settings(), transport=_recording_transport(calls))

    async with Client(server) as client:
        result = await client.call_tool(tool_name, bad_args)

    assert result.is_error
    assert calls == []


@pytest.mark.anyio
async def test_lifespan_shares_and_closes_client() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    seen_clients: list[DuplicacyClient] = []
    server = create_server(Settings(), transport=_recording_transport(calls))

    @server.tool()
    async def inspect_client(ctx: AppContext) -> dict[str, Any]:
        c = ctx.request_context.lifespan_context
        seen_clients.append(c)
        return {"open": c.is_open}

    async with Client(server) as client:
        await client.call_tool("get_schedule_status", {})
        r1 = await client.call_tool("inspect_client", {})
        r2 = await client.call_tool("inspect_client", {})
        assert r1.structured_content == {"open": True}
        assert r2.structured_content == {"open": True}
        assert len(calls) == 1
        assert len(seen_clients) == 2
        assert seen_clients[0] is seen_clients[1]

    assert not seen_clients[0].is_open


def test_main_exits_cleanly_on_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DUPLICACY_WEB_URL", "not-a-url")
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "DUPLICACY_WEB_URL" in captured.err

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from duplicacy_mcp.client import DuplicacyClient, DuplicacyError, Settings


class _ChunkedStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def test_settings_defaults_and_normalization() -> None:
    defaults = Settings.from_env({})
    assert defaults == Settings(
        web_url="http://localhost:3875",
        computer="localhost",
        timeout=60.0,
        log_level="WARNING",
    )

    custom = Settings.from_env(
        {
            "DUPLICACY_WEB_URL": " https://user:secret@backup.local:8443/api/ ",
            "DUPLICACY_COMPUTER": " nas-01 ",
            "DUPLICACY_TIMEOUT": "15.5",
            "DUPLICACY_LOG_LEVEL": "debug",
        }
    )
    assert custom.web_url == "https://user:secret@backup.local:8443/api"
    assert custom.safe_base_url == "https://backup.local:8443/api"
    assert custom.computer == "nas-01"
    assert custom.timeout == 15.5
    assert custom.log_level == "DEBUG"

    ipv6 = Settings(web_url="http://user:pw@[::1]:3875/")
    assert ipv6.safe_base_url == "http://[::1]:3875"


@pytest.mark.parametrize(
    "env",
    [
        {"DUPLICACY_WEB_URL": ""},
        {"DUPLICACY_WEB_URL": "ftp://localhost:3875"},
        {"DUPLICACY_WEB_URL": "http://"},
        {"DUPLICACY_WEB_URL": "http://localhost:99999"},
        {"DUPLICACY_WEB_URL": "http://localhost:3875?x=1"},
        {"DUPLICACY_WEB_URL": "http://localhost:3875#frag"},
        {"DUPLICACY_COMPUTER": "   "},
        {"DUPLICACY_TIMEOUT": "0"},
        {"DUPLICACY_TIMEOUT": "-5"},
        {"DUPLICACY_TIMEOUT": "nan"},
        {"DUPLICACY_TIMEOUT": "inf"},
        {"DUPLICACY_TIMEOUT": "fast"},
        {"DUPLICACY_LOG_LEVEL": "TRACE"},
    ],
)
def test_settings_rejects_invalid_env(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Settings.from_env(env)


def test_settings_rejects_bool_timeout() -> None:
    with pytest.raises(ValueError, match="DUPLICACY_TIMEOUT"):
        Settings(timeout=True)  # type: ignore[arg-type]
    assert math.isfinite(Settings().timeout)


@pytest.mark.anyio
async def test_client_lifecycle_and_connection_reuse() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_paths.append(request.url.path)
        return httpx2.Response(200, json={"message": "ok", "path": request.url.path})

    client = DuplicacyClient(Settings(), transport=httpx2.MockTransport(handler))
    with pytest.raises(RuntimeError, match="async with"):
        await client.post("/get_schedule_status", {})

    async with client:
        assert client.is_open
        underlying = client._client
        r1 = await client.post("/get_schedule_status", {})
        r2 = await client.post("/get_backup_status", {})
        assert client._client is underlying
        assert r1["path"] == "/get_schedule_status"
        assert r2["path"] == "/get_backup_status"

    assert not client.is_open
    assert seen_paths == ["/get_schedule_status", "/get_backup_status"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload", [{"status": 1}, {"message": None}, {"message": ""}, {"message": "OK"}]
)
async def test_client_accepts_success_messages(payload: dict[str, Any]) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=payload)

    async with DuplicacyClient(Settings(), transport=httpx2.MockTransport(handler)) as client:
        assert await client.post("/get_backup_status", {}) == payload


@pytest.mark.anyio
async def test_client_error_cases() -> None:
    def make_client(
        handler: Any,
        *,
        max_response_bytes: int = 1024,
        url: str = "http://user:topsecret@localhost:3875",
    ) -> DuplicacyClient:
        return DuplicacyClient(
            Settings(web_url=url, timeout=2.0),
            transport=httpx2.MockTransport(handler),
            max_response_bytes=max_response_bytes,
        )

    # API-level error message
    async with make_client(
        lambda _r: httpx2.Response(200, json={"message": "storage already exists"})
    ) as c:
        with pytest.raises(DuplicacyError, match="storage already exists"):
            await c.post("/add_storage", {})

    # 302 redirect must not be followed
    seen: list[str] = []

    def redirect_handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(302, headers={"Location": "http://evil.example/login"})

    async with make_client(redirect_handler) as c:
        with pytest.raises(DuplicacyError, match="HTTP 302 redirect"):
            await c.post("/get_schedule_status", {})
    assert len(seen) == 1

    # 401 / 403 auth error
    async with make_client(lambda _r: httpx2.Response(401, text="Unauthorized")) as c:
        with pytest.raises(DuplicacyError, match="HTTP 401"):
            await c.post("/get_schedule_status", {})

    # Non-JSON HTML response
    async with make_client(
        lambda _r: httpx2.Response(
            200, headers={"content-type": "text/html"}, text="<html>login</html>"
        )
    ) as c:
        with pytest.raises(DuplicacyError, match="non-JSON"):
            await c.post("/get_schedule_status", {})

    # Non-dict JSON response
    async with make_client(lambda _r: httpx2.Response(200, json=["unexpected"])) as c:
        with pytest.raises(DuplicacyError, match="unexpected JSON type list"):
            await c.post("/get_schedule_status", {})

    # Oversized Content-Length header
    async with make_client(
        lambda _r: httpx2.Response(200, headers={"content-length": "5000"}, json={"ok": True}),
        max_response_bytes=256,
    ) as c:
        with pytest.raises(DuplicacyError, match="exceeded 256 bytes"):
            await c.post("/get_backup_status", {})

    # Oversized streamed body without Content-Length
    async with make_client(
        lambda _r: httpx2.Response(200, stream=_ChunkedStream([b"x" * 150, b"y" * 150])),
        max_response_bytes=256,
    ) as c:
        with pytest.raises(DuplicacyError, match="exceeded 256 bytes"):
            await c.post("/get_backup_status", {})


@pytest.mark.anyio
async def test_client_never_leaks_secrets_in_errors() -> None:
    secret_password = "super-secret-repo-encryption-key"
    url_secret = "url-embedded-secret"

    def raise_timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout(f"timeout reaching {request.url}", request=request)

    def raise_connect(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"failed {request.url}", request=request)

    handlers: list[Any] = [
        lambda _r: httpx2.Response(500, text=json.dumps({"echo": "err"})),
        raise_timeout,
        raise_connect,
        lambda _r: httpx2.Response(200, content=b"not-json"),
    ]

    for handler in handlers:
        async with DuplicacyClient(
            Settings(web_url=f"http://admin:{url_secret}@localhost:3875"),
            transport=httpx2.MockTransport(handler),
        ) as client:
            with pytest.raises(DuplicacyError) as exc_info:
                await client.post("/add_storage", {"credentials": {"password": secret_password}})
        msg = str(exc_info.value)
        assert secret_password not in msg
        assert url_secret not in msg

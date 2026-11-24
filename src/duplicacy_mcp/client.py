"""HTTP client and validated configuration for the duplicacy-web API."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, cast, get_args
from urllib.parse import urlsplit, urlunsplit

import httpx2

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
VALID_LOG_LEVELS = frozenset(get_args(LogLevel))

DEFAULT_WEB_URL = "http://localhost:3875"
DEFAULT_COMPUTER = "localhost"
DEFAULT_TIMEOUT = 60.0
DEFAULT_LOG_LEVEL: LogLevel = "WARNING"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_LIMITS = httpx2.Limits(
    max_connections=10,
    max_keepalive_connections=4,
    keepalive_expiry=30.0,
)


class DuplicacyError(RuntimeError):
    """Raised when duplicacy-web returns an error or cannot be reached safely."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings for duplicacy-mcp."""

    web_url: str = DEFAULT_WEB_URL
    computer: str = DEFAULT_COMPUTER
    timeout: float = DEFAULT_TIMEOUT
    log_level: LogLevel = DEFAULT_LOG_LEVEL

    def __post_init__(self) -> None:
        normalized_url = self.web_url.strip().rstrip("/")
        parts = urlsplit(normalized_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or not parts.hostname:
            raise ValueError(
                "DUPLICACY_WEB_URL must be a valid http:// or https:// URL with a host"
            )
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError("DUPLICACY_WEB_URL contains an invalid port") from exc
        if parts.query or parts.fragment:
            raise ValueError("DUPLICACY_WEB_URL must not include a query string or fragment")
        object.__setattr__(self, "web_url", normalized_url)

        normalized_computer = self.computer.strip()
        if not normalized_computer:
            raise ValueError("DUPLICACY_COMPUTER must not be empty")
        object.__setattr__(self, "computer", normalized_computer)

        if isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("DUPLICACY_TIMEOUT must be a positive finite number")

        normalized_level = self.log_level.strip().upper()
        if normalized_level not in VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ValueError(f"DUPLICACY_LOG_LEVEL must be one of: {allowed}")
        object.__setattr__(self, "log_level", cast(LogLevel, normalized_level))

    @property
    def safe_base_url(self) -> str:
        """Base URL with any embedded userinfo credentials removed for safe logging."""
        parts = urlsplit(self.web_url)
        hostname = parts.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        safe_netloc = f"{hostname}:{parts.port}" if parts.port is not None else hostname
        return urlunsplit((parts.scheme, safe_netloc, parts.path, "", ""))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Load and validate settings from environment variables."""
        source = os.environ if env is None else env
        web_url = source.get("DUPLICACY_WEB_URL", DEFAULT_WEB_URL)
        computer = source.get("DUPLICACY_COMPUTER", DEFAULT_COMPUTER)

        raw_timeout = source.get("DUPLICACY_TIMEOUT")
        if raw_timeout is None:
            timeout = DEFAULT_TIMEOUT
        else:
            try:
                timeout = float(raw_timeout)
            except ValueError as exc:
                raise ValueError(
                    f"DUPLICACY_TIMEOUT must be a positive finite number, got {raw_timeout!r}"
                ) from exc

        raw_level = source.get("DUPLICACY_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
        if raw_level not in VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ValueError(f"DUPLICACY_LOG_LEVEL must be one of: {allowed}")

        return cls(
            web_url=web_url,
            computer=computer,
            timeout=timeout,
            log_level=cast(LogLevel, raw_level),
        )


class DuplicacyClient:
    """Async HTTP client managing a pooled connection to duplicacy-web."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._settings = settings
        self._transport = transport
        self._max_response_bytes = max_response_bytes
        self._client: httpx2.AsyncClient | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def computer(self) -> str:
        return self._settings.computer

    @property
    def safe_base_url(self) -> str:
        return self._settings.safe_base_url

    @property
    def is_open(self) -> bool:
        return self._client is not None and not self._client.is_closed

    async def __aenter__(self) -> DuplicacyClient:
        if self._client is not None and not self._client.is_closed:
            return self
        client_kwargs: dict[str, Any] = {
            "base_url": self._settings.web_url,
            "timeout": httpx2.Timeout(
                connect=5.0,
                read=self._settings.timeout,
                write=10.0,
                pool=5.0,
            ),
            "limits": DEFAULT_LIMITS,
            "follow_redirects": False,
            "headers": {"Accept": "application/json"},
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        self._client = httpx2.AsyncClient(**client_kwargs)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to a duplicacy-web endpoint and validate the JSON response."""
        client = self._client
        if client is None or client.is_closed:
            raise RuntimeError("DuplicacyClient must be entered with 'async with' before use")

        buf = bytearray()
        status = 0
        content_type = "unknown"

        try:
            async with client.stream("POST", endpoint, json=payload) as response:
                status = response.status_code
                content_type = response.headers.get("content-type", "unknown")

                if 300 <= status < 400:
                    raise DuplicacyError(
                        f"duplicacy-web {endpoint} returned HTTP {status} redirect; "
                        "admin authentication may be enabled"
                    )
                if status in (401, 403):
                    raise DuplicacyError(
                        f"duplicacy-web {endpoint} returned HTTP {status}; "
                        "authentication required or denied"
                    )
                if status >= 400:
                    raise DuplicacyError(f"duplicacy-web {endpoint} returned HTTP {status}")

                raw_length = response.headers.get("content-length")
                if raw_length is not None:
                    try:
                        if int(raw_length) > self._max_response_bytes:
                            raise DuplicacyError(
                                f"duplicacy-web {endpoint} response exceeded "
                                f"{self._max_response_bytes} bytes"
                            )
                    except ValueError:
                        pass

                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self._max_response_bytes:
                        raise DuplicacyError(
                            f"duplicacy-web {endpoint} response exceeded "
                            f"{self._max_response_bytes} bytes"
                        )
        except httpx2.TimeoutException as exc:
            raise DuplicacyError(
                f"duplicacy-web {endpoint} timed out after {self._settings.timeout:g}s"
            ) from exc
        except httpx2.HTTPError as exc:
            raise DuplicacyError(
                f"duplicacy-web {endpoint} request failed ({type(exc).__name__})"
            ) from exc

        try:
            data = json.loads(buf.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DuplicacyError(
                f"duplicacy-web {endpoint} returned non-JSON response "
                f"(HTTP {status}, content-type: {content_type})"
            ) from exc

        if not isinstance(data, dict):
            raise DuplicacyError(
                f"duplicacy-web {endpoint} returned unexpected JSON type {type(data).__name__}"
            )

        message = data.get("message")
        if message is not None and (
            not isinstance(message, str) or message.strip().lower() not in ("", "ok")
        ):
            raise DuplicacyError(f"duplicacy-web {endpoint}: {message}")

        return data

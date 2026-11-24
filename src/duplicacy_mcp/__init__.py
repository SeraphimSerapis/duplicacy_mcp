"""duplicacy-mcp: MCP server wrapping the duplicacy-web HTTP API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("duplicacy-mcp")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]

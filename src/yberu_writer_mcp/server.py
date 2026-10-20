"""Yberu Writer MCP stdio server using the installed MCP SDK 2.x API."""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from .config import Settings
from .session import WriterService
from .tools.registry import register_tools


def build_server(settings: Settings | None = None, service: WriterService | None = None) -> tuple[MCPServer, WriterService]:
    settings = settings or Settings.from_env()
    service = service or WriterService(settings)
    server = MCPServer(name="yberu-writer-mcp", title="Yberu Writer MCP", version="0.1.0",
                       description="Safe, revision-guarded control of open LibreOffice Writer documents.",
                       log_level=settings.log_level)
    register_tools(server, service)
    return server, service


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=Settings.from_env().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server, service = build_server()
    try:
        server.run("stdio")
    finally:
        service.close()


if __name__ == "__main__":
    main()

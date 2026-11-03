"""Yberu Writer MCP stdio server using the installed MCP SDK 2.x API."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus

from .config import Settings
from .mcp_events import MCPEventPublisher
from .session import WriterService
from .tools.registry import register_tools
from .tools.resources import register_resources


def build_server(settings: Settings | None = None, service: WriterService | None = None) -> tuple[MCPServer, WriterService]:
    settings = settings or Settings.from_env()
    service = service or WriterService(settings)
    subscription_bus = InMemorySubscriptionBus()
    event_publisher = MCPEventPublisher(subscription_bus)
    service.set_event_publisher(event_publisher)

    @asynccontextmanager
    async def lifespan(_server):
        event_publisher.start(asyncio.get_running_loop())
        try:
            yield {}
        finally:
            event_publisher.stop()

    server = MCPServer(name="yberu-writer-mcp", title="Yberu Writer MCP", version="0.1.0",
                       description="Safe, revision-guarded control of open LibreOffice Writer documents.",
                       log_level=settings.log_level, subscriptions=subscription_bus, lifespan=lifespan)
    register_tools(server, service)
    register_resources(server, service)
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

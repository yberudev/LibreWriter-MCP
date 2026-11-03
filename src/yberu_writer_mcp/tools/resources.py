"""MCP resources that expose the latest coalesced Writer event state."""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from ..errors import WriterError
from ..session import WriterService


def register_resources(server: MCPServer, service: WriterService) -> MCPServer:
    @server.resource(
        "yberu-writer://documents/{document_id}/events",
        name="writer_document_events",
        description="Latest coalesced event metadata for one open Writer document; contains no document text.",
        mime_type="application/json",
    )
    def writer_document_events(document_id: str) -> str:
        """Read event metadata after a matching MCP resource update notification."""
        try:
            value = service.read_event_snapshot(document_id)
        except WriterError as exc:
            if exc.code == "DOCUMENT_NOT_FOUND":
                raise ResourceNotFoundError(exc.message) from exc
            raise ResourceError(json.dumps(exc.as_dict(), ensure_ascii=False)) from exc
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    return server

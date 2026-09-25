from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .contracts import Contracts


class ToolCallError(RuntimeError):
    """A tool error is not an empty evidence result."""


class EvidenceGateway:
    def __init__(
        self, session: ClientSession, contracts: Contracts, *, request_interval: float = 0.0
    ) -> None:
        if not math.isfinite(request_interval) or request_interval < 0:
            raise ValueError("MCP_REQUEST_INTERVAL_SECONDS must be finite and nonnegative")
        self._request_interval = request_interval
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, dict[str, Any]] | None = None

    async def list_tools(self) -> list[str]:
        return sorted(await self.describe_tools())

    async def describe_tools(self) -> dict[str, dict[str, Any]]:
        if self._tools is None:
            discovered = {}
            cursor = None
            seen_cursors: set[str] = set()
            while True:
                response = await self._session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
                )
                for tool in response.tools:
                    discovered[tool.name] = tool.model_dump(mode="json", by_alias=True)
                cursor = getattr(response, "next_cursor", None)
                if cursor is None:
                    cursor = getattr(response, "nextCursor", None)
                if not cursor:
                    break
                if cursor in seen_cursors:
                    raise ValueError("MCP tool discovery repeated a pagination cursor")
                seen_cursors.add(cursor)
            self._tools = discovered
        return self._tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if self._request_interval:
            # One request in flight, then a quiet interval, including after tool errors.
            async with self._request_lock:
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0.0, self._next_request_at - loop.time()))
                try:
                    return await self._call(tool_name, case_id=case_id, **arguments)
                finally:
                    self._next_request_at = loop.time() + self._request_interval
        return await self._call(tool_name, case_id=case_id, **arguments)

    async def _call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = None
        for attempt in range(3):
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                break
            except BaseException:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))

        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise ToolCallError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    request_interval = float(os.getenv("MCP_REQUEST_INTERVAL_SECONDS", "0"))
    if not math.isfinite(request_interval) or request_interval < 0:
        raise ValueError("MCP_REQUEST_INTERVAL_SECONDS must be finite and nonnegative")
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=60.0, write=60.0, pool=60.0)
    async with (
        httpx2.AsyncClient(
            headers=headers, timeout=timeout, transport=httpx2.AsyncHTTPTransport(retries=2)
        ) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts, request_interval=request_interval)

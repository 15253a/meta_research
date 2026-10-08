from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import json
import os
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


class ExternalMcpClientError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ExternalMcpClient:
    def __init__(self, *, timeout_seconds: float = 10, max_bytes: int = 1024 * 1024) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    def discover(self, connection: dict[str, Any]) -> dict[str, Any]:
        return self._run(connection, None)

    def call(self, connection: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._run(connection, (name, arguments))

    def _run(self, connection: dict[str, Any], call: tuple[str, dict[str, Any]] | None) -> dict[str, Any]:
        try:
            return asyncio.run(self._request(connection, call))
        except (TimeoutError, asyncio.TimeoutError) as error:
            raise ExternalMcpClientError("timeout") from error
        except ExternalMcpClientError:
            raise
        except Exception as error:
            leaves = _exception_leaves(error)
            code = "unreachable"
            client_error = next((item for item in leaves if isinstance(item, ExternalMcpClientError)), None)
            if client_error is not None:
                code = client_error.code
            elif any(isinstance(item, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)) for item in leaves):
                code = "timeout"
            elif any(getattr(getattr(item, "response", None), "status_code", None) in (401, 403) for item in leaves):
                code = "authentication_failed"
            elif any(isinstance(item, FileNotFoundError) for item in leaves):
                code = "process_start_failed"
            elif any(type(item).__name__ in {"ValidationError", "McpError"} for item in leaves):
                code = "invalid_protocol"
            raise ExternalMcpClientError(code) from error

    async def _request(self, connection: dict[str, Any], call: tuple[str, dict[str, Any]] | None) -> dict[str, Any]:
        async with asyncio.timeout(self.timeout_seconds):
            async with AsyncExitStack() as stack:
                if connection["transport"] == "stdio":
                    parameters = StdioServerParameters(
                        command=connection["command"], args=connection.get("arguments", []),
                        env=connection.get("environment", {}), cwd=connection.get("working_directory"),
                    )
                    stderr = stack.enter_context(open(os.devnull, "w"))
                    streams = await stack.enter_async_context(stdio_client(parameters, errlog=stderr))
                else:
                    http_client = await stack.enter_async_context(httpx.AsyncClient(
                        headers=connection.get("headers", {}), timeout=self.timeout_seconds,
                    ))
                    streams = await stack.enter_async_context(streamable_http_client(
                        connection["url"], http_client=http_client,
                    ))
                session = await stack.enter_async_context(ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                ))
                initialized = await session.initialize()
                if call is not None:
                    result = (await session.send_request(types.ClientRequest(types.CallToolRequest(
                        params=types.CallToolRequestParams(name=call[0], arguments=call[1]),
                    )), types.CallToolResult)).model_dump(mode="json", by_alias=True, exclude_none=True)
                    self._bounded(result)
                    return result
                tools: list[dict[str, Any]] = []
                cursor = None
                seen: set[str] = set()
                for _page in range(32):
                    page = await session.list_tools(cursor=cursor)
                    tools.extend(tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in page.tools)
                    self._bounded(tools)
                    cursor = page.nextCursor
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise ExternalMcpClientError("invalid_catalog")
                    seen.add(cursor)
                else:
                    raise ExternalMcpClientError("invalid_catalog")
                names = [tool["name"] for tool in tools]
                if len(set(names)) != len(names):
                    raise ExternalMcpClientError("invalid_catalog")
                result = {"server_name": initialized.serverInfo.name, "protocol_version": initialized.protocolVersion,
                    "server_instructions": initialized.instructions or "", "tools": tools}
                self._bounded(result)
                return result

    def _bounded(self, value: object) -> None:
        if len(json.dumps(value, ensure_ascii=False).encode()) > self.max_bytes:
            raise ExternalMcpClientError("response_too_large")


def _exception_leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for item in error.exceptions for leaf in _exception_leaves(item)]
    return [error]

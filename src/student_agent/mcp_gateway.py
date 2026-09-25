from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2
from jsonschema import Draft202012Validator, FormatChecker
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import PaginatedRequestParams

from .cases import CASE_ID_PATTERN
from .contracts import ContractError, Contracts


class GatewayError(RuntimeError):
    """Sanitized failure metadata; raw server messages never enter public trace."""

    def __init__(
        self, code: str, *, retryable: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


def _attribute(value: Any, snake: str, camel: str, default: Any = None) -> Any:
    return getattr(value, snake, getattr(value, camel, default))


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if seconds < float("inf") else None
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def transport_error(exc: Exception) -> GatewayError | None:
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, ExceptionGroup):
        errors = [transport_error(child) for child in exc.exceptions]
        if any(error is None for error in errors):
            return None
        known = [error for error in errors if error is not None]
        return next((error for error in known if not error.retryable), known[0])
    if isinstance(exc, httpx2.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return GatewayError("MCP_AUTH_FAILED")
        if status == 404:
            return GatewayError("MCP_NOT_FOUND")
        return GatewayError(
            "MCP_HTTP_ERROR",
            retryable=status == 429 or 500 <= status <= 599,
            retry_after=_retry_after(exc.response.headers.get("Retry-After")),
        )
    if isinstance(exc, (TimeoutError, httpx2.TimeoutException)):
        return GatewayError("MCP_TIMEOUT", retryable=True)
    if isinstance(exc, httpx2.NetworkError):
        return GatewayError("MCP_CONNECTION_FAILED", retryable=True)
    if isinstance(exc, MCPError):
        # JSON-RPC codes are not HTTP status codes. Do not infer errors from prose.
        return GatewayError("MCP_PROTOCOL_ERROR")
    return None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def validate_arguments(self, payload: dict[str, Any]) -> None:
        validator = Draft202012Validator(self.input_schema, format_checker=FormatChecker())
        if not validator.is_valid(payload):
            raise GatewayError("TOOL_ARGUMENTS_INVALID")
        # The discovered competition tools list their complete set of arguments.
        # Reject typos even when the server schema omits additionalProperties=false.
        if set(payload) - set(self.input_schema.get("properties", {})):
            raise GatewayError("TOOL_ARGUMENTS_INVALID")


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._catalog: dict[str, ToolDefinition] | None = None
        self._discovery_lock = asyncio.Lock()

    async def discover_tools(self) -> dict[str, ToolDefinition]:
        """Keep full input schemas and follow pagination once per connection."""
        async with self._discovery_lock:
            if self._catalog is None:
                catalog: dict[str, ToolDefinition] = {}
                cursor = None
                seen_cursors: set[str] = set()
                while True:
                    params = PaginatedRequestParams(cursor=cursor) if cursor else None
                    response = await self._session.list_tools(params=params)
                    for tool in response.tools:
                        schema = _attribute(tool, "input_schema", "inputSchema")
                        if not isinstance(schema, dict) or tool.name in catalog:
                            raise GatewayError("TOOL_CATALOG_INVALID")
                        Draft202012Validator.check_schema(schema)
                        catalog[tool.name] = ToolDefinition(
                            tool.name, tool.description or "", deepcopy(schema)
                        )
                    cursor = _attribute(response, "next_cursor", "nextCursor")
                    if not cursor:
                        break
                    if cursor in seen_cursors:
                        raise GatewayError("TOOL_CATALOG_CURSOR_LOOP")
                    seen_cursors.add(cursor)
                self._catalog = catalog
        return deepcopy(self._catalog)

    async def list_tools(self) -> list[str]:
        return sorted(await self.discover_tools())

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise GatewayError("CASE_SCOPE_INVALID")
        catalog = await self.discover_tools()
        if tool_name not in catalog:
            raise GatewayError("TOOL_NOT_DISCOVERED")
        payload = {"case_id": case_id, **arguments}
        catalog[tool_name].validate_arguments(payload)
        try:
            result = await self._session.call_tool(tool_name, arguments=payload)
        except Exception as exc:
            error = transport_error(exc)
            if error is None:
                raise
            raise error from None
        if _attribute(result, "is_error", "isError", False):
            raise GatewayError("MCP_TOOL_ERROR")
        evidence = _attribute(result, "structured_content", "structuredContent")
        if evidence is None:
            text_blocks = [
                block.text
                for block in getattr(result, "content", [])
                if getattr(block, "text", None)
            ]
            if len(text_blocks) != 1:
                raise GatewayError("EVIDENCE_INVALID")
            try:
                evidence = json.loads(text_blocks[0])
            except (TypeError, ValueError):
                raise GatewayError("EVIDENCE_INVALID") from None
        try:
            self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
            # Reject NaN/Infinity, which are not JSON numbers.
            json.dumps(evidence, allow_nan=False)
        except (ContractError, TypeError, ValueError):
            raise GatewayError("EVIDENCE_INVALID") from None
        return deepcopy(evidence)


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    try:
        async with (
            httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
            streamable_http_client(endpoint, http_client=http_client) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            yield EvidenceGateway(session, contracts)
    except Exception as exc:
        error = transport_error(exc)
        if error is None:
            raise
        raise error from None

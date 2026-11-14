"""Read-only clients: Jaeger native MCP (streamable HTTP) and Prometheus HTTP API."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import core


class BackendError(RuntimeError):
    """Query backends are unavailable or returned something we cannot trust."""


def _tool_text(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


class JaegerMCP:
    """Thin adapter over Jaeger's native MCP endpoint.

    Only whitelisted tools are exposed. The `sql` field of search_traces is
    unreachable: callers pass structured filters only.
    """

    ALLOWED = {
        "get_services",
        "search_traces",
        "get_trace_topology",
        "get_trace_errors",
        "get_critical_path",
        "get_span_names",
        "get_span_details",
    }

    def __init__(self, url: str = core.JAEGER_MCP_URL, timeout: float = core.TOOL_TIMEOUT):
        self.url = url
        self.timeout = timeout

    async def call(self, tool: str, arguments: dict, *, trace_id: str | None = None) -> dict:
        if tool not in self.ALLOWED:
            raise PermissionError(f"tool not allowed: {tool}")
        if "sql" in arguments:
            raise PermissionError("raw sql is not accepted")
        started = time.monotonic()
        try:
            async with streamablehttp_client(self.url, timeout=self.timeout) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments)
        except Exception as exc:  # connection, protocol, timeout
            raise BackendError(f"mcp_unavailable: {type(exc).__name__}") from exc
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        text = _tool_text(result)
        if getattr(result, "isError", False):
            raise BackendError("mcp_tool_error")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise BackendError("mcp_invalid_json") from exc
        raw_bytes = len(text.encode("utf-8"))
        if raw_bytes > core.MAX_TOOL_BYTES:
            raise BackendError("mcp_output_over_budget")
        return {
            "tool": tool,
            "arguments": arguments,
            "elapsed_ms": elapsed_ms,
            "result": payload,
            "raw_bytes": raw_bytes,
            "result_sha256": core.sha256_text(text),
        }


class Prometheus:
    def __init__(self, url: str = core.PROM_URL, timeout: float = core.TOOL_TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _q(value: float) -> str:
        if float(value) == int(value):
            return str(int(value))
        return f"{value:.3f}".rstrip("0")

    @staticmethod
    def _check(expr: str) -> None:
        if not isinstance(expr, str) or not expr or len(expr) > 400:
            raise PermissionError("expression rejected")

    async def query(self, expr: str, at: float | None = None) -> dict:
        self._check(expr)
        params: dict[str, Any] = {"query": expr}
        if at is not None:
            params["time"] = self._q(at)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(f"{self.url}/api/v1/query", params=params)
            except httpx.HTTPError as exc:
                raise BackendError(f"prom_unavailable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise BackendError(f"prom_http_{resp.status_code}")
        body = resp.json()
        if body.get("status") != "success":
            raise BackendError("prom_status_error")
        return {
            "expr": expr,
            "time": at,
            "result": body.get("data", {}),
            "result_sha256": core.sha256_text(resp.text),
        }

    async def query_range(self, expr: str, start: float, end: float, step: float) -> dict:
        self._check(expr)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(
                    f"{self.url}/api/v1/query_range",
                    params={"query": expr, "start": self._q(start), "end": self._q(end), "step": self._q(step)},
                )
            except httpx.HTTPError as exc:
                raise BackendError(f"prom_unavailable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise BackendError(f"prom_http_{resp.status_code}")
        body = resp.json()
        data = body.get("data", {})
        series = []
        for item in data.get("result", []) or []:
            points = [[float(ts), float(val)] for ts, val in item.get("values", [])]
            series.append({"labels": item.get("metric", {}), "points": points})
        return {
            "expr": expr,
            "start": start,
            "end": end,
            "step_s": step,
            "series": series,
            "result_sha256": core.sha256_text(resp.text),
        }

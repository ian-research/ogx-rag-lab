"""SDK adapters. Protocol failures are never silently converted into successful text."""
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import settings
from security import service_key
from tool_contracts import MODEL_TOOLS, SCHEMAS, RunFailure


def headers(identity, as_of):
    return {"Authorization": "Bearer " + service_key(),
            "X-Authoritative-User-Id": identity, "X-Rag-As-Of": as_of}


async def discover(identity, as_of):
    async with streamablehttp_client(settings.MCP_URL, headers=headers(identity, as_of)) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            response = await session.list_tools()
            names = [tool.name for tool in response.tools]
    if set(names) != set(SCHEMAS) or len(names) != len(SCHEMAS):
        raise RunFailure("tool_configuration_error", "工具列表与固定只读配置不一致。")
    return MODEL_TOOLS


def decode_result(result):
    if result.isError:
        raise RunFailure("tool_error", "工具返回执行错误，本次查询未正常完成。")
    blocks = result.content
    if len(blocks) != 1 or getattr(blocks[0], "type", None) != "text":
        raise RunFailure("tool_error", "工具返回格式不符合本实验约定。")
    text = blocks[0].text
    if len(text.encode()) > settings.MAX_TOOL_BYTES:
        raise RunFailure("result_too_large", "工具结果超过上下文预算，请缩小查询范围。")
    try:
        data = json.loads(text)
    except ValueError:
        raise RunFailure("tool_error", "工具没有返回结构化查询结果。") from None
    if not isinstance(data, dict) or data.get("status") not in {
        "ok", "no_record", "no_data", "needs_selection", "conflict"
    }:
        raise RunFailure("tool_error", "工具未成功查询，不能将错误当成业务事实。")
    return data


async def call_tool(name, arguments, identity, as_of):
    async with streamablehttp_client(settings.MCP_URL, headers=headers(identity, as_of)) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    return decode_result(result)


async def ask_model(messages, tools):
    async with httpx.AsyncClient(timeout=settings.MODEL_TIMEOUT) as client:
        response = await client.post(settings.OGX_BASE + "/v1/chat/completions", json={
            "model": settings.OGX_MODEL, "messages": messages, "tools": tools,
            "tool_choice": "auto", "temperature": 0.0, "max_tokens": 1024,
            "chat_template_kwargs": {"enable_thinking": False}})
        response.raise_for_status()
        return response.json()

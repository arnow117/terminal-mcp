"""Five session tools over the official low-level MCP stdio server."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server

from . import __version__
from .audit import AuditCall, current_call
from .config import Config
from .contracts import TOOL_SCHEMAS, ToolError, validate_arguments
from .session import SessionManager

logger = logging.getLogger(__name__)
_TOOL_METHODS = {
    "terminal_start": "start",
    "terminal_read": "read",
    "terminal_write": "write",
    "terminal_list": "list",
    "terminal_kill": "kill",
}


@dataclass
class AppContext:
    manager: SessionManager


async def on_list_tools(
    ctx: ServerRequestContext[AppContext], params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=schema["name"], description=schema["description"],
                   input_schema=schema["inputSchema"], output_schema=schema["outputSchema"])
        for schema in TOOL_SCHEMAS.values()
    ])


def _summary(name: str, result: dict) -> str:
    if name == "terminal_list":
        return f"{result['count']} retained session(s)."
    text = f"Session {result['session_id']}: {result['status']}."
    for key in ("exit_code", "signal", "written_bytes", "next_cursor", "dropped_bytes", "wait_reason", "removed"):
        if key in result and result[key] is not None:
            text += f" {key}={result[key]}."
    # Structured content is authoritative; text previews never duplicate a whole page.
    for key in ("output", "stdout", "stderr"):
        if result.get(key):
            preview = json.dumps(result[key][:160], ensure_ascii=False)
            if len(preview) > 180:
                preview = preview[:180] + "..."
            elif len(result[key]) > 160:
                preview += "..."
            text += f" {key}: {preview}"
    return text


async def on_call_tool(
    ctx: ServerRequestContext[AppContext], params: types.CallToolRequestParams,
) -> types.CallToolResult:
    manager = ctx.lifespan_context.manager
    call = AuditCall(params.name, params.arguments or {}, manager)
    token = current_call.set(call)
    result, error = None, None
    cancelled = False
    try:
        # This preflight audit must succeed before a tool can create a process.
        try:
            manager.log_audit(call.event(decision="pending"))
        except ToolError:
            # Broken audit storage must not prevent explicit ownership cleanup.
            # The safety latch and completion audit still return INTERNAL_ERROR.
            if params.name != "terminal_kill":
                raise
        arguments = validate_arguments(params.name, {} if params.arguments is None else params.arguments)
        method = getattr(manager, _TOOL_METHODS[params.name])
        result = await method(**arguments)
        manager.check_persistence()
    except asyncio.CancelledError:
        cancelled = True
        raise
    except ToolError as exc:
        error = exc
    except Exception:
        # Do not expose exception payloads, command bodies, or environment values.
        # Cancellation is a BaseException and must propagate to the SDK unchanged.
        logger.error("INTERNAL_ERROR while handling %s.", params.name)
        error = ToolError("INTERNAL_ERROR", "The tool could not complete because of an internal error.")
    finally:
        try:
            manager.log_audit(call.event(
                result=result if result is not None else error.as_dict() if error else None,
                error_code="CANCELLED" if cancelled else error.code if error else None,
            ))
        except ToolError as exc:
            error = exc
        finally:
            current_call.reset(token)
    if error is None:
        return types.CallToolResult(
            structured_content=result,
            content=[types.TextContent(type="text", text=_summary(params.name, result))],
            is_error=False,
        )
    return types.CallToolResult(
        structured_content=error.as_dict(),
        content=[types.TextContent(type="text", text=f"{error.code}: {error.message} Hint: {error.hint}")],
        is_error=True,
    )


def create_server(config: Config) -> Server[AppContext]:
    @asynccontextmanager
    async def lifespan(server: Server[AppContext]) -> AsyncIterator[AppContext]:
        manager = SessionManager(config)
        try:
            await manager.initialize()
            yield AppContext(manager=manager)
        finally:
            # EOF, handler failure, and cancellation share the same ownership boundary.
            with anyio.CancelScope(shield=True):
                await manager.shutdown_all()

    return Server(
        "terminal-mcp", version=__version__, lifespan=lifespan,
        on_list_tools=on_list_tools, on_call_tool=on_call_tool,
    )


async def run_stdio(config: Config) -> None:
    server = create_server(config)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

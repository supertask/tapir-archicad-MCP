"""MCP tools of the command guard (fork addition, kept apart from upstream's functions.py)."""

import asyncio

from mcp.types import ToolAnnotations

from tapir_archicad_mcp.app import mcp
from tapir_archicad_mcp.context import command_guard_instance, multi_conn_instance
from tapir_archicad_mcp.dialogs import dialogs_supported, list_dialogs_for_port


@mcp.tool(
    name="archicad_get_dialogs",
    title="Get Archicad Modal Dialogs",
    description=(
        "Lists the modal dialogs (title, text, button labels) each running Archicad is showing. While a modal "
        "dialog is open Archicad does not process commands. Pass a port to inspect one instance; without it every "
        "active instance is inspected. Read-only: it never presses a button. Windows only."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True,
    ),
)
async def archicad_get_dialogs(port: int | None = None) -> dict:
    if not dialogs_supported():
        return {"supported": False, "instances": [],
                "message": "Dialog inspection is only available on Windows."}
    if port is not None:
        ports = [port]
    else:
        ports = await asyncio.to_thread(lambda: [int(p) for p in multi_conn_instance.get().open_port_headers])
    instances = []
    for target in ports:
        dialogs = await asyncio.to_thread(list_dialogs_for_port, target)
        instances.append({
            "port": target,
            "blocked": any(d.blocking for d in dialogs),
            "dialogs": [d.to_dict() for d in dialogs],
        })
    return {"supported": True, "instances": instances}


@mcp.tool(
    name="archicad_list_pending_commands",
    title="List Pending Archicad Commands",
    description=(
        "Lists commands this server stopped waiting for because Archicad did not answer (usually because of a "
        "modal dialog). Such a command stays queued inside Archicad and runs when the dialog is closed. status is "
        "'queued_in_archicad', 'executed_late' or 'failed_late' (with its outcome). Check this before resending a "
        "command that timed out."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
)
def archicad_list_pending_commands() -> dict:
    return {"pendingCommands": command_guard_instance.get().pending.summaries()}


@mcp.tool(
    name="archicad_clear_pending_commands",
    title="Clear Pending Archicad Command Records",
    description=(
        "Forgets pending-command records once the user has checked their effect, so identical commands can be "
        "sent again. With pending_ids, removes exactly those records. Without it, removes every record that has "
        "already finished (optionally only for one port); records still queued in Archicad are kept. This only "
        "edits this server's records; it never touches Archicad."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
)
def archicad_clear_pending_commands(pending_ids: list[str] | None = None, port: int | None = None) -> dict:
    return {"removed": command_guard_instance.get().pending.clear(pending_ids, port)}

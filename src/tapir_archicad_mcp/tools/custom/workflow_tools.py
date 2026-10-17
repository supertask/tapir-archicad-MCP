"""Workflow tools of the fork: run several commands in one call, save with a file check, read the guide.

Every command still goes through the command guard (dialog preflight, time limit,
pending-command records), exactly like archicad_call_tool.
"""

import json
import logging
import time
from importlib import resources
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from multiconn_archicad import SoloProjectID, TeamworkProjectID
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tapir_archicad_mcp.app import mcp
from tapir_archicad_mcp.context import command_guard_instance, job_store_instance
from tapir_archicad_mcp.request_lifecycle import DispatchLifecycle
from tapir_archicad_mcp.tools.custom.functions import _dispatch_command, _get_header, _parameters_of
from tapir_archicad_mcp.tools.tool_registry import ToolRegistryEntry, get_tool_entry

log = logging.getLogger(__name__)

GUIDE_FILE = "archicad26_guide.md"
DEFAULT_MAX_RESULT_CHARS = 20_000


def validate_command(name: str, port: int, params: dict | None) -> tuple[ToolRegistryEntry, Any]:
    """Check a command name and its parameters before anything is sent."""
    entry = get_tool_entry(name)
    arguments: dict[str, Any] = {"port": port}
    if entry.params_model is not None:
        arguments["params"] = params if params is not None else {}
    elif params:
        raise ToolError(f"Command '{name}' takes no parameters.")
    try:
        return entry, entry.arguments_model.model_validate(arguments)
    except ValidationError as e:
        raise ToolError(f"Invalid parameters for '{name}': {e}") from e


def run_validated(name: str, entry: ToolRegistryEntry, validated: Any, request_id: str | None = None) -> Any:
    """Send one validated command through the command guard and return Archicad's full answer."""
    port = validated.port
    header = _get_header(port)
    call_args: dict[str, Any] = {"conn_header": header}
    if entry.params_model is not None:
        call_args["params"] = validated.params
    guard = command_guard_instance.get()
    with DispatchLifecycle(name, port, request_id):
        guard.preflight(header, port)
        return guard.run(
            name, port, _parameters_of(entry, validated),
            lambda: _dispatch_command(name, entry, call_args, port),
        )


def run_command(name: str, port: int, params: dict | None = None, request_id: str | None = None) -> Any:
    entry, validated = validate_command(name, port, params)
    return run_validated(name, entry, validated, request_id)


def require_success(name: str, result: Any) -> Any:
    """Tapir reports some failures as {'success': false, 'error': ...} instead of raising."""
    if isinstance(result, dict) and result.get("success") is False:
        error = result.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ToolError(f"'{name}' failed in Archicad: {message or result}")
    return result


def shrink(result: Any, max_chars: int) -> Any:
    """Keep a step's answer small enough for the model; say so when it is cut."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if max_chars <= 0 or len(text) <= max_chars:
        return result
    return {"truncated": True, "totalChars": len(text), "preview": text[:max_chars]}


class BatchStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Command name, as listed by archicad_list_commands.")
    params: dict | None = Field(
        default=None, description="The command's 'params' object (see archicad_get_command_schema); omit if none.",
    )


def run_batch(
    port: int,
    commands: list[tuple[str, ToolRegistryEntry, Any]],
    stop_on_error: bool,
    expect_window_type: str | None,
    max_result_chars: int,
    request_id: str,
) -> dict:
    if expect_window_type:
        current = run_command("app_get_current_window_type", port, None, f"{request_id}#window")
        actual = current.get("currentWindowType") if isinstance(current, dict) else None
        if actual != expect_window_type:
            raise ToolError(
                f"The active Archicad window is '{actual}', not '{expect_window_type}'. Nothing was sent. Many "
                "element commands act on the active window's database only (a Layout window reads and edits "
                "drawings, not walls). Switch with app_change_window and run the batch again."
            )
    results: list[dict] = []
    failed_at: int | None = None
    for index, (name, entry, validated) in enumerate(commands):
        try:
            answer = require_success(name, run_validated(name, entry, validated, f"{request_id}#{index}"))
            results.append({"index": index, "name": name, "ok": True, "result": shrink(answer, max_result_chars)})
        except Exception as e:  # noqa: BLE001 - every step reports its own outcome
            results.append({"index": index, "name": name, "ok": False, "error": str(e)})
            if failed_at is None:
                failed_at = index
            if stop_on_error:
                break
    return {
        "ran": len(results),
        "succeeded": sum(1 for r in results if r["ok"]),
        "firstFailedIndex": failed_at,
        "notRun": len(commands) - len(results),
        "steps": results,
    }


@mcp.tool(
    name="archicad_batch",
    title="Run Several Archicad Commands in One Call",
    description=(
        "Runs a list of commands in order on one Archicad instance and returns every step's result. Use it "
        "instead of many archicad_call_tool calls for bulk work. Each step is {'name': <command>, 'params': {...}} "
        "with the same names and params as archicad_call_tool (without 'port'). All steps are validated before "
        "the first is sent. Every step goes through the same dialog check and time limit as a single call; with "
        "stop_on_error (default) the batch stops at the first failure and reports which steps were not run. "
        "expect_window_type (e.g. 'FloorPlan', 'Layout') refuses to start unless that window is active. Each "
        "step is its own undo step in Archicad. Save before large or risky batches (bulk drawing re-creation "
        "has crashed Archicad 26). Long batches return status='running' and a jobHandle for archicad_get_job."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
)
async def archicad_batch(
    port: int,
    steps: list[BatchStep],
    stop_on_error: bool = True,
    expect_window_type: str | None = None,
    allow_duplicate: bool = False,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> dict:
    if not steps:
        raise ToolError("steps is empty.")
    commands = []
    for index, step in enumerate(steps):
        try:
            entry, validated = validate_command(step.name, port, step.params)
        except ToolError as e:
            raise ToolError(f"Step {index} ({step.name}): {e}. Nothing was sent.") from e
        commands.append((step.name, entry, validated))
    guard = command_guard_instance.get()
    if not allow_duplicate:
        for name, entry, validated in commands:
            guard.check_duplicate(name, port, _parameters_of(entry, validated))
    store = job_store_instance.get()
    job = store.submit(
        "archicad_batch", port, {"steps": [step.name for step in steps]},
        lambda handle: run_batch(port, commands, stop_on_error, expect_window_type, max_result_chars, handle),
    )
    await store.wait(job)
    return store.initial_result(job)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def save_and_verify(port: int, wait_seconds: float, request_id: str) -> dict:
    project = _get_header(port).archicad_id
    if isinstance(project, TeamworkProjectID):
        result = require_success("project_save_project", run_command("project_save_project", port, None, request_id))
        return {"saved": True, "projectType": "teamwork", "tapirResult": result,
                "note": "Teamwork projects are saved on the BIMcloud; there is no local file to check."}
    if not isinstance(project, SoloProjectID):
        raise ToolError(
            "The project is untitled, so it has no file to save into. Save it once as an archive with "
            "project_save_project_as_archive (it then becomes the open project) or ask the user to use "
            "File > Save As; after that archicad_save_project works."
        )
    path = Path(project.projectPath)
    before = _mtime(path)
    result = require_success("project_save_project", run_command("project_save_project", port, None, request_id))
    deadline = time.monotonic() + max(wait_seconds, 0)
    after = _mtime(path)
    while (after is None or after == before) and time.monotonic() < deadline:
        time.sleep(0.5)
        after = _mtime(path)
    saved = after is not None and after != before
    response = {
        "saved": saved, "projectType": "solo", "path": str(path),
        "modifiedBefore": before, "modifiedAfter": after, "tapirResult": result,
    }
    if not saved:
        response["note"] = (
            "Archicad answered the save, but the file on disk did not change. Either there was nothing to save "
            "or the save did not happen. If changes were made, ask the user to press Ctrl+S in Archicad and "
            "check again."
        )
    return response


@mcp.tool(
    name="archicad_save_project",
    title="Save the Archicad Project and Confirm the File Changed",
    description=(
        "Saves the open project (Tapir SaveProject) and then checks the project file's modification time, so "
        "'saved' means the file on disk really changed. Untitled projects are refused with what to do instead. "
        "Use it after every heavy step: work after the last save survives an Archicad crash only in recovery data."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
async def archicad_save_project(port: int, wait_seconds: float = 30.0) -> dict:
    store = job_store_instance.get()
    job = store.submit(
        "archicad_save_project", port, {}, lambda handle: save_and_verify(port, wait_seconds, handle),
    )
    await store.wait(job)
    return store.initial_result(job)


def read_guide() -> str:
    return resources.files("tapir_archicad_mcp.guides").joinpath(GUIDE_FILE).read_text(encoding="utf-8")


@mcp.tool(
    name="archicad_get_guide",
    title="Get the Archicad 26 Working Guide",
    description=(
        "Returns the working guide for this server on Archicad 26 (Japanese edition): which tool to use for "
        "what, how to check results visually, saving, PDF, layouts and drawings, text, dialogs and the known "
        "pitfalls. Read it once before building or editing a project."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def archicad_get_guide() -> str:
    return read_guide()

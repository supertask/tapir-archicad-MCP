"""Bounded, dialog-aware dispatch of Archicad commands.

Archicad does not answer JSON API commands while it shows a modal dialog; the
command stays in its queue and runs when the dialog is closed. Without a limit a
dispatch therefore waits forever. This module

* checks with API.IsAlive (which Archicad answers even behind a dialog) before
  sending, so nothing is queued behind a dialog that is already open,
* bounds every command with a per-command time limit and gives up early when a
  modal dialog keeps Archicad busy, reporting the dialog's title, text and buttons,
* records commands that were abandoned while still queued in Archicad, follows
  them to their late outcome, and refuses to send an identical write again until
  the user has seen what happened.
"""

import json
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from multiconn_archicad.errors import APIErrorBase, RequestError, StandardAPIError

from tapir_archicad_mcp.dialogs import DialogInfo, dialogs_supported, list_dialogs_for_port

log = logging.getLogger(__name__)

# Commands that legitimately run for minutes; they get the long time limit.
LONG_RUNNING_COMMANDS = frozenset(
    {
        "project_open_project",
        "project_close_project",
        "project_save_project",
        "project_save_project_as_archive",
        "project_save_as_module_file",
        "project_print_view",
        "project_rebuild_view",
        "project_create_hotlink_nodes",
        "navigator_publish_publisher_set",
        "navigator_update_drawings",
        "ifc_ifc_file_operation",
        "issues_export_issues_to_bcf",
        "issues_import_issues_from_bcf",
        "library_reload_libraries",
        "library_add_libraries",
        "library_set_libraries",
        "teamwork_teamwork_receive",
        "teamwork_teamwork_send",
        "classifications_import_classifications_xml",
        "properties_import_properties_xml",
        "favorites_import_favorites",
        "app_show_alert",
    }
)

# Commands that open a dialog on purpose and wait for the user to answer it; a
# dialog is not a reason to give up on them early.
DIALOG_COMMANDS = frozenset({"app_show_alert"})

# A command whose name says it only reads. Repeating it cannot change the project.
_READ_ONLY_NAME = re.compile(r"^[a-z]+_(get|list|is|find|search|has)_")
_MODAL_DIALOG_MESSAGE = re.compile(r"modal dialog:?\s*(.*?)\)?\s*$", re.IGNORECASE)
POLL_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class GuardSettings:
    """Time limits; CLI/environment values are validated by the argument parser."""

    command_timeout_seconds: float = 60
    long_command_timeout_seconds: float = 900
    command_timeout_overrides: dict[str, float] = field(default_factory=dict)
    preflight_timeout_seconds: float = 5
    dialog_grace_seconds: float = 10
    pending_ttl_seconds: float = 86400
    max_pending_commands: int = 64

    def timeout_for(self, command: str) -> float | None:
        """The time limit for one command; None means wait without limit."""
        if command in self.command_timeout_overrides:
            seconds = self.command_timeout_overrides[command]
        elif command in LONG_RUNNING_COMMANDS:
            seconds = self.long_command_timeout_seconds
        else:
            seconds = self.command_timeout_seconds
        return seconds if seconds > 0 else None


def parse_timeout_overrides(text: str | None) -> dict[str, float]:
    """Parse 'name=seconds,name=seconds' into a dict."""
    overrides: dict[str, float] = {}
    if not text:
        return overrides
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"Expected 'command=seconds', got '{item}'.")
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"Invalid number of seconds for '{name.strip()}': {value}")
        overrides[name.strip()] = seconds
    return overrides


def is_read_only_command(command: str) -> bool:
    return bool(_READ_ONLY_NAME.match(command))


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ArchicadUnresponsiveError(ToolError):
    """Archicad did not answer in time (or could not, because of a modal dialog)."""

    def __init__(self, message: str, dialogs: list[DialogInfo] | None = None, pending_id: str | None = None):
        super().__init__(message)
        self.dialogs = dialogs or []
        self.pending_id = pending_id


def _dialog_report(dialogs: list[DialogInfo], dialog_name: str | None = None) -> str:
    if dialogs:
        lines = [f"  - {dialog.describe()}" for dialog in dialogs]
        return "Archicad is showing a modal dialog:\n" + "\n".join(lines)
    if dialog_name:
        return f"Archicad reports an open modal dialog: '{dialog_name}'."
    if not dialogs_supported():
        return "Dialog inspection is only available on Windows; check the Archicad window for an open dialog."
    return "No modal dialog was found; Archicad may be busy with a long operation."


# ---------------------------------------------------------------------------
# Pending (abandoned but still queued) commands
# ---------------------------------------------------------------------------


@dataclass
class PendingCommand:
    pending_id: str
    command: str
    port: int
    parameters: str
    reason: str
    abandoned_at: str
    abandoned_clock: float
    read_only: bool
    dialogs: list[dict] = field(default_factory=list)
    status: str = "queued_in_archicad"
    finished_at: str | None = None
    outcome: str | None = None

    def summary(self) -> dict:
        preview = self.parameters if len(self.parameters) <= 300 else self.parameters[:299] + "…"
        return {
            "pendingId": self.pending_id,
            "command": self.command,
            "port": self.port,
            "parametersPreview": preview,
            "status": self.status,
            "reason": self.reason,
            "abandonedAt": self.abandoned_at,
            "finishedAt": self.finished_at,
            "outcome": self.outcome,
            "dialogs": self.dialogs,
        }


class PendingCommandRegistry:
    """Commands this server stopped waiting for while Archicad still had them queued."""

    def __init__(self, settings: GuardSettings, clock: Callable[[], float] | None = None) -> None:
        self.settings = settings
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, PendingCommand] = OrderedDict()

    def add(self, command: str, port: int, parameters: str, reason: str, dialogs: list[DialogInfo]) -> PendingCommand:
        with self._lock:
            self.prune()
            entry = PendingCommand(
                pending_id="pending:" + uuid.uuid4().hex[:12],
                command=command,
                port=port,
                parameters=parameters,
                reason=reason,
                abandoned_at=_now(),
                abandoned_clock=self._clock(),
                read_only=is_read_only_command(command),
                dialogs=[dialog.to_dict() for dialog in dialogs],
            )
            self._entries[entry.pending_id] = entry
            while len(self._entries) > self.settings.max_pending_commands:
                self._entries.popitem(last=False)
            return entry

    def finish(self, pending_id: str, outcome: str, succeeded: bool) -> None:
        with self._lock:
            entry = self._entries.get(pending_id)
            if entry is None:
                return
            entry.status = "executed_late" if succeeded else "failed_late"
            entry.finished_at = _now()
            entry.outcome = outcome
        log.warning("[%s] Abandoned command '%s' on port %s finished late: %s",
                    pending_id, entry.command, entry.port, entry.status)

    def find_duplicate(self, command: str, port: int, parameters: str) -> PendingCommand | None:
        """An identical write that is still queued or finished late and was not cleared."""
        if is_read_only_command(command):
            return None
        with self._lock:
            self.prune()
            for entry in reversed(self._entries.values()):
                if entry.command == command and entry.port == port and entry.parameters == parameters:
                    return entry
        return None

    def queued_on_port(self, port: int) -> list[PendingCommand]:
        with self._lock:
            return [e for e in self._entries.values() if e.port == port and e.status == "queued_in_archicad"]

    def summaries(self) -> list[dict]:
        with self._lock:
            self.prune()
            return [entry.summary() for entry in reversed(self._entries.values())]

    def clear(self, pending_ids: list[str] | None = None, port: int | None = None) -> list[str]:
        """Forget entries the user has dealt with. Still-queued entries are kept unless named explicitly."""
        with self._lock:
            removed = []
            for pending_id, entry in list(self._entries.items()):
                if pending_ids is not None:
                    selected = pending_id in pending_ids
                else:
                    selected = (port is None or entry.port == port) and entry.status != "queued_in_archicad"
                if selected:
                    del self._entries[pending_id]
                    removed.append(pending_id)
            return removed

    def prune(self) -> None:
        ttl = self.settings.pending_ttl_seconds
        if ttl <= 0:
            return
        now = self._clock()
        with self._lock:
            for pending_id, entry in list(self._entries.items()):
                if entry.status != "queued_in_archicad" and now - entry.abandoned_clock >= ttl:
                    del self._entries[pending_id]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class CommandGuard:
    def __init__(
        self,
        settings: GuardSettings | None = None,
        *,
        dialog_lister: Callable[[int], list[DialogInfo]] = list_dialogs_for_port,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings or GuardSettings()
        self._list_dialogs = dialog_lister
        self._clock = clock or time.monotonic
        self.pending = PendingCommandRegistry(self.settings, clock=self._clock)

    # -- before sending -----------------------------------------------------

    def check_duplicate(self, command: str, port: int, parameters: str) -> None:
        entry = self.pending.find_duplicate(command, port, parameters)
        if entry is None:
            return
        if entry.status == "queued_in_archicad":
            state = (
                f"is still queued in Archicad since {entry.abandoned_at} and will run as soon as the blocking "
                "dialog is closed"
            )
        else:
            state = f"was executed by Archicad after this server stopped waiting ({entry.status}: {entry.outcome})"
        raise ToolError(
            f"Duplicate refused: an identical '{command}' on port {port} {state}. Sending it again could apply "
            f"the change twice. Check the project first. To send it anyway pass allow_duplicate=true; to forget "
            f"the record call archicad_clear_pending_commands(pending_ids=['{entry.pending_id}'])."
        )

    def preflight(self, conn_header: Any, port: int) -> None:
        """Refuse to send while Archicad cannot process commands; sends nothing that would queue."""
        timeout = self.settings.preflight_timeout_seconds
        if timeout <= 0:
            return
        core = getattr(conn_header, "core", None)
        post_command = getattr(core, "post_command", None)
        if post_command is None:
            return
        try:
            post_command("API.IsAlive", timeout=timeout)
            return
        except StandardAPIError as e:
            match = _MODAL_DIALOG_MESSAGE.search(e.message or "")
            if match is None:
                log.info("API.IsAlive answered with an unexpected error on port %s: %s", port, e)
                return
            dialog_name = match.group(1).strip() or None
            reason = "modal dialog"
        except RequestError as e:
            dialog_name = None
            reason = f"no answer to API.IsAlive within {timeout:g} s ({type(e).__name__})"
        dialogs = self._list_dialogs(port)
        queued = self.pending.queued_on_port(port)
        raise ArchicadUnresponsiveError(
            f"Archicad on port {port} cannot process commands right now ({reason}). The command was NOT sent. "
            f"{_dialog_report(dialogs, dialog_name)} Ask the user to close the dialog in Archicad, then retry."
            + (f" {len(queued)} earlier command(s) are still queued in Archicad and will run when it is closed; "
               "see archicad_list_pending_commands." if queued else ""),
            dialogs,
        )

    # -- sending ------------------------------------------------------------

    def run(self, command: str, port: int, parameters: str, call: Callable[[], Any]) -> Any:
        """Run a dispatch with a time limit, giving up early on a blocking dialog."""
        limit = self.settings.timeout_for(command)
        outcome: dict[str, Any] = {}
        done = threading.Event()
        lock = threading.Lock()
        abandoned: dict[str, str] = {}

        def worker() -> None:
            try:
                outcome["result"] = call()
            except BaseException as exc:  # noqa: BLE001 - reported to the waiting thread
                outcome["error"] = exc
            with lock:
                done.set()
                pending_id = abandoned.get("id")
            if pending_id is not None:
                self._record_late_outcome(pending_id, outcome)

        thread = threading.Thread(target=worker, name=f"TapirCall-{command}", daemon=True)
        started = self._clock()
        thread.start()

        grace = 0 if command in DIALOG_COMMANDS else self.settings.dialog_grace_seconds
        dialog_since: float | None = None
        dialogs: list[DialogInfo] = []
        reason = ""
        while not done.wait(POLL_INTERVAL_SECONDS):
            now = self._clock()
            if grace > 0:
                dialogs = [d for d in self._list_dialogs(port) if d.blocking]
                if dialogs:
                    dialog_since = dialog_since if dialog_since is not None else now
                    if now - dialog_since >= grace:
                        reason = f"a modal dialog kept Archicad busy for {grace:g} s"
                        break
                else:
                    dialog_since = None
            if limit is not None and now - started >= limit:
                reason = f"no answer within the {limit:g} s limit for this command"
                if not dialogs:
                    dialogs = [d for d in self._list_dialogs(port) if d.blocking]
                break

        with lock:
            if done.is_set():
                if "error" in outcome:
                    raise outcome["error"]
                return outcome.get("result")
            entry = self.pending.add(command, port, parameters, reason, dialogs)
            abandoned["id"] = entry.pending_id

        log.error("[%s] Stopped waiting for '%s' on port %s: %s", entry.pending_id, command, port, reason)
        raise ArchicadUnresponsiveError(
            f"Archicad on port {port} did not respond to '{command}': {reason}. {_dialog_report(dialogs)} "
            f"IMPORTANT: the command is still queued inside Archicad and will run when the dialog is closed "
            f"(recorded as {entry.pending_id}). Do NOT resend it; ask the user to close the dialog, then check "
            f"archicad_list_pending_commands for its late outcome.",
            dialogs,
            entry.pending_id,
        )

    def _record_late_outcome(self, pending_id: str, outcome: dict[str, Any]) -> None:
        if "error" in outcome:
            exc = outcome["error"]
            message = str(exc) if isinstance(exc, (APIErrorBase, ToolError)) else type(exc).__name__
            self.pending.finish(pending_id, message[:500], succeeded=False)
        else:
            result = outcome.get("result")
            try:
                text = json.dumps(result, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = repr(result)
            self.pending.finish(pending_id, "succeeded: " + (text if len(text) <= 300 else text[:299] + "…"), True)

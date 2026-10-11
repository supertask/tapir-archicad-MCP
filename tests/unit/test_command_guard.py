import sys
import threading
import time
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from multiconn_archicad.errors import CommandTimeoutError, StandardAPIError

import tapir_archicad_mcp.command_guard as command_guard_module
from tapir_archicad_mcp.command_guard import (
    ArchicadUnresponsiveError,
    CommandGuard,
    GuardSettings,
    is_read_only_command,
    parse_timeout_overrides,
)
from tapir_archicad_mcp.context import command_guard_instance
from tapir_archicad_mcp.dialogs import DialogInfo, is_cancel_like
from tapir_archicad_mcp.tools.custom.functions import archicad_call_tool
from tapir_archicad_mcp.tools.custom.guard_tools import (
    archicad_clear_pending_commands,
    archicad_get_dialogs,
    archicad_list_pending_commands,
)

PORT = 19723
MISSING_ADDON = DialogInfo(
    title="欠落しているアドオン",
    texts=["一部のアドオンでは、プロジェクト要素に関連する追加データが管理されます。"],
    buttons=["OK"],
    windowClass="DGModalDlgClass",
    handle=1234,
)


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(command_guard_module, "POLL_INTERVAL_SECONDS", 0.02)


def _guard(fake_dialogs, **settings) -> CommandGuard:
    guard = CommandGuard(GuardSettings(**settings), dialog_lister=fake_dialogs)
    command_guard_instance.set(guard)
    return guard


class Blocker:
    """A dispatch that hangs until released, like a command queued behind a dialog."""

    def __init__(self, result=None, error=None):
        self.release = threading.Event()
        self.result = result
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.release.wait(10)
        if self.error is not None:
            raise self.error
        return self.result


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# -- settings --------------------------------------------------------------


def test_time_limits_per_command():
    settings = GuardSettings(
        command_timeout_seconds=60,
        long_command_timeout_seconds=900,
        command_timeout_overrides={"elements_get_all_elements": 5, "project_open_project": 0},
    )
    assert settings.timeout_for("elements_get_selected_elements") == 60
    assert settings.timeout_for("navigator_publish_publisher_set") == 900
    assert settings.timeout_for("elements_get_all_elements") == 5
    assert settings.timeout_for("project_open_project") is None


def test_parse_timeout_overrides():
    assert parse_timeout_overrides("") == {}
    assert parse_timeout_overrides(" a_b=5, c_d=0.5 ,") == {"a_b": 5.0, "c_d": 0.5}
    for bad in ("a_b", "=5", "a_b=-1", "a_b=nan", "a_b=x"):
        with pytest.raises(ValueError):
            parse_timeout_overrides(bad)


def test_read_only_classification():
    assert is_read_only_command("elements_get_selected_elements")
    assert is_read_only_command("navigator_get_publisher_set_names")
    assert not is_read_only_command("navigator_publish_publisher_set")
    assert not is_read_only_command("project_open_project")


def test_cancel_like_labels():
    assert is_cancel_like("&Cancel")
    assert is_cancel_like("キャンセル")
    assert not is_cancel_like("OK")


# -- run: time limit, dialogs, late outcome --------------------------------


def test_fast_command_returns_its_result(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=5)
    assert guard.run("elements_get_selected_elements", PORT, "{}", lambda: {"ok": 1}) == {"ok": 1}
    assert guard.pending.summaries() == []


def test_command_errors_propagate_unchanged(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=5)

    def fail():
        raise ToolError("boom")

    with pytest.raises(ToolError, match="boom"):
        guard.run("elements_get_selected_elements", PORT, "{}", fail)


def test_time_limit_abandons_command_and_records_late_success(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=0.2, dialog_grace_seconds=0)
    blocker = Blocker(result={"done": True})

    started = time.monotonic()
    with pytest.raises(ArchicadUnresponsiveError) as raised:
        guard.run("elements_delete_elements", PORT, '{"x":1}', blocker)
    assert time.monotonic() - started < 3
    message = str(raised.value)
    assert "did not respond" in message and "0.2 s limit" in message
    assert "still queued inside Archicad" in message

    [entry] = guard.pending.summaries()
    assert entry["pendingId"] == raised.value.pending_id
    assert entry["status"] == "queued_in_archicad"

    blocker.release.set()
    assert _wait_for(lambda: guard.pending.summaries()[0]["status"] == "executed_late")
    assert '"done": true' in guard.pending.summaries()[0]["outcome"]


def test_late_failure_is_recorded(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=0.2, dialog_grace_seconds=0)
    blocker = Blocker(error=ToolError("element not found"))
    with pytest.raises(ArchicadUnresponsiveError):
        guard.run("elements_delete_elements", PORT, "{}", blocker)
    blocker.release.set()
    assert _wait_for(lambda: guard.pending.summaries()[0]["status"] == "failed_late")
    assert guard.pending.summaries()[0]["outcome"] == "element not found"


def test_blocking_dialog_stops_waiting_early_and_is_reported(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=30, dialog_grace_seconds=0.2)
    fake_dialogs.by_port[PORT] = [MISSING_ADDON]
    blocker = Blocker()

    started = time.monotonic()
    with pytest.raises(ArchicadUnresponsiveError) as raised:
        guard.run("elements_get_selected_elements", PORT, "{}", blocker)
    assert time.monotonic() - started < 3
    message = str(raised.value)
    assert "modal dialog kept Archicad busy" in message
    assert "欠落しているアドオン" in message and "'OK'" in message
    assert raised.value.dialogs == [MISSING_ADDON]
    assert guard.pending.summaries()[0]["dialogs"][0]["title"] == "欠落しているアドオン"
    blocker.release.set()


def test_progress_window_does_not_stop_waiting(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=5, dialog_grace_seconds=0.05)
    fake_dialogs.by_port[PORT] = [DialogInfo(title="Opening", buttons=["Cancel"], blocking=False)]
    blocker = Blocker(result={"ok": True})
    threading.Timer(0.3, blocker.release.set).start()
    assert guard.run("project_open_project", PORT, "{}", blocker) == {"ok": True}


def test_dialog_commands_are_not_stopped_by_their_own_dialog(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=5, dialog_grace_seconds=0.05)
    fake_dialogs.by_port[PORT] = [MISSING_ADDON]
    blocker = Blocker(result={})
    threading.Timer(0.3, blocker.release.set).start()
    assert guard.run("app_show_alert", PORT, "{}", blocker) == {}


# -- preflight -------------------------------------------------------------


def _header(post_command):
    return SimpleNamespace(core=SimpleNamespace(post_command=post_command))


def test_preflight_refuses_when_archicad_reports_a_modal_dialog(fake_dialogs):
    guard = _guard(fake_dialogs)
    fake_dialogs.by_port[PORT] = [MISSING_ADDON]
    sent = []

    def is_alive(command, parameters=None, timeout=None):
        sent.append((command, timeout))
        raise StandardAPIError(
            code=4001, message="Invalid program status (there is an open modal dialog: 欠落しているアドオン)"
        )

    with pytest.raises(ArchicadUnresponsiveError) as raised:
        guard.preflight(_header(is_alive), PORT)
    assert sent == [("API.IsAlive", 5)]
    message = str(raised.value)
    assert "was NOT sent" in message and "欠落しているアドオン" in message
    assert guard.pending.summaries() == []


def test_preflight_uses_archicads_dialog_name_when_windows_cannot_be_read(fake_dialogs):
    guard = _guard(fake_dialogs)

    def is_alive(command, parameters=None, timeout=None):
        raise StandardAPIError(code=4001, message="Invalid program status (there is an open modal dialog: Save)")

    with pytest.raises(ArchicadUnresponsiveError, match="open modal dialog: 'Save'"):
        guard.preflight(_header(is_alive), PORT)


def test_preflight_refuses_when_is_alive_times_out(fake_dialogs):
    guard = _guard(fake_dialogs, preflight_timeout_seconds=1)

    def is_alive(command, parameters=None, timeout=None):
        raise CommandTimeoutError("timed out")

    with pytest.raises(ArchicadUnresponsiveError, match="no answer to API.IsAlive within 1 s"):
        guard.preflight(_header(is_alive), PORT)


def test_preflight_passes_when_alive_and_can_be_disabled(fake_dialogs):
    guard = _guard(fake_dialogs)
    guard.preflight(_header(lambda command, parameters=None, timeout=None: {}), PORT)

    def never(*args, **kwargs):
        raise AssertionError("preflight must not run")

    _guard(fake_dialogs, preflight_timeout_seconds=0).preflight(_header(never), PORT)


def test_preflight_refuses_dialogs_archicad_answers_is_alive_behind(fake_dialogs):
    """Tapir's alerts are Windows message boxes: IsAlive succeeds, commands still queue."""
    guard = _guard(fake_dialogs)
    alive = _header(lambda command, parameters=None, timeout=None: {})
    fake_dialogs.by_port[PORT] = [DialogInfo(title="MCP保留テスト", buttons=["OK"], windowClass="#32770")]
    with pytest.raises(ArchicadUnresponsiveError, match="(?s)NOT sent.*MCP保留テスト"):
        guard.preflight(alive, PORT)

    fake_dialogs.by_port[PORT] = [DialogInfo(title="Find", buttons=["OK"], blocking=False)]
    guard.preflight(alive, PORT)


def test_preflight_ignores_unrelated_errors(fake_dialogs):
    guard = _guard(fake_dialogs)

    def is_alive(command, parameters=None, timeout=None):
        raise StandardAPIError(code=1, message="something else")

    guard.preflight(_header(is_alive), PORT)


# -- duplicates ------------------------------------------------------------


def test_identical_write_is_refused_until_cleared(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=0.1, dialog_grace_seconds=0)
    blocker = Blocker(result={})
    with pytest.raises(ArchicadUnresponsiveError):
        guard.run("elements_delete_elements", PORT, '{"a":1}', blocker)

    with pytest.raises(ToolError, match="still queued in Archicad"):
        guard.check_duplicate("elements_delete_elements", PORT, '{"a":1}')
    guard.check_duplicate("elements_delete_elements", PORT, '{"a":2}')
    guard.check_duplicate("elements_delete_elements", 19724, '{"a":1}')

    # Clearing without ids keeps records that are still queued.
    assert guard.pending.clear() == []
    blocker.release.set()
    assert _wait_for(lambda: guard.pending.summaries()[0]["status"] == "executed_late")
    with pytest.raises(ToolError, match="executed by Archicad after"):
        guard.check_duplicate("elements_delete_elements", PORT, '{"a":1}')

    assert len(guard.pending.clear(port=PORT)) == 1
    guard.check_duplicate("elements_delete_elements", PORT, '{"a":1}')


def test_identical_reads_are_not_refused(fake_dialogs):
    guard = _guard(fake_dialogs, command_timeout_seconds=0.1, dialog_grace_seconds=0)
    blocker = Blocker(result={})
    with pytest.raises(ArchicadUnresponsiveError):
        guard.run("elements_get_selected_elements", PORT, "{}", blocker)
    guard.check_duplicate("elements_get_selected_elements", PORT, "{}")
    blocker.release.set()


def test_finished_records_expire(fake_dialogs):
    now = [0.0]
    guard = CommandGuard(GuardSettings(pending_ttl_seconds=10), dialog_lister=fake_dialogs, clock=lambda: now[0])
    entry = guard.pending.add("elements_delete_elements", PORT, "{}", "test", [])
    now[0] = 100
    assert len(guard.pending.summaries()) == 1  # still queued: never expires
    guard.pending.finish(entry.pending_id, "ok", True)
    assert guard.pending.summaries() == []


# -- through archicad_call_tool ---------------------------------------------



class HangingArchicad:
    """FakeArchicad stand-in whose Tapir command hangs as if behind a dialog."""

    def __init__(self, fake):
        self.fake = fake
        self.release = threading.Event()
        self.sent = 0

    def post_tapir_command(self, command, parameters=None):
        self.sent += 1
        self.release.wait(10)
        return {"executed": command}


@pytest.mark.asyncio
async def test_call_tool_reports_dialog_and_refuses_resend(fake_archicad, fake_dialogs):
    _guard(fake_dialogs, command_timeout_seconds=30, dialog_grace_seconds=0.2, preflight_timeout_seconds=0)
    hanging = HangingArchicad(fake_archicad)
    fake_archicad.core.post_tapir_command = hanging.post_tapir_command
    fake_dialogs.by_port[fake_archicad.port] = [MISSING_ADDON]
    arguments = {"port": fake_archicad.port, "params": {"elements": [{"elementId": {"guid": "1" * 8 + "-1111-1111-1111-" + "1" * 12}}]}}

    with pytest.raises(ToolError) as raised:
        await archicad_call_tool("elements_delete_elements", arguments)
    assert "欠落しているアドオン" in str(raised.value)

    with pytest.raises(ToolError, match="Duplicate refused"):
        await archicad_call_tool("elements_delete_elements", arguments)
    assert hanging.sent == 1

    pending = archicad_list_pending_commands()["pendingCommands"]
    assert [p["command"] for p in pending] == ["elements_delete_elements"]

    fake_dialogs.by_port.clear()
    hanging.release.set()
    assert _wait_for(lambda: archicad_list_pending_commands()["pendingCommands"][0]["status"] == "executed_late")

    # allow_duplicate sends it anyway; clearing the record also unblocks it.
    assert await archicad_call_tool("elements_delete_elements", arguments, allow_duplicate=True) == {
        "executed": "DeleteElements"
    }
    assert archicad_clear_pending_commands()["removed"] == [pending[0]["pendingId"]]
    assert await archicad_call_tool("elements_delete_elements", arguments) == {"executed": "DeleteElements"}
    assert hanging.sent == 3


@pytest.mark.asyncio
async def test_call_tool_preflight_sends_nothing_behind_an_open_dialog(fake_archicad, fake_dialogs):
    _guard(fake_dialogs)
    fake_dialogs.by_port[fake_archicad.port] = [MISSING_ADDON]

    def is_alive(command, parameters=None, timeout=None):
        raise StandardAPIError(
            code=4001, message="Invalid program status (there is an open modal dialog: 欠落しているアドオン)"
        )

    fake_archicad.core.post_command = is_alive
    with pytest.raises(ToolError, match="was NOT sent"):
        await archicad_call_tool("elements_get_selected_elements", {"port": fake_archicad.port})
    assert fake_archicad.calls == []
    assert archicad_list_pending_commands()["pendingCommands"] == []


@pytest.mark.asyncio
async def test_get_dialogs_tool(fake_archicad, monkeypatch):
    from tapir_archicad_mcp.tools.custom import guard_tools as functions

    monkeypatch.setattr(functions, "dialogs_supported", lambda: True)
    monkeypatch.setattr(functions, "list_dialogs_for_port", lambda port: [MISSING_ADDON])
    result = await archicad_get_dialogs()
    assert result["supported"] is True
    [instance] = result["instances"]
    assert instance["port"] == fake_archicad.port and instance["blocked"] is True
    assert instance["dialogs"][0]["buttons"] == ["OK"]

    monkeypatch.setattr(functions, "dialogs_supported", lambda: False)
    assert (await archicad_get_dialogs(fake_archicad.port))["supported"] is False


# -- the real Win32 lookup --------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 dialogs")
def test_win32_lookup_reads_a_real_message_box():
    import ctypes
    import os

    from tapir_archicad_mcp.dialogs import list_dialogs_for_pid

    user32 = ctypes.windll.user32
    title = f"tapir-mcp-test-{os.getpid()}"
    box = threading.Thread(
        target=lambda: user32.MessageBoxW(None, "Body text of the test dialog", title, 0x1), daemon=True
    )  # MB_OKCANCEL
    box.start()
    try:
        assert _wait_for(lambda: any(d.title == title for d in list_dialogs_for_pid(os.getpid())))
        [dialog] = [d for d in list_dialogs_for_pid(os.getpid()) if d.title == title]
        assert dialog.windowClass == "#32770"
        assert "OK" in dialog.buttons and ("Cancel" in dialog.buttons or "キャンセル" in dialog.buttons)
        assert any("Body text of the test dialog" in text for text in dialog.texts)
        assert dialog.blocking is True
    finally:
        hwnd = user32.FindWindowW("#32770", title)
        if hwnd:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        box.join(5)
    assert not box.is_alive()

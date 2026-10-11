"""Read-only inspection of the modal dialogs an Archicad process is showing.

While Archicad shows a modal dialog (a missing add-on warning, a save prompt, a
library loading report...) it does not process JSON API commands: they wait in
its queue until somebody closes the dialog. This module finds those dialogs so
the server can tell the user what is blocking Archicad instead of waiting
silently.

Only Win32 calls through ctypes are used, so there is no extra dependency. On
other platforms every lookup returns an empty result.
"""

import logging
import sys
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

# Window classes Archicad (DG library) and the Windows common dialogs use for
# modal dialogs. Palettes and the main frame use other DG* classes.
MODAL_DIALOG_CLASSES = ("DGModalDlgClass", "#32770")
# A dialog whose buttons are all of these (or which has no button at all) is
# treated as a progress window rather than a question waiting for an answer.
CANCEL_LIKE_BUTTONS = {"cancel", "キャンセル", "中止", "stop", "停止", "abort"}
MAX_TEXTS = 20
MAX_TEXT_CHARS = 1000


@dataclass
class DialogInfo:
    """One visible modal dialog of an Archicad process."""

    title: str
    texts: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    windowClass: str = ""
    handle: int = 0
    blocking: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        parts = [f"'{self.title}'"]
        if self.buttons:
            parts.append("buttons: " + ", ".join(f"'{b}'" for b in self.buttons))
        if self.texts:
            text = " / ".join(self.texts)
            parts.append("text: " + (text if len(text) <= 300 else text[:299] + "…"))
        return " - ".join(parts)


def _clean_label(text: str) -> str:
    # Win32 button labels carry '&' mnemonics, e.g. '&Save'.
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").strip()


def is_cancel_like(label: str) -> bool:
    return _clean_label(label).lower() in CANCEL_LIKE_BUTTONS


def find_pid_for_port(port: int) -> int | None:
    """Return the PID of the process listening on an Archicad API port."""
    try:
        from multiconn_archicad.orchestration.system.process_utils import find_pid_by_port
    except ImportError:  # pragma: no cover - multiconn layout changed
        log.warning("Cannot import find_pid_by_port from multiconn_archicad.")
        return None
    return find_pid_by_port(port)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _GW_OWNER = 4

    def _window_text(hwnd: int) -> str:
        length = _user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def _window_class(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def _window_pid(hwnd: int) -> int:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _top_level_windows(pid: int) -> list[int]:
        windows: list[int] = []

        def callback(hwnd, _):
            if _window_pid(hwnd) == pid and _user32.IsWindowVisible(hwnd):
                windows.append(hwnd)
            return True

        _user32.EnumWindows(_ENUM_PROC(callback), 0)
        return windows

    def _child_windows(hwnd: int) -> list[int]:
        children: list[int] = []

        def callback(child, _):
            children.append(child)
            return True

        _user32.EnumChildWindows(hwnd, _ENUM_PROC(callback), 0)
        return children

    def _read_dialog(hwnd: int) -> DialogInfo:
        texts: list[str] = []
        buttons: list[str] = []
        for child in _child_windows(hwnd):
            if not _user32.IsWindowVisible(child):
                continue
            text = _window_text(child).strip()
            if not text:
                continue
            child_class = _window_class(child)
            if child_class == "Button":
                buttons.append(_clean_label(text))
            elif child_class == "Static" and len(texts) < MAX_TEXTS:
                texts.append(text[:MAX_TEXT_CHARS])
        answerable = [b for b in buttons if not is_cancel_like(b)]
        return DialogInfo(
            title=_window_text(hwnd),
            texts=texts,
            buttons=buttons,
            windowClass=_window_class(hwnd),
            handle=int(hwnd),
            blocking=bool(answerable),
        )

    def list_dialogs_for_pid(pid: int) -> list[DialogInfo]:
        dialogs = []
        for hwnd in _top_level_windows(pid):
            if _window_class(hwnd) in MODAL_DIALOG_CLASSES and _user32.IsWindowEnabled(hwnd):
                dialogs.append(_read_dialog(hwnd))
        return dialogs


else:

    def list_dialogs_for_pid(pid: int) -> list[DialogInfo]:
        return []


def dialogs_supported() -> bool:
    return sys.platform == "win32"


_pid_cache: dict[int, int] = {}


def _cached_pid(port: int) -> int | None:
    # Scanning every TCP connection is slow; the port keeps its process while it runs.
    pid = _pid_cache.get(port)
    if pid is not None:
        import psutil

        if psutil.pid_exists(pid):
            return pid
    pid = find_pid_for_port(port)
    if pid is None:
        _pid_cache.pop(port, None)
    else:
        _pid_cache[port] = pid
    return pid


def list_dialogs_for_port(port: int) -> list[DialogInfo]:
    """List the modal dialogs of the Archicad listening on a port; never raises."""
    try:
        pid = _cached_pid(port)
        if pid is None:
            return []
        return list_dialogs_for_pid(pid)
    except Exception:
        log.exception("Dialog inspection failed for port %s", port)
        return []

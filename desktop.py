"""Gestion du bureau virtuel Windows dédié au navigateur de Rewards.

Le navigateur est lancé hors écran, puis ses fenêtres sont déplacées sur un
bureau virtuel "Microsoft Rewards" créé pour l'occasion et supprimé à la fin.
Tu peux continuer à utiliser ton bureau normalement pendant ce temps.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

from pyvda import AppView, VirtualDesktop, get_virtual_desktops

log = logging.getLogger("rewards")

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_SHOWNOACTIVATE = 4

DESKTOP_NAME = "Microsoft Rewards"


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def top_level_windows() -> list[int]:
    hwnds: list[int] = []

    def cb(hwnd, _):
        hwnds.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return hwnds


def browser_windows(pid: int) -> list[int]:
    """Fenêtres principales (visibles, avec titre) du processus navigateur."""
    return [
        h for h in top_level_windows()
        if window_pid(h) == pid
        and user32.IsWindowVisible(h)
        and window_class(h) == "Chrome_WidgetWin_1"
        and window_title(h)
    ]


def find_window_by_title(token: str, timeout: float = 15) -> int | None:
    end = time.time() + timeout
    while time.time() < end:
        for h in top_level_windows():
            if token in window_title(h):
                return h
        time.sleep(0.2)
    return None


class HiddenDesktop:
    """Bureau virtuel dédié : créé à l'entrée, supprimé à la sortie."""

    def __init__(self, name: str = DESKTOP_NAME):
        self.name = name
        self.desktop: VirtualDesktop | None = None
        self.created = False
        self._pid: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        for d in get_virtual_desktops():
            if d.name == self.name:
                self.desktop = d
                break
        if self.desktop is None:
            self.desktop = VirtualDesktop.create()
            self.created = True
            try:
                self.desktop.rename(self.name)
            except Exception:  # renommage non supporté sur certaines versions
                pass
        log.info("Bureau virtuel « %s » prêt (n°%d)", self.name, self.desktop.number)
        return self

    def adopt(self, hwnd: int):
        """Déplace la fenêtre et toutes les futures fenêtres de son processus sur le bureau dédié."""
        self._pid = window_pid(hwnd)
        self._move(hwnd)
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _move(self, hwnd: int):
        try:
            view = AppView(hwnd=hwnd)
            if view.desktop_id != self.desktop.id:
                view.move(self.desktop)
        except Exception as e:
            log.debug("Déplacement de la fenêtre %s impossible : %s", hwnd, e)
            return
        # La fenêtre a été lancée hors écran : on la replace normalement,
        # sur le bureau caché, pour que la page soit rendue correctement.
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.left < -1000 or rect.top < -1000:
            user32.SetWindowPos(hwnd, 0, 0, 0, 1366, 900, SWP_NOZORDER | SWP_NOACTIVATE)

    def _watch(self):
        while not self._stop.wait(0.5):
            try:
                for h in browser_windows(self._pid):
                    self._move(h)
            except Exception as e:
                log.debug("Surveillance des fenêtres : %s", e)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self.created and self.desktop is not None:
            try:
                current = VirtualDesktop.current()
                fallback = current if current.id != self.desktop.id else VirtualDesktop(1)
                self.desktop.remove(fallback=fallback)
                log.info("Bureau virtuel « %s » supprimé", self.name)
            except Exception as e:
                log.warning("Impossible de supprimer le bureau virtuel : %s", e)
        return False

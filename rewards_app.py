"""Application Microsoft Rewards pour ordinateur (Microsoft Store) : les quêtes qui lui sont réservées.

La quête « Exclusivité de l'application Rewards » (une recherche Bing par jour) est verrouillée dans Edge :
Microsoft ne la débloque et ne la valide que dans l'application, une fenêtre WebView2 qui affiche
rewards.bing.com et signale ses requêtes comme venant de l'application. On ne l'imite pas : on lance la
vraie application, sans fenêtre, avec un port de débogage local (127.0.0.1), et on clique ses cartes
comme dans Edge. Seules ces quêtes y sont faites ; tout le reste se fait dans Edge.
"""
import contextlib
import csv
import ctypes
import ctypes.wintypes as wt
import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright
from playwright.sync_api import Error as PWError

from quests import LINK_FIELDS, Rewards, is_true, offers, page_data, pause, skipped_link
from state import State

log = logging.getLogger("rewards")

# « Microsoft Rewards » du Microsoft Store (9MW3CMW8JKSD).
PACKAGE_FAMILY = "Microsoft.24183918CCDA_8wekyb3d8bbwe"
EXE_NAME = "microsoft-rewards-desktop.exe"
# Options que WebView2 ajoute à celles de l'application au lancement.
WEBVIEW2_ARGS = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"


def executable() -> Path | None:
    """Chemin de l'application installée, sinon None."""
    k32 = ctypes.windll.kernel32
    count, size = wt.UINT(0), wt.UINT(0)
    k32.GetPackagesByPackageFamily(PACKAGE_FAMILY, ctypes.byref(count), None, ctypes.byref(size), None)
    if not count.value:
        return None
    names = (wt.LPWSTR * count.value)()
    buf = ctypes.create_unicode_buffer(size.value)
    if k32.GetPackagesByPackageFamily(PACKAGE_FAMILY, ctypes.byref(count), names, ctypes.byref(size), buf):
        return None
    for name in names:
        length = wt.UINT(0)
        k32.GetPackagePathByFullName(name, ctypes.byref(length), None)
        path = ctypes.create_unicode_buffer(length.value)
        if not k32.GetPackagePathByFullName(name, ctypes.byref(length), path):
            exe = Path(path.value) / EXE_NAME
            if exe.exists():
                return exe
    return None


def running() -> list[int]:
    """PID des instances de l'application en cours d'exécution."""
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace",
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return [int(row[1]) for row in csv.reader(out.splitlines()) if len(row) > 1 and row[0].lower() == EXE_NAME]


def kill(pid: int):
    """Ferme l'application et ses processus WebView2."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(20):
        if pid not in running():
            break
        time.sleep(0.5)
    time.sleep(1)  # le temps que WebView2 libère le dossier de profil de l'application


def launch(exe: Path, port: int | None = None) -> subprocess.Popen:
    """Lance l'application sans fenêtre (comme au démarrage de Windows), avec un port de débogage si `port`."""
    env = dict(os.environ)
    env.pop(WEBVIEW2_ARGS, None)
    if port:
        env[WEBVIEW2_ARGS] = f"--remote-debugging-port={port}"
    return subprocess.Popen([str(exe), "--hidden"], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def connect(pw: Playwright, port: int, timeout: float = 30) -> Browser:
    end = time.monotonic() + timeout
    while True:
        try:
            return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except PWError:
            if time.monotonic() > end:
                raise
            time.sleep(1)


def main_view(browser: Browser, timeout: float = 30) -> tuple[BrowserContext, Page]:
    """Vue principale de l'application : celle qui affiche rewards.bing.com (ou la connexion Microsoft)."""
    end = time.monotonic() + timeout
    while True:
        pages = [(ctx, p) for ctx in browser.contexts for p in ctx.pages]
        for ctx, page in pages:
            host = urlsplit(page.url).hostname or ""
            if host == "rewards.bing.com" or host.startswith("login."):
                return ctx, page
        if time.monotonic() > end:
            raise RuntimeError("vue Rewards introuvable dans l'application")
        if pages:
            pages[0][1].wait_for_timeout(1000)  # laisse Playwright recevoir les vues créées entre-temps
        else:
            time.sleep(1)


@contextlib.contextmanager
def opened(pw: Playwright, exe: Path):
    """L'application lancée sans fenêtre et pilotable : renvoie (contexte, vue principale).

    Elle n'accepte qu'une instance : si elle était déjà ouverte, elle est fermée le temps des quêtes,
    puis relancée normalement (sans port de débogage) à la fin.
    """
    before = running()
    if before:
        log.info("Application Rewards déjà ouverte : relancée le temps des quêtes")
    for pid in before:
        kill(pid)
    port = free_port()
    proc = launch(exe, port)
    browser = None
    try:
        browser = connect(pw, port)
        yield main_view(browser)
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()  # déconnexion seulement : c'est kill() qui ferme l'application
        kill(proc.pid)
        if before:
            launch(exe)


def bing_only(o: dict) -> bool:
    """Vrai si tous les liens de l'offre mènent à Bing, que l'application ouvre dans sa propre fenêtre
    (les autres liens s'ouvriraient dans le navigateur par défaut)."""
    hosts = [urlsplit(str(o[k])).hostname or "" for k in LINK_FIELDS if o.get(k)]
    return bool(hosts) and all(h == "bing.com" or h.endswith(".bing.com") for h in hosts)


class AppRewards(Rewards):
    """Les quêtes du site, faites dans la vue principale de l'application."""

    def open_card(self, loc: Locator, blank: bool) -> Page | None:
        # Les recherches Bing s'ouvrent dans le navigateur intégré de l'application, une vue qui existe
        # déjà (et que le script ne ferme pas) : pas de nouvel onglet à attendre.
        loc.click(timeout=15000)
        pause(self.page, 4, 7)
        return None

    @staticmethod
    def doable(items, label) -> list[dict]:
        """Tâches ni terminées ni verrouillées dont les liens restent dans l'application. Le filtre de texte
        d'Edge n'est pas utile ici (et écarterait une tâche comme « Installation artistique »)."""
        todo = []
        for o in items:
            if is_true(o.get("isCompleted")) or is_true(o.get("isLocked")):
                continue
            if skipped_link(o) or not bing_only(o):
                log.info("  ignorée (lien hors de l'application) : %s", label(o))
                continue
            todo.append(o)
        return todo

    def verify(self, url: str, ids: list[str]) -> dict[str, bool]:
        """Une tâche de quête validée perd son lien et disparaît des données de la page : absente d'une page
        qui contient encore d'autres tâches, elle est validée."""
        status = super().verify(url, ids)
        try:
            loaded = bool(offers(page_data(self.page)))
        except PWError:
            loaded = False
        return {i: status.get(i, True) for i in ids} if loaded else status


def quests(pw: Playwright, state: State, urls: list[str]) -> list[str]:
    """Fait dans l'application les quêtes `urls` qui lui sont réservées ; renvoie les actions validées."""
    if not urls:
        return []
    exe = executable()
    if exe is None:
        log.info("Application Rewards pour ordinateur non installée : quête(s) exclusive(s) ignorée(s)")
        return []
    with opened(pw, exe) as (ctx, page):
        r = AppRewards(ctx, state, page)
        if not r.logged_in():
            log.warning("L'application Rewards n'est pas connectée à ton compte : ouvre-la une fois pour te connecter.")
            return []
        r.do_quests(urls)
        return r.done

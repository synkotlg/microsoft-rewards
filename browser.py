"""Lancement de Microsoft Edge avec un profil dédié (connexion conservée entre les lancements)."""
import contextlib
import logging
import uuid
from pathlib import Path

from playwright.sync_api import BrowserContext, Playwright

from desktop import HiddenDesktop, find_window_by_title

log = logging.getLogger("rewards")

PROFILE_DIR = Path(__file__).parent / "edge-profile"

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{v} Mobile Safari/537.36 EdgA/{v}"
)

BASE_ARGS = [
    # La fenêtre est sur un autre bureau : on empêche Edge de la considérer
    # comme masquée et de ralentir/mettre en pause les pages.
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1366,900",
]


@contextlib.contextmanager
def edge(pw: Playwright, visible: bool = False, mobile: bool = False):
    """Ouvre Edge. Si visible=False, la fenêtre vit sur un bureau virtuel dédié."""
    PROFILE_DIR.mkdir(exist_ok=True)
    args = list(BASE_ARGS)
    if not visible:
        args.append("--window-position=-32000,-32000")

    options = dict(
        user_data_dir=str(PROFILE_DIR),
        channel="msedge",
        headless=False,
        args=args,
        no_viewport=True,
        locale="fr-FR",
        # Aucun fichier n'est jamais téléchargé (les téléchargements sont annulés).
        accept_downloads=False,
    )
    if mobile:
        options.update(
            no_viewport=False,
            viewport={"width": 412, "height": 915},
            device_scale_factor=2.625,
            is_mobile=True,
            has_touch=True,
            user_agent=MOBILE_UA.format(v=_edge_version()),
        )

    desk = contextlib.nullcontext() if visible else HiddenDesktop()
    with desk:
        ctx: BrowserContext = pw.chromium.launch_persistent_context(**options)
        try:
            if not visible:
                token = f"rewards-{uuid.uuid4().hex[:8]}"
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_content(f"<title>{token}</title>")
                hwnd = find_window_by_title(token)
                if hwnd:
                    desk.adopt(hwnd)
                else:
                    log.warning("Fenêtre Edge introuvable, elle reste hors écran")
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                ctx.close()


def _edge_version() -> str:
    """Version d'Edge installée (pour un user-agent mobile crédible)."""
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        with contextlib.suppress(OSError):
            with winreg.OpenKey(root, r"Software\Microsoft\Edge\BLBeacon") as k:
                return winreg.QueryValueEx(k, "version")[0]
    return "140.0.0.0"

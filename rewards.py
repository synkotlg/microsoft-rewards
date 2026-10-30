"""Microsoft Rewards : fait automatiquement les quêtes quotidiennes dans Edge.

    python rewards.py --setup     première connexion (fenêtre visible)
    python rewards.py             exécution normale, sur un bureau virtuel caché
    python rewards.py --visible   exécution normale mais fenêtre visible (débogage)
    python rewards.py --dump      sauvegarde les pages Rewards dans dump/ (débogage)
"""
import argparse
import ctypes
import json
import logging
import math
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import search
from browser import edge
from quests import DASHBOARD, EARN, AccountFlagged, Rewards, offers, page_data
from state import State
from telegram_notify import TelegramNotifier, load_env_file

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
DUMP_DIR = ROOT / "dump"
ENV_FILE = ROOT / ".env"

# Pause aléatoire (en secondes) entre deux recherches Bing.
SEARCH_DELAY = (15, 45)

log = logging.getLogger("rewards")


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(logging.DEBUG)
    if sys.stdout:  # absent quand lancé par pythonw (tâche planifiée)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)
        log.addHandler(console)
    logfile = logging.FileHandler(LOG_DIR / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(fmt)
    log.addHandler(logfile)
    for old in sorted(LOG_DIR.glob("*.log"))[:-30]:  # on garde 30 jours de journaux
        old.unlink(missing_ok=True)


def single_instance():
    """Empêche deux exécutions simultanées (elles partagent le même profil Edge)."""
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MicrosoftRewardsAuto")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def alert(message: str):
    ctypes.windll.user32.MessageBoxW(0, message, "Microsoft Rewards", 0x30 | 0x40000)


def setup():
    """Ouvre Edge (visible) pour que l'utilisateur se connecte une fois à son compte Microsoft."""
    print(
        "\n  Une fenêtre Edge va s'ouvrir.\n"
        "  1. Connecte-toi à ton compte Microsoft (coche « Rester connecté »).\n"
        "  2. Attends d'arriver sur ton tableau de bord Rewards.\n"
        "  3. Ferme la fenêtre Edge : la connexion est enregistrée.\n"
    )
    with sync_playwright() as pw:
        with edge(pw, visible=True) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(DASHBOARD)
            while ctx.pages:  # on attend que l'utilisateur ferme la fenêtre
                try:
                    ctx.pages[0].wait_for_timeout(1000)
                except Exception:
                    break
        with edge(pw) as ctx:
            try:
                ok = Rewards(ctx).logged_in()
            except AccountFlagged as flag:
                print(f"  Connexion enregistrée, mais Microsoft Rewards signale ton compte : {flag}\n")
                return
    print("  Connexion enregistrée ✓\n" if ok else "  Pas connecté : relance setup.bat.\n")


def format_duration(seconds):
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"


def format_summary(start, end, gained, done, mobile_searches, elapsed):
    """Message Telegram de fin d'exécution (français, lu par l'utilisateur)."""
    note = ", hors recherches mobiles" if mobile_searches else ""
    return (f"Terminé en {format_duration(elapsed)} : {start} → {end} points (+{gained}{note}). "
            f"Actions validées : {done}.")


def run(visible: bool, with_search: bool, notify) -> int:
    notify("Lancement" + ("" if with_search else " (sans les recherches Bing)")
           + (" (fenêtre visible)" if visible else ""))
    code, message, summary = session(visible, with_search)
    notify(summary)
    # L'alerte (bloquante) n'est affichée qu'une fois Edge et le bureau caché refermés.
    if message:
        alert(message)
    return code


def session(visible: bool, with_search: bool) -> tuple[int, str | None, str]:
    """Fait les quêtes. Renvoie (code de sortie, message d'alerte éventuel, résumé Telegram)."""
    state = State()
    mobile_searches = 0
    started = time.monotonic()
    with sync_playwright() as pw:
        with edge(pw, visible=visible) as ctx:
            r = Rewards(ctx, state)
            try:
                if not r.logged_in():
                    log.error("Pas connecté au compte Microsoft : lance setup.bat pour te reconnecter.")
                    return (2, "Ta session Microsoft Rewards a expiré.\n\nLance setup.bat pour te reconnecter.",
                            "Échec : la session Microsoft a expiré, aucune quête faite (lance setup.bat).")
                start = r.balance(page_data(r.page))
                log.info("Solde de départ : %s points", start)

                steps = [
                    ("Sections du tableau de bord", r.discover_sections),
                    ("Ensemble du jour", r.daily_set),
                    ("Autres activités", r.more_activities),
                    ("Recherches thématiques", r.explore_on_bing),
                    ("Quêtes", r.quests),
                ]
                counters = {}
                if with_search:
                    steps.append(("Recherches Bing", lambda: counters.update(r.searches(SEARCH_DELAY))))
                steps.append(("Points à réclamer", r.claim))
                for name, step in steps:
                    log.info("── %s", name)
                    try:
                        step()
                    except AccountFlagged:
                        raise
                    except Exception:
                        log.exception("Erreur pendant « %s » (on continue)", name)

                try:
                    end = r.balance(r.goto(DASHBOARD))
                except AccountFlagged:
                    raise
                except Exception:
                    log.exception("Lecture du solde final impossible")
                    end = None
            except AccountFlagged as flag:
                log.error("Compte signalé par Microsoft (%s) : arrêt immédiat, rien d'autre n'est fait.", flag)
                return (3, (f"Microsoft Rewards signale ton compte :\n\n{flag}\n\n"
                            "Le script s'est arrêté sans rien faire. Il s'arrêtera de la même façon "
                            "tant que cet avertissement sera présent."),
                        f"Compte signalé par Microsoft : {flag}. Arrêt immédiat, rien d'autre n'a été fait.")

        mobile = counters.get("mobile") or {}
        if with_search and mobile.get("max", 0) > mobile.get("progress", 0):
            log.info("── Recherches mobiles : %s/%s points", mobile.get("progress"), mobile.get("max"))
            try:
                with edge(pw, visible=visible, mobile=True) as mctx:
                    mobile_searches = math.ceil((mobile["max"] - mobile.get("progress", 0)) / 3)
                    search.run(mctx.pages[0], mobile_searches, SEARCH_DELAY)
            except Exception:
                log.exception("Erreur pendant les recherches mobiles")

    gained = (end - start) if isinstance(start, int) and isinstance(end, int) else "?"
    log.info("Terminé : %s → %s points (+%s%s). Actions validées : %d", start, end, gained,
             ", hors recherches mobiles" if mobile_searches else "", len(r.done))
    return 0, None, format_summary(start, end, gained, len(r.done), mobile_searches,
                                   time.monotonic() - started)


def dump(visible: bool):
    """Sauvegarde les pages Rewards et leurs offres (pour le débogage)."""
    DUMP_DIR.mkdir(exist_ok=True)
    with sync_playwright() as pw, edge(pw, visible=visible) as ctx:
        r = Rewards(ctx)
        for name, url in [("dashboard", DASHBOARD), ("earn", EARN)]:
            objs = r.goto(url, check=False)
            log.info("URL : %s", r.page.url)
            (DUMP_DIR / f"{name}.html").write_text(r.page.content(), encoding="utf-8")
            (DUMP_DIR / f"{name}.offers.json").write_text(
                json.dumps(offers(objs), indent=1, ensure_ascii=False), encoding="utf-8")
            r.page.screenshot(path=DUMP_DIR / f"{name}.png", full_page=True)
    log.info("Dump écrit dans %s", DUMP_DIR)


def main():
    parser = argparse.ArgumentParser(description="Quêtes Microsoft Rewards automatiques")
    parser.add_argument("--setup", action="store_true", help="première connexion au compte Microsoft")
    parser.add_argument("--visible", action="store_true", help="fenêtre visible au lieu du bureau caché")
    parser.add_argument("--no-search", action="store_true", help="ne fait pas les recherches Bing")
    parser.add_argument("--dump", action="store_true", help="sauvegarde les pages Rewards pour le débogage")
    parser.add_argument("--notify-name", default="microsoft-rewards",
                        help="nom du projet mis devant chaque message Telegram (défaut : microsoft-rewards)")
    parser.add_argument("--env-file", default=str(ENV_FILE),
                        help="fichier KEY=VALUE chargé au démarrage, les vraies variables d'environnement "
                             "l'emportent (défaut : .env à côté de rewards.py)")
    args = parser.parse_args()

    setup_logging()
    loaded = load_env_file(args.env_file)
    if loaded:
        log.info("Variables chargées depuis %s : %s", args.env_file, ", ".join(loaded))
    mutex = single_instance()
    if mutex is None:
        log.info("Déjà en cours d'exécution, on s'arrête.")
        return 0
    if args.setup:
        setup()
        return 0
    if args.dump:
        dump(args.visible)
        return 0
    # Notifications Telegram (silencieuses si TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ne sont pas définis).
    notifier = TelegramNotifier.from_env(prefix=f"[{args.notify_name}] ")
    notify = notifier.send if notifier else (lambda text: False)
    try:
        return run(args.visible, with_search=not args.no_search, notify=notify)
    except Exception as err:
        log.exception("Erreur inattendue")
        notify(f"Échec inattendu : {type(err).__name__}: {err} (voir les journaux).")
        return 1


if __name__ == "__main__":
    sys.exit(main())

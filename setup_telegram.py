#!/usr/bin/env python3
"""Create a Telegram bot from your browser and save TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env.

Opens Telegram Web in a browser window driven by Playwright, asks @BotFather for
a new bot, starts it, then reads your chat id from the Bot API. Every step can
also be done by hand in that window: the script keeps watching the page and
picks the token up as soon as it appears.

  python -m pip install playwright      once (Edge is used on Windows)
  python setup_telegram.py              writes .env next to this script
  python setup_telegram.py --env-file ../other/.env --token 123:ABC   reuse an existing bot

Only this setup needs Playwright; the bot and telegram_notify.py stay standard library.
"""

import argparse
import os
import random
import re
import sys
import time
from pathlib import Path

from telegram_notify import TelegramNotifier, load_env_file

WEB = "https://web.telegram.org/k/"
TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{35}\b")
MESSAGE_INPUT = ".input-message-input[contenteditable='true'], #editable-message-text, [contenteditable='true']"
START_BUTTON = re.compile(r"^\s*(start|d[ée]marrer|commencer|restart)\s*$", re.I)

# BotFather always answers in English, whatever the interface language.
ASK_NAME = "choose a name for your bot"
ASK_USERNAME = "choose a username for your bot"
USERNAME_REFUSED = ("already taken", "username is invalid")


class SetupError(Exception):
    pass


def find_new_token(text, known):
    tokens = [t for t in TOKEN_RE.findall(text) if t not in known]
    return tokens[-1] if tokens else None


def make_username(base, rng=random):
    """Bot username: starts with a letter, letters/digits/underscores, ends with "bot", 32 chars max."""
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_")[:20] or "notify"
    if not stem[0].isalpha():
        stem = ("b" + stem)[:20]
    return f"{stem}_{rng.randint(1000, 9999)}_bot"


def mask(token):
    return f"{token.split(':')[0]}:...{token[-4:]}"


def update_env_file(path, values):
    """Set KEY=VALUE pairs in a .env file, keeping every other line."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    remaining = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        key = key[len("export "):].strip() if key.startswith("export ") else key
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{key}={value}" for key, value in remaining.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)


class TelegramWeb:
    """Thin layer over the Telegram Web page. Selectors live here so a UI change is a one-place fix."""

    def __init__(self, page, base=WEB):
        self.page = page
        self.base = base

    def open_chat(self, username):
        self.page.goto(f"{self.base}#@{username}")

    def text(self):
        return self.page.evaluate("() => document.body.innerText")

    def input_ready(self):
        try:
            return self.page.locator(MESSAGE_INPUT).first.is_visible()
        except Exception:
            return False

    def send(self, message):
        self.page.locator(MESSAGE_INPUT).first.click(timeout=5000)
        self.page.keyboard.type(message)
        self.page.keyboard.press("Enter")

    def press_start(self):
        button = self.page.get_by_role("button", name=START_BUTTON)
        if not button.count():
            return False
        button.first.click(timeout=5000)
        return True

    def wait(self, seconds):
        self.page.wait_for_timeout(seconds * 1000)


class Setup:
    def __init__(self, web, api=TelegramNotifier, out=print, timeout=600, rng=random, clock=time.monotonic):
        self.web = web
        self.api = api
        self.out = out
        self.timeout = timeout
        self.rng = rng
        self.clock = clock

    def _wait_until(self, predicate, timeout, step=1):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            result = predicate()
            if result:
                return result
            self.web.wait(step)
        return None

    def open_botfather(self):
        self.web.open_chat("BotFather")
        if not self._wait_until(self.web.input_ready, 10):
            self.out("Connecte-toi a Telegram dans la fenetre du navigateur (QR code avec ton telephone).")
            self.out("Si la conversation @BotFather ne s'ouvre pas ensuite, ouvre-la a la main.")
            if not self._wait_until(self.web.input_ready, self.timeout, step=2):
                raise SetupError("la conversation @BotFather ne s'est pas ouverte")
        self.web.wait(3)  # let the chat history render

    def _send(self, message, phrases, known, timeout=30):
        """Send a message to BotFather, return the expected phrase it answered with, or "token"."""
        before = self.web.text().lower()
        counts = {p: before.count(p) for p in phrases}
        self.web.send(message)

        def answered():
            current = self.web.text()
            if find_new_token(current, known):
                return "token"
            return next((p for p in phrases if current.lower().count(p) > counts[p]), None)

        reply = self._wait_until(answered, timeout)
        if not reply:
            raise SetupError(f"pas de reponse de BotFather a {message!r}")
        return reply

    def _auto_create(self, name, username_base, known):
        self._send("/newbot", [ASK_NAME], known)
        self._send(name, [ASK_USERNAME], known)
        for _ in range(5):
            username = make_username(username_base, self.rng)
            self.out(f"Username propose : @{username}")
            if self._send(username, list(USERNAME_REFUSED), known) == "token":
                return find_new_token(self.web.text(), known)
        raise SetupError("aucun username accepte apres 5 essais")

    def create_bot(self, name, username_base):
        # Tokens of bots created earlier may be visible in the chat history: ignore them.
        known = set(TOKEN_RE.findall(self.web.text()))
        try:
            return self._auto_create(name, username_base, known)
        except SetupError as err:
            self.out(f"Creation automatique interrompue : {err}.")
        self.out("Termine a la main dans la fenetre : /newbot, un nom, puis un username finissant par 'bot'.")
        self.out("J'ecoute la page et je recupere le token des qu'il s'affiche...")
        token = self._wait_until(lambda: find_new_token(self.web.text(), known), self.timeout, step=2)
        if not token:
            raise SetupError("aucun token detecte dans la page")
        return token

    def start_bot(self, token):
        api = self.api(token, None)
        username = api.me()["username"]
        self.out(f"Bot : @{username}")
        self.web.open_chat(username)
        self.web.wait(3)
        if not self.web.press_start():
            if self.web.input_ready():
                self.web.send("/start")
            else:
                self.out(f"Clique sur DEMARRER dans la conversation avec @{username}.")

        def private_chat():
            try:
                return next((cid for cid in api.chats() if cid > 0), None)
            except Exception:
                return None

        chat_id = self._wait_until(private_chat, self.timeout, step=2)
        if not chat_id:
            raise SetupError(f"aucun message recu par @{username}")
        return chat_id


def configure(setup, env_path, token=None, name="Mes notifications", username_base="notifs"):
    """Full flow. The token is saved as soon as it is known, so a failure later loses nothing."""
    if not token:
        setup.open_botfather()
        token = setup.create_bot(name, username_base)
        update_env_file(env_path, {"TELEGRAM_BOT_TOKEN": token})
        setup.out(f"Token {mask(token)} enregistre dans {env_path}")
    chat_id = setup.start_bot(token)
    update_env_file(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat_id)})
    setup.out(f"Chat id {chat_id} enregistre dans {env_path}")
    setup.api(token, chat_id).send("Configuration terminee : les notifications arriveront ici.")
    return token, chat_id


def default_profile_dir():
    base = os.environ.get("LOCALAPPDATA") or Path.home() / ".cache"
    return Path(base) / "telegram-setup" / "browser"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().with_name(".env")),
                        help="file to write (default: .env next to this script)")
    parser.add_argument("--token", help="reuse this existing bot instead of creating one")
    parser.add_argument("--name", default="Mes notifications", help="display name of the new bot")
    parser.add_argument("--username", default="notifs",
                        help="start of the new bot username (a number and _bot are appended)")
    parser.add_argument("--browser", default="msedge" if sys.platform == "win32" else "chromium",
                        choices=["msedge", "chrome", "chromium"], help="browser to drive")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait at each manual step")
    parser.add_argument("--force", action="store_true", help="create a new bot even if .env is complete")
    args = parser.parse_args(argv)

    existing = {}
    load_env_file(args.env_file, existing)
    if existing.get("TELEGRAM_BOT_TOKEN") and existing.get("TELEGRAM_CHAT_ID") and not args.force:
        print(f"{args.env_file} contient deja TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID (--force pour refaire).")
        return 0
    # A token saved by an interrupted run is reused, so no second bot gets created.
    token = args.token or (None if args.force else existing.get("TELEGRAM_BOT_TOKEN"))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Installe Playwright d'abord : python -m pip install playwright", file=sys.stderr)
        return 2

    with sync_playwright() as playwright:
        options = {"headless": False, "no_viewport": True}
        if args.browser != "chromium":
            options["channel"] = args.browser
        try:
            context = playwright.chromium.launch_persistent_context(str(default_profile_dir()), **options)
        except Exception as err:
            print(f"Impossible d'ouvrir {args.browser} : {str(err).splitlines()[0]}", file=sys.stderr)
            print("Essaie --browser chrome, ou --browser chromium apres 'python -m playwright install chromium'.",
                  file=sys.stderr)
            return 2
        page = context.pages[0] if context.pages else context.new_page()
        try:
            token, chat_id = configure(Setup(TelegramWeb(page), timeout=args.timeout), args.env_file,
                                       token, args.name, args.username)
        except SetupError as err:
            print(f"Echec : {err}", file=sys.stderr)
            return 1
        finally:
            context.close()
    print(f"Termine. Bot {mask(token)}, chat {chat_id}. Un message de test a ete envoye sur Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

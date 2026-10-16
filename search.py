"""Recherches Bing avec des requêtes variées (tendances Google du jour + liste de secours)."""
import logging
import random
import re
import urllib.request
from contextlib import suppress
from html import unescape

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page

log = logging.getLogger("rewards")

BING = "https://www.bing.com/"
TRENDS_RSS = "https://trends.google.com/trending/rss?geo=FR"

FALLBACK_QUERIES = [
    "météo demain", "recette lasagnes maison", "horaires marée", "programme tv ce soir",
    "résultats ligue 1", "comment faire du pain maison", "meilleurs films 2026", "prix du carburant",
    "idée cadeau anniversaire", "horoscope du jour", "traduction anglais français", "calories banane",
    "comment planter des tomates", "exercices abdos débutant", "histoire de la tour eiffel",
    "distance terre lune", "capitale de l'australie", "recette crêpes", "jours fériés 2026",
    "convertisseur euro dollar", "série netflix populaire", "bienfaits du thé vert",
    "comment nettoyer un four", "musées gratuits paris", "plus haute montagne d'europe",
    "recette soupe potiron", "astuces rangement maison", "quand planter les bulbes",
    "championnat du monde de rugby", "que faire ce week-end", "tarif timbre 2026",
    "comment apprendre la guitare", "meilleur smartphone 2026", "recette poulet curry",
    "origine d'halloween", "signes de fatigue chez le chat", "combien d'os dans le corps humain",
    "planètes du système solaire", "recette tarte aux pommes", "horaires bureau de poste",
    "comment économiser de l'énergie", "jeux de société en famille", "randonnée près de chez moi",
    "prix de l'or aujourd'hui", "vocabulaire espagnol de base", "films d'animation à voir",
    "comment faire un CV", "définition intelligence artificielle", "recette cookies moelleux",
    "résultats formule 1", "livres à lire cet automne", "exposition en cours", "cinéma horaires",
]


def trending_queries() -> list[str]:
    try:
        req = urllib.request.Request(TRENDS_RSS, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            xml = r.read().decode("utf-8", "replace")
        titles = re.findall(r"<item>\s*<title>(.*?)</title>", xml, re.S)
        return [unescape(t).strip() for t in titles if t.strip()]
    except Exception as e:
        log.debug("Tendances indisponibles : %s", e)
        return []


def queries(n: int) -> list[str]:
    trends = trending_queries()
    random.shuffle(trends)
    pool = trends + random.sample(FALLBACK_QUERIES, len(FALLBACK_QUERIES))
    seen, out = set(), []
    for q in pool:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:n]


def search_in(page: Page, query: str):
    """Tape une requête dans la barre de recherche Bing, comme un humain."""
    box = page.locator("#sb_form_q")
    if "bing.com" not in page.url or not box.count():  # pas (ou plus) sur une page Bing avec la barre
        page.goto(BING, wait_until="domcontentloaded")
    with suppress(PWError):
        reject = page.locator("#bnp_btn_reject")
        if reject.is_visible(timeout=1000):
            reject.click()
    box.click()
    box.fill("")
    box.press_sequentially(query, delay=random.randint(60, 160))
    page.wait_for_timeout(random.randint(300, 900))
    box.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(random.randint(2000, 4000))
    with suppress(PWError):
        page.mouse.wheel(0, random.randint(200, 1200))


def run(page: Page, count: int, delay: tuple[int, int]):
    qs = queries(count)
    with suppress(PWError):  # en cas d'échec, search_in() retentera d'aller sur Bing
        page.goto(BING, wait_until="domcontentloaded")
    for i, q in enumerate(qs, 1):
        try:
            search_in(page, q)
            log.info("  recherche %d/%d : %s", i, len(qs), q)
        except PWError as e:
            log.warning("  recherche %d/%d échouée : %s", i, len(qs), str(e).splitlines()[0])
            with suppress(PWError):
                page.goto(BING, wait_until="domcontentloaded")
        if i < len(qs):
            page.wait_for_timeout(random.uniform(*delay) * 1000)

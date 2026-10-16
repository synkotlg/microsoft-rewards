"""Quêtes du site Microsoft Rewards (nouvelle interface Next.js, 2026).

Le tableau de bord embarque ses données dans `self.__next_f` (flux RSC de Next.js).
On y lit les offres (offerId, isCompleted, isLocked, points...), puis on clique sur
les vraies cartes de la page : c'est le site lui-même qui déclare l'activité à
Microsoft, exactement comme lors d'un clic manuel. Après chaque série, la page est
relue pour vérifier ce que Microsoft a réellement validé.
"""
import json
import logging
import math
import random
import re
from contextlib import suppress
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PWError

import search
from state import State

log = logging.getLogger("rewards")

REWARDS = "https://rewards.bing.com"
DASHBOARD = REWARDS + "/dashboard"
EARN = REWARDS + "/earn"

# Liens jamais suivis : échange de points, téléchargements, installations, parrainage, boutique,
# liens qui ouvrent une application Windows.
SKIP_LINKS = (
    "/redeem", "apps.microsoft.com", "/apps/wallpaper", "microsoft-edge://", "ms-windows-store:",
    "ms-search:", "referandearn", "/refer", "microsoft.com/fr-fr/store", "xbox.com", "autodownload=",
)

# Offres jamais faites, d'après leur texte : téléchargement, installation, parrainage.
SKIP_TEXT = re.compile(r"t[ée]l[ée]charg|install|download|parrain", re.I)
TEXT_FIELDS = ("title", "description", "linkText", "ariaLabel", "ctaText")

# Signes que Microsoft a signalé ou suspendu le compte : dans ce cas on arrête tout.
# - un avertissement dans les données de la page (nom vu dans l'ancienne API : Fraud_UserWarning_BotScore_UX) ;
# - le texte de suspension AFFICHÉ (il est aussi présent, invisible, dans chaque page comme modèle).
FLAG_NAME = re.compile(r"fraud_?\w*warning|botscore", re.I)
SERIOUS_WARNING = re.compile(r"fraud|bot|suspen|restrict|ban", re.I)
SUSPENDED_TEXT = re.compile(r"Votre\s+compte\s+Microsoft\s+Rewards\s+a\s+été\s+suspendu"
                            r"|Your\s+Microsoft\s+Rewards\s+account\s+has\s+been\s+suspended", re.I)

# Next.js vide `self.__next_f` après l'hydratation : on relit les balises <script> d'origine.
FLIGHT_JS = r"""
() => [...document.scripts]
  .map(s => s.textContent)
  .filter(t => t.startsWith('self.__next_f.push('))
  .map(t => {
    try {
      const a = JSON.parse(t.slice('self.__next_f.push('.length, t.lastIndexOf(')')));
      return a[0] === 1 ? a[1] : '';
    } catch (e) { return ''; }
  })
  .join('')
"""

# Associe chaque élément cliquable à l'offre qu'il représente, via les props React.
TAG_JS = r"""
() => {
  const found = {};
  for (const el of document.querySelectorAll('a, button')) {
    const key = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
    if (!key) continue;
    for (let f = el[key], d = 0; f && d < 25; f = f.return, d++) {
      const p = f.memoizedProps;
      if (!p || typeof p !== 'object') continue;
      const id = p.offerId || (p.instrument && p.instrument.data && p.instrument.data.offerId);
      if (id) {
        if (!(id in found)) {
          el.setAttribute('data-rw-offer', id);
          found[id] = el.innerText.replace(/\s+/g, ' ').trim();
        }
        break;
      }
    }
  }
  return found;
}
"""

# Vrai quand React a « hydraté » la page (sinon les cartes ne sont pas encore cliquables).
HYDRATED_JS = "() => [...document.querySelectorAll('a')].some(a => Object.keys(a).some(k => k.startsWith('__reactFiber$')))"

NEXT_BUTTON = re.compile(r"^(Suivant|Question suivante|Afficher le résultat|Voir le résultat|Next|Show result|See results?)", re.I)

# Recherches thématiques « Explorer sur Bing » : thème -> requêtes possibles.
# L'offerId a la forme ENUS_<thème>_exploreonbing_activation_Evergreen. Microsoft ne valide
# l'offre que si Bing range la requête dans le bon thème : elle doit être concrète. Pour les
# cartes « à compléter » (« les paroles de votre chanson préférée », « un mot que vous ne
# comprenez pas »...), il faut nommer une vraie chanson, un vrai mot, une vraie ville, etc.
# Un thème inconnu est rapproché de la plus longue clé qu'il contient (« personalloan » -> « loan »).
EXPLORE_TOPICS = {
    # Cartes « à compléter »
    "lyric": ["paroles bohemian rhapsody queen", "bohemian rhapsody lyrics", "hotel california lyrics",
              "paroles la bohème charles aznavour", "billie jean lyrics"],
    "translat": ["traduire bonjour en anglais", "traduction merci en espagnol", "comment dit-on chat en allemand"],
    "dictionar": ["définition procrastination", "définition sérendipité", "définition éphémère"],
    "define": ["définition procrastination", "définition sérendipité", "définition éphémère"],
    "timezone": ["heure à new york", "heure à tokyo", "quelle heure est-il à montréal"],
    "stock": ["cours action apple", "cours action lvmh", "cours action airbus"],
    "economy": ["cours du cac 40", "cours action lvmh", "taux d'inflation france"],
    "packagetracking": ["suivi colis la poste", "suivre un colis colissimo", "suivi colis chronopost"],
    "shoppinglist": ["acheter une cafetière", "acheter des écouteurs sans fil", "acheter une lampe de bureau"],
    "currency": ["100 euros en dollars", "50 euros en livres sterling", "taux de change euro yen"],
    "direction": ["itinéraire paris lyon", "trajet bordeaux toulouse", "itinéraire marseille nice"],
    "weather": ["météo paris", "météo lyon demain", "météo marseille ce week-end"],
    # Thèmes
    "bankaccount": ["ouvrir un compte bancaire en ligne", "comparatif banque en ligne", "meilleur compte épargne"],
    "concert": ["billets concert paris", "billetterie concert", "concerts à venir à lyon"],
    "rentalcar": ["location voiture pas cher", "location de voiture aéroport", "comparateur location voiture"],
    "airlineticket": ["billet d'avion pas cher", "vol paris new york", "comparateur de vols"],
    "flight": ["billet d'avion pas cher", "vol paris barcelone", "comparateur de vols"],
    "airportparking": ["parking aéroport roissy", "parking orly pas cher", "réserver un parking aéroport"],
    "flower": ["livraison de fleurs", "envoyer un bouquet de fleurs", "fleuriste livraison à domicile"],
    "streaming": ["comparatif plateformes de streaming", "meilleur service de streaming", "abonnement streaming prix"],
    "cellphone": ["forfait mobile pas cher", "comparatif forfait mobile 5g", "meilleur forfait mobile"],
    "mobileplan": ["forfait mobile pas cher", "comparatif forfait mobile 5g", "meilleur forfait mobile"],
    "phoneplan": ["forfait mobile pas cher", "comparatif forfait mobile 5g", "meilleur forfait mobile"],
    "internet": ["comparatif box internet", "meilleure offre fibre", "fournisseur internet fibre"],
    "cruise": ["croisière méditerranée", "croisière pas chère", "croisière fjords norvège"],
    "jewel": ["bijoux en or femme", "bague de fiançailles", "collier en argent"],
    "realestate": ["maison à vendre", "appartement à vendre", "prix immobilier au m2"],
    "homeimprovement": ["rénovation salle de bain prix", "isolation des combles", "idées peinture salon"],
    "garden": ["quand tailler les rosiers", "entretien du jardin en automne", "plantes d'intérieur faciles"],
    "mattress": ["meilleur matelas", "matelas mémoire de forme", "comparatif matelas"],
    "petsupplies": ["croquettes pour chien", "litière pour chat", "accessoires pour chien"],
    "petfood": ["croquettes pour chien", "pâtée pour chat", "croquettes sans céréales"],
    "insurance": ["comparateur assurance auto", "assurance habitation pas chère", "comparatif mutuelle santé"],
    "healthinsurance": ["comparatif mutuelle santé", "mutuelle santé pas chère", "meilleure mutuelle famille"],
    "autoinsurance": ["comparateur assurance auto", "assurance auto pas chère", "devis assurance voiture"],
    "carinsurance": ["comparateur assurance auto", "assurance auto pas chère", "devis assurance voiture"],
    "homeinsurance": ["assurance habitation pas chère", "comparatif assurance habitation", "devis assurance maison"],
    "lifeinsurance": ["comparatif assurance vie", "meilleure assurance vie", "rendement assurance vie"],
    "petinsurance": ["assurance chien et chat", "comparatif mutuelle animaux", "assurance santé animaux"],
    "travelinsurance": ["assurance voyage", "assurance annulation voyage", "comparatif assurance voyage"],
    "creditreport": ["vérifier son score de crédit", "améliorer son score de crédit", "consulter son fichier banque de france"],
    "creditscore": ["vérifier son score de crédit", "améliorer son score de crédit", "consulter son fichier banque de france"],
    "creditcard": ["meilleure carte bancaire", "carte bancaire gratuite", "comparatif cartes bancaires"],
    "loan": ["simulation prêt personnel", "taux prêt immobilier", "simulation crédit auto"],
    "mortgage": ["taux prêt immobilier", "simulation prêt immobilier", "courtier prêt immobilier"],
    "jobs": ["offres d'emploi", "offre d'emploi développeur", "recrutement cdi"],
    "recipe": ["recette poulet rôti", "recette gâteau au chocolat", "recette lasagnes"],
    "fooddelivery": ["livraison repas à domicile", "commander une pizza en ligne", "livraison sushi"],
    "restaurant": ["restaurant italien paris", "meilleur restaurant à lyon", "réserver un restaurant"],
    "deal": ["bons plans high-tech", "promo smartphone", "soldes ordinateur portable"],
    "coupon": ["code promo", "bons de réduction", "code promo livraison gratuite"],
    "shopping": ["bons plans high-tech", "promo baskets", "soldes ordinateur portable"],
    "hotel": ["hôtel pas cher paris", "hôtel bord de mer", "réserver un hôtel à lyon"],
    "travel": ["idées de voyage en europe", "vacances pas chères", "week-end à rome"],
    "packinglist": ["liste valise vacances", "quoi emporter en voyage", "liste bagage cabine"],
    "safari": ["safari au kenya", "safari en tanzanie prix", "meilleure période safari"],
    "usedcar": ["voiture d'occasion", "voiture d'occasion pas chère", "acheter une voiture d'occasion"],
    "cars": ["prix voiture électrique", "comparatif suv", "voiture neuve"],
    "movie": ["films à l'affiche", "sorties cinéma de la semaine", "horaires cinéma"],
    "book": ["meilleurs livres 2026", "prix goncourt 2026", "romans à lire"],
    "videogame": ["sorties jeux vidéo du mois", "meilleurs jeux ps5", "jeux vidéo populaires"],
    "craft": ["activité manuelle enfant", "idées bricolage diy", "loisirs créatifs"],
    "haircare": ["soin cheveux secs", "shampoing sans sulfate", "masque cheveux maison"],
    "symptom": ["symptômes grippe", "symptômes rhume", "symptômes angine"],
    "fitness": ["exercices sport à la maison", "programme musculation débutant", "exercices abdos"],
    "sport": ["résultats ligue 1", "classement top 14", "score psg"],
    "news": ["actualités du jour", "dernières nouvelles france", "actualité économique"],
    "election": ["résultats élections", "prochaines élections en france", "sondage présidentielle"],
}

# Clés courtes acceptées en fin de thème (« personalloan », « cheaphotels »...). Les autres clés
# courtes ne valent qu'en début de thème, sinon « passport » donnerait « sport », « aircraft » « craft ».
SUFFIX_OK = {"loan", "cars", "hotel"}

# Faute de thème reconnu dans l'offerId, mot du texte de la carte (en français) -> thème.
# Cherché en début de mot ; du plus spécifique au plus général : le premier trouvé l'emporte.
EXPLORE_KEYWORDS = [
    ("paroles", "lyric"), ("chanson", "lyric"), ("tradu", "translat"), ("définition", "dictionar"),
    ("fuseau horaire", "timezone"), ("décalage horaire", "timezone"), ("cours de bourse", "stock"),
    ("cours de l'action", "stock"), ("colis", "packagetracking"), ("liste de courses", "shoppinglist"),
    ("taux de change", "currency"), ("devise", "currency"), ("itinéraire", "direction"), ("météo", "weather"),
    ("assurance", "insurance"), ("score de crédit", "creditscore"), ("rapport de crédit", "creditreport"),
    ("carte de crédit", "creditcard"), ("cartes de crédit", "creditcard"), ("parking", "airportparking"),
    ("fleur", "flower"), ("streaming", "streaming"), ("forfaits mobiles", "mobileplan"),
    ("forfait mobile", "mobileplan"), ("internet", "internet"), ("fibre", "internet"), ("forfait", "mobileplan"),
    ("croisière", "cruise"), ("bijou", "jewel"), ("concert", "concert"), ("spectacle", "concert"),
    ("comptes chèques", "bankaccount"), ("compte bancaire", "bankaccount"), ("épargne", "bankaccount"),
    ("banque", "bankaccount"), ("prêt immobilier", "mortgage"), ("crédit immobilier", "mortgage"),
    ("location de voiture", "rentalcar"), ("voiture de location", "rentalcar"),
    ("location de vacances", "travel"), ("billet d'avion", "flight"), ("vols", "flight"), ("avion", "flight"),
    ("immobilier", "realestate"), ("rénovation", "homeimprovement"), ("jardin", "garden"),
    ("matelas", "mattress"), ("animaux", "petsupplies"), ("animal", "petsupplies"), ("prêt", "loan"),
    ("crédit", "loan"), ("emploi", "jobs"), ("postes", "jobs"), ("recette", "recipe"),
    ("cuisiner", "fooddelivery"), ("livraison de repas", "fooddelivery"), ("restaurant", "restaurant"),
    ("hôtel", "hotel"), ("valise", "packinglist"), ("voyage", "travel"), ("occasion", "usedcar"),
    ("voiture", "cars"), ("film", "movie"), ("livre", "book"), ("jeux vidéo", "videogame"),
    ("cheveux", "haircare"), ("symptôme", "symptom"), ("sport", "sport"), ("élection", "election"),
    ("actualité", "news"), ("bons plans", "deal"), ("promo", "deal"),
]


def pause(page: Page, a: float = 1.0, b: float = 3.0):
    page.wait_for_timeout(random.uniform(a, b) * 1000)


def _walk(o, out: list):
    if isinstance(o, dict):
        if "offerId" in o or "pointsCounters" in o or ("balance" in o and "level" in o):
            out.append(o)
        for v in o.values():
            _walk(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out)


ROW = re.compile(rb"([0-9a-f]+):")


def flight_rows(flight: str) -> list:
    """Lignes JSON du flux RSC (« <id>:<json> », une par ligne).

    Les lignes de texte « <id>:T<longueur hexa>,<texte> » ne finissent PAS par un saut de ligne :
    la ligne suivante est collée derrière. On saute donc exactement <longueur> octets, sinon
    cette ligne suivante (parfois une offre) serait perdue.
    """
    data = flight.encode("utf-8")
    rows, i, n = [], 0, len(data)
    while i < n:
        m = ROW.match(data, i)
        if m and data[m.end():m.end() + 1] == b"T":
            comma = data.find(b",", m.end())
            length = data[m.end() + 1:comma] if comma > 0 else b""
            if length and all(c in b"0123456789abcdef" for c in length):
                i = comma + 1 + int(length, 16)
                continue
        end = data.find(b"\n", i)
        end = n if end < 0 else end
        if m and data[m.end():m.end() + 1] in (b"[", b"{"):
            with suppress(ValueError):
                rows.append(json.loads(data[m.end():end]))
        i = end + 1
    return rows


def parse_flight(flight: str) -> list[dict]:
    """Objets utiles contenus dans le flux RSC d'une page."""
    objs: list[dict] = []
    for row in flight_rows(flight):
        _walk(row, objs)
    return objs


def account_warning(o) -> str | None:
    """Nom d'un avertissement grave (fraude, bot, suspension...) présent dans les données, sinon None."""
    if isinstance(o, dict):
        warnings = o.get("userWarnings")
        if isinstance(warnings, list):
            for w in warnings:
                name = (w.get("name") or w.get("type") or "") if isinstance(w, dict) else str(w)
                if SERIOUS_WARNING.search(name) or FLAG_NAME.search(name):
                    return name
                log.info("Avertissement Rewards (non bloquant) : %s", name or w)
        name = o.get("name")
        if isinstance(name, str) and FLAG_NAME.search(name):
            return name
        items = o.values()
    elif isinstance(o, list):
        items = o
    else:
        return None
    for v in items:
        found = account_warning(v)
        if found:
            return found
    return None


def page_data(page: Page) -> list[dict]:
    """Objets utiles contenus dans le flux RSC de la page (chargée par un vrai goto)."""
    return parse_flight(page.evaluate(FLIGHT_JS))


def offers(objs: list[dict]) -> dict[str, dict]:
    """Fusionne les différentes représentations d'une même offre."""
    merged: dict[str, dict] = {}
    for o in objs:
        if "offerId" not in o:
            continue
        cur = merged.setdefault(o["offerId"], {})
        for k, v in o.items():
            if v != "$undefined" and not isinstance(v, (dict, list)):
                cur.setdefault(k, v)
    return merged


def is_true(v) -> bool:
    return v is True or v == "true"


def skipped_link(o: dict) -> bool:
    """Vrai si un lien de l'offre (href, destination, ctaUrl...) mène à quelque chose qu'on ne fait jamais."""
    return any(s in v.lower() for v in o.values() if isinstance(v, str) for s in SKIP_LINKS)


def excluded(o: dict) -> str | None:
    """Raison pour laquelle l'offre n'est jamais faite, sinon None."""
    if skipped_link(o):
        return "lien exclu"
    if any(SKIP_TEXT.search(str(o.get(k) or "")) for k in TEXT_FIELDS):
        return "téléchargement, installation ou parrainage"
    return None


def same_page(url: str, target: str) -> bool:
    a, b = urlsplit(url), urlsplit(target)
    return (a.netloc, a.path.rstrip("/")) == (b.netloc, b.path.rstrip("/"))


class AccountFlagged(Exception):
    """Microsoft a signalé (avertissement) ou suspendu le compte Rewards."""


class Rewards:
    def __init__(self, ctx: BrowserContext, state: State | None = None):
        self.ctx = ctx
        self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self.state = state or State()
        self.done: list[str] = []

    # ------------------------------------------------------------------ outils

    def goto(self, url: str, check: bool = True) -> list[dict]:
        """Charge une page Rewards et renvoie ses données. Lève AccountFlagged si le compte est signalé
        (sauf check=False, pour le mode --dump qui doit pouvoir enregistrer la page malgré tout)."""
        if self.page.is_closed():  # onglet principal fermé par un site : on en rouvre un
            self.page = self.ctx.new_page()
        self.page.goto(url, wait_until="domcontentloaded")
        with suppress(PWError):
            self.page.wait_for_load_state("networkidle", timeout=8000)
        with suppress(PWError):
            self.page.wait_for_function(HYDRATED_JS, timeout=15000)
        pause(self.page, 1.5, 3)
        rows = flight_rows(self.page.evaluate(FLIGHT_JS))
        if check:
            self.check_flag(rows)
        self.dismiss_banners(self.page)
        objs: list[dict] = []
        for row in rows:
            _walk(row, objs)
        return objs

    def check_flag(self, rows: list):
        """Lève AccountFlagged si la page montre un avertissement grave ou une suspension du compte."""
        name = account_warning(rows)
        if name:
            raise AccountFlagged(f"avertissement « {name} » dans les données de la page")
        with suppress(PWError):
            if self.page.get_by_text(SUSPENDED_TEXT).locator("visible=true").count():
                raise AccountFlagged("la page indique que le compte Rewards est suspendu")

    def ensure(self, url: str):
        """Va sur `url` seulement si on n'y est pas déjà (évite de recharger la page à chaque carte)."""
        if self.page.is_closed() or not same_page(self.page.url, url):
            self.goto(url)

    def dismiss_banners(self, page: Page):
        """Refuse les cookies facultatifs et ferme les bulles d'aide qui bloquent les clics."""
        for sel in ("#bnp_btn_reject", "#wcpConsentBannerCtrl button:has-text('Refuser')"):
            with suppress(PWError):
                btn = page.locator(sel).first
                if btn.is_visible(timeout=500):
                    btn.click()
                    pause(page, 0.5, 1)
        with suppress(PWError):
            btn = page.get_by_role("button", name=re.compile(r"^(Refuser|Reject)$")).first
            if btn.is_visible(timeout=500):
                btn.click()
        with suppress(PWError):
            for btn in page.locator("[role=dialog] button:has-text('Fermer')").all():
                if btn.is_visible():
                    btn.click()
                    pause(page, 0.3, 0.8)

    def logged_in(self) -> bool:
        objs = self.goto(DASHBOARD)
        url = self.page.url
        if "login." in url or "/welcome" in url or "/signin" in url:
            return False
        return self.balance(objs) is not None

    @staticmethod
    def balance(objs: list[dict]) -> int | None:
        for o in objs:
            if "balance" in o and "level" in o and isinstance(o["balance"], int):
                return o["balance"]
        return None

    def counters(self) -> dict:
        """Compteurs de recherche du jour : {"pc": {"progress", "max"}, "mobile": ...}."""
        for o in self.goto(EARN):
            if "pointsCounters" in o:
                return o["pointsCounters"]
        return {}

    def verify(self, url: str, ids: list[str]) -> dict[str, bool]:
        """Recharge `url` et dit, pour chaque offre de `ids`, si Microsoft l'a validée.

        Une offre absente de la page rechargée (page arrivée incomplète) n'est pas dans le résultat :
        on ne sait pas, donc on ne la compte ni comme réussie ni comme échouée.
        """
        data: dict[str, dict] = {}
        for _ in range(2):  # la page arrive parfois incomplète : on recharge une fois
            try:
                data = offers(self.goto(url))
            except PWError as e:
                log.debug("Rechargement de %s impossible : %s", url, e)
                data = {}
            if all(i in data for i in ids):
                break
        return {i: is_true(data[i].get("isCompleted")) for i in ids if i in data}

    def tag(self, page: Page) -> dict[str, str]:
        return page.evaluate(TAG_JS)

    def close_dialogs(self, page: Page):
        """Ferme une fenêtre laissée ouverte par la carte précédente (elle bloquerait les clics)."""
        with suppress(PWError):
            if page.locator("[role=dialog]").locator("visible=true").count():
                page.keyboard.press("Escape")
                pause(page, 0.4, 0.8)

    def click_offer(self, offer_id: str, label: str, then=None) -> bool:
        """Clique la carte d'une offre ; `then(page)` est appelé sur la page ouverte par la carte."""
        page = self.page
        try:
            texts: dict[str, str] = {}
            for _ in range(3):
                texts = self.tag(page)
                if offer_id in texts:
                    break
                page.wait_for_timeout(2500)
            loc = page.locator(f'[data-rw-offer="{offer_id}"]')
            if offer_id not in texts or not loc.count():
                log.warning("  carte introuvable : %s", label)
                return False
            loc = loc.first
            href = (loc.get_attribute("href", timeout=5000) or "").lower()
            if any(s in href for s in SKIP_LINKS):
                log.info("  ignorée (lien %s) : %s", href[:40], label)
                return False
            blank = loc.get_attribute("target", timeout=5000) == "_blank"
            self.close_dialogs(page)
        except PWError as e:
            log.warning("  échec : %s (%s)", label, str(e).splitlines()[0])
            return False

        reported = []

        def on_response(r):
            if r.request.method == "POST" and "next-action" in r.request.headers and 200 <= r.status < 300:
                reported.append(r.status)

        page.on("response", on_response)
        start = page.url
        try:
            loc.scroll_into_view_if_needed(timeout=10000)
            pause(page, 0.8, 2)
            # La carte ouvre normalement un nouvel onglet ; certaines naviguent dans l'onglet courant.
            clicked, tab = False, None
            try:
                with self.ctx.expect_page(timeout=20000 if blank else 5000) as info:
                    loc.click(timeout=15000)
                    clicked = True
                tab = info.value
            except PWError:
                if not clicked:
                    raise
            if tab is not None:
                try:
                    self.visit(tab, then)
                finally:
                    with suppress(PWError):
                        tab.close()
            else:
                pause(page, 2, 4)
                if not page.is_closed() and not same_page(page.url, start):
                    self.visit(page, then)  # la page de la carte a remplacé la liste (rechargée ensuite)
                elif then:
                    log.info("  la carte n'a ouvert aucune page : %s", label)
            for _ in range(20):
                if reported or page.is_closed():
                    break
                page.wait_for_timeout(500)
        except PWError as e:
            log.warning("  échec : %s (%s)", label, str(e).splitlines()[0])
            return False
        finally:
            with suppress(PWError):
                page.remove_listener("response", on_response)
        log.info("  → %s%s", label, "" if reported else " (pas de confirmation du serveur)")
        return True

    def visit(self, tab: Page, then=None):
        with suppress(PWError):
            tab.wait_for_load_state("domcontentloaded", timeout=30000)
        if any(s in tab.url.lower() for s in SKIP_LINKS):  # ex. redirection vers le Store
            log.info("  page ignorée : %s", tab.url[:60])
            return
        pause(tab, 3, 5)
        self.dismiss_banners(tab)
        if "bing.com" in tab.url:
            solve_quiz(tab)
        if then:
            then(tab)
        with suppress(PWError):
            tab.mouse.wheel(0, random.randint(300, 900))
        pause(tab, 2, 4)

    def _attempt(self, url: str, o: dict, label, then) -> bool:
        """Un essai sur une offre ; les erreurs Playwright n'interrompent pas les offres suivantes."""
        try:
            self.ensure(url)
            ok = self.click_offer(o["offerId"], label(o), then(o) if then else None)
        except PWError as e:
            log.warning("  échec : %s (%s)", label(o), str(e).splitlines()[0])
            ok = False
        with suppress(PWError):
            pause(self.page, 2, 5)
        return ok

    def do_offers(self, url: str, todo: list[dict], label, then=None):
        """Clique chaque offre de `todo` (cartes de la page `url`), vérifie, réessaie une fois, mémorise.

        `label(o)` donne le libellé du journal ; `then(o)` renvoie l'éventuelle action à faire
        dans la page ouverte par la carte (appelé à chaque essai).
        Une offre restée non validée deux jours est mise de côté quelques jours (state.py) :
        inutile de recliquer chaque jour une tâche que Microsoft ne valide jamais.
        """
        clicked = []
        for o in todo:
            if self.state.given_up(o["offerId"]):
                log.info("  mise de côté (non validée lors des derniers jours) : %s", label(o))
                continue
            if self._attempt(url, o, label, then):
                clicked.append(o)
        if not clicked:
            return
        status = self.verify(url, [o["offerId"] for o in clicked])
        retry = [o for o in clicked if status.get(o["offerId"]) is False]
        for o in retry:
            log.info("  pas encore validée, nouvel essai : %s", label(o))
            self._attempt(url, o, label, then)
        if retry:
            status.update(self.verify(url, [o["offerId"] for o in retry]))
        for o in clicked:
            ok = status.get(o["offerId"])
            if ok:
                self.state.succeeded(o["offerId"])
                self.done.append(label(o))
                log.info("  ✓ %s", label(o))
            elif ok is False:
                self.state.failed(o["offerId"])
                log.warning("  ✗ non validée par Microsoft : %s", label(o))
            else:
                log.info("  ? état inconnu (page incomplète) : %s", label(o))

    @staticmethod
    def doable(items, label) -> list[dict]:
        """Offres ni terminées, ni verrouillées, ni exclues (liens/textes interdits : on dit pourquoi)."""
        todo = []
        for o in items:
            if is_true(o.get("isCompleted")) or is_true(o.get("isLocked")):
                continue
            why = excluded(o)
            if why:
                log.info("  ignorée (%s) : %s", why, label(o))
                continue
            todo.append(o)
        return todo

    # ------------------------------------------------------------------ quêtes

    def discover_sections(self):
        """Déplie les sections du tableau de bord (+5 points la première fois)."""
        for url in (DASHBOARD, EARN):
            self.goto(url)
            triggers = self.page.locator('button[slot="trigger"][aria-expanded="false"]')
            for _ in range(triggers.count()):
                btn = triggers.nth(0)  # la liste se réduit à chaque section dépliée
                with suppress(PWError):
                    name = btn.get_attribute("aria-label") or "section"
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    pause(self.page, 1.5, 3)
                    self.dismiss_banners(self.page)
                    log.info("  ✓ section dépliée : %s", name)

    def daily_set(self):
        objs = self.goto(DASHBOARD)
        items = [o for o in offers(objs).values() if "date" in o and "DailySet" in o["offerId"]]

        def label(o):
            return f"{o.get('title', o['offerId'])} (+{o.get('points', '?')})"

        todo = self.doable(items, label)
        log.info("Ensemble du jour : %d/%d à faire", len(todo), len(items))
        self.do_offers(DASHBOARD, todo, label)

    def more_activities(self):
        """« Continuer à gagner » : quiz, liens Bing, et offres promotionnelles qui rapportent des points."""
        objs = self.goto(EARN)
        items = [
            o for o in offers(objs).values()
            if isinstance(o.get("points"), int) and o["points"] > 0
            and "date" not in o and "exploreonbing" not in o["offerId"].lower()
            # Les promos avec mentions légales s'ouvrent dans une fenêtre de partenaire/installation.
            and not o.get("legalText") and not o.get("legalCtaText")
        ]

        def label(o):
            return f"{o.get('title', o['offerId'])} (+{o['points']})"

        todo = self.doable(items, label)
        log.info("Autres activités : %d à faire", len(todo))
        self.do_offers(EARN, todo, label)

    def explore_on_bing(self):
        objs = self.goto(EARN)
        items = [o for o in offers(objs).values() if "exploreonbing" in o["offerId"].lower()]
        texts = self.tag(self.page)

        def label(o):
            return (texts.get(o["offerId"]) or o["offerId"]).replace("​", "")[:40]

        todo = self.doable(items, label)
        log.info("Recherches thématiques : %d à faire", len(todo))
        used: dict[str, list[str]] = {}

        def then(o):
            # Au réessai, une autre requête que la première.
            options = explore_queries(o["offerId"], texts.get(o["offerId"], ""))
            tried = used.setdefault(o["offerId"], [])
            query = random.choice([q for q in options if q not in tried] or options)
            tried.append(query)

            def act(tab):
                log.info("    recherche : « %s »", query)
                search.search_in(tab, query)
            return act

        self.do_offers(EARN, todo, label, then)

    def quests(self):
        self.goto(EARN)
        urls = sorted({
            h for h in (a.get_attribute("href") for a in self.page.locator('a[href^="/earn/quest/"]').all()) if h
        })
        log.info("Quêtes : %d trouvées", len(urls))

        def label(o):
            return o.get("ariaLabel") or o.get("linkText") or o["offerId"]

        for url in urls:
            tasks = []
            for _ in range(2):  # la page arrive parfois incomplète : on réessaie une fois
                tasks = [o for o in offers(self.goto(REWARDS + url)).values() if "linkText" in o]
                if tasks:
                    break
            todo = self.doable(tasks, label)
            log.info("  %s : %d tâche(s) faisable(s) sur %d", url.rsplit("/", 1)[-1], len(todo), len(tasks))
            self.do_offers(REWARDS + url, todo, label)

    def claim(self):
        """Réclame les points en attente (bouton « Prêt à réclamer »), sinon ils expirent."""
        self.goto(DASHBOARD)
        btn = self.page.get_by_role("button", name=re.compile(r"(Prêt à réclamer|Ready to claim)", re.I)).first
        try:
            if not btn.is_visible(timeout=3000):
                return
            m = re.search(r"\d[\d\s  .,]*", btn.inner_text())
            amount = int(re.sub(r"\D", "", m.group(0))) if m else 0
        except PWError:
            return
        if amount <= 0:
            return
        log.info("Points à réclamer : %d", amount)
        try:
            btn.click()
            pause(self.page, 2, 3)
            # Le bouton de la fenêtre contient aussi le montant : « 3 En attente Réclamer des points ».
            cta = self.page.locator("[role=dialog] button").filter(
                has_text=re.compile(r"Réclamer des points|Claim points", re.I)).first
            cta.click(timeout=10000)
            self.page.get_by_text(re.compile(r"Revendication réussie|Claim successful|réussi", re.I)).first.wait_for(timeout=20000)
            log.info("  ✓ %d points réclamés", amount)
            self.done.append(f"{amount} points réclamés")
        except PWError as e:
            log.warning("  réclamation non confirmée : %s", str(e).splitlines()[0])

    def searches(self, delay: tuple[int, int]) -> dict:
        """Recherches Bing PC jusqu'au plafond du jour. Renvoie les derniers compteurs."""
        counters = {}
        for attempt in range(4):
            counters = self.counters()
            pc = counters.get("pc") or {}
            progress, maximum = pc.get("progress", 0), pc.get("max", 0)
            log.info("Recherches PC : %s/%s points", progress, maximum)
            if not maximum or progress >= maximum:
                break
            needed = math.ceil((maximum - progress) / 3) + (1 if attempt else 0)
            search.run(self.page, needed, delay)
        return counters


def explore_topic(offer_id: str, card_text: str) -> str | None:
    """Thème d'une recherche thématique : d'après l'offerId, sinon d'après le texte de la carte."""
    m = re.match(r"^[A-Za-z]+_(.+?)_exploreonbing", offer_id, re.I)
    topic = m.group(1).lower() if m else ""
    for t in (topic, topic[:-1] if topic.endswith("s") else None):  # « flights » -> « flight »
        if t in EXPLORE_TOPICS:
            return t
    keys = [
        k for k in EXPLORE_TOPICS
        if (topic.startswith(k) and len(k) >= 5)
        or ((topic.endswith(k) or topic.endswith(k + "s")) and (len(k) >= 6 or k in SUFFIX_OK))
    ]
    if keys:
        return max(keys, key=len)
    text = card_text.lower()
    for word, key in EXPLORE_KEYWORDS:
        if re.search(r"(?<!\w)" + re.escape(word), text):
            return key
    return None


def explore_queries(offer_id: str, card_text: str) -> list[str]:
    """Requêtes possibles pour une recherche thématique (une au 1er essai, une autre au réessai)."""
    topic = explore_topic(offer_id, card_text)
    if topic:
        return EXPLORE_TOPICS[topic]
    # Thème inconnu : description de la carte (« Recherchez sur Bing les ... »), puis son titre.
    log.info("    thème inconnu pour %s : requêtes tirées du texte de la carte", offer_id)
    title, _, desc = card_text.replace("​", "").partition("Recherchez sur Bing")
    m = re.match(r"\s*(?:pour (?:trouver |comparer )?|les |des |l'|la |le )?(.+?)\s*(?:\.|\+|$)", desc)
    candidates = [c for c in ((m.group(1)[:60] if m else ""), title.strip()[:60]) if c]
    return candidates or ["actualités du jour"]


def solve_quiz(tab: Page):
    """Répond aux quiz Bing (bonne réponse lue dans la page) ; ne fait rien ailleurs."""
    legacy_clicks: dict[str, int] = {}
    for _ in range(30):
        try:
            html = tab.content()
            blocks = re.findall(r"var choices = (\[.*?\]);\s*QuizQuestionPane\.init", html, re.S)
            if blocks:
                # Quiz Bing « BingQA » : options .btq_opt, puis « Suivant » / « Afficher le résultat ».
                options = tab.locator(".btq_opt").locator("visible=true")
                nxt = tab.locator(".btq_foot").get_by_role("button", name=NEXT_BUTTON).locator("visible=true")
                if options.count():
                    flags = re.findall(r'isCorrect:\s*"(true|false)"', blocks[-1])
                    idx = flags.index("true") if "true" in flags else 0
                    options.nth(min(idx, options.count() - 1)).click()
                elif nxt.count():
                    nxt.first.click()
                else:
                    return
            elif not _legacy_quiz_step(tab, legacy_clicks):
                return
            pause(tab, 2.5, 4.5)
            tab.wait_for_load_state("domcontentloaded")
        except PWError as e:
            log.debug("Quiz : %s", e)
            return


def _legacy_quiz_step(tab: Page, clicks: dict[str, int]) -> bool:
    """Anciens formats (quiz « rq », sondages) encore servis certains jours."""
    for sel, limit in (("#rqStartQuiz", 1), ('[iscorrectoption="True"]', 10), ("#btoption0", 1)):
        el = tab.locator(sel).locator("visible=true")
        if el.count() and clicks.get(sel, 0) < limit:
            clicks[sel] = clicks.get(sel, 0) + 1
            el.first.click()
            return True
    return False

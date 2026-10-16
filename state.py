"""Mémoire entre deux exécutions (fichier state.json).

Offres cliquées mais jamais validées par Microsoft : après deux jours sans succès, on arrête
de les recliquer pendant une semaine.
"""
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("rewards")

PATH = Path(__file__).parent / "state.json"

# Une offre restée non validée MAX_FAILS jours différents est mise de côté SKIP_DAYS jours.
MAX_FAILS = 2
SKIP_DAYS = 7


class State:
    def __init__(self, path: Path = PATH):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as e:
            log.warning("%s illisible, remis à zéro : %s", path.name, e)
            self.data = {}
        if not isinstance(self.data, dict) or not isinstance(self.data.get("fails"), dict):
            self.data = {"fails": {}}
        # Les offres mises de côté depuis plus de SKIP_DAYS jours sont retentées.
        limit = (date.today() - timedelta(days=SKIP_DAYS)).isoformat()
        self.data["fails"] = {
            k: v for k, v in self.data["fails"].items()
            if isinstance(v, dict) and isinstance(v.get("count"), int)
            and isinstance(v.get("last"), str) and v["last"] >= limit
        }

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Impossible d'enregistrer %s : %s", self.path.name, e)

    def given_up(self, offer_id: str) -> bool:
        """Vrai si l'offre est restée non validée lors des dernières exécutions : inutile de recliquer."""
        return self.data["fails"].get(offer_id, {}).get("count", 0) >= MAX_FAILS

    def failed(self, offer_id: str):
        """Offre restée non validée (après réessai). Compté une seule fois par jour : la tâche
        planifiée peut tourner plusieurs fois le même jour, une mauvaise journée ne suffit pas."""
        today = date.today().isoformat()
        entry = self.data["fails"].setdefault(offer_id, {"count": 0})
        if entry.get("last") != today:
            entry["count"] = entry.get("count", 0) + 1
            entry["last"] = today
            self.save()

    def succeeded(self, offer_id: str):
        if self.data["fails"].pop(offer_id, None) is not None:
            self.save()

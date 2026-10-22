import os
import re

import psycopg
import pytest

from assistant.claude import Usage

URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@/pacewell_test?host=/tmp&port=5433")


def _base_disponible() -> bool:
    try:
        admin = re.sub(r"/pacewell_test", "/postgres", URL)
        with psycopg.connect(admin, autocommit=True) as cx:
            if not cx.execute("SELECT 1 FROM pg_database WHERE datname = 'pacewell_test'").fetchone():
                cx.execute("CREATE DATABASE pacewell_test")
        with psycopg.connect(URL, autocommit=True) as cx:
            cx.execute("DROP TABLE IF EXISTS passages, documents, echanges CASCADE")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def url_base():
    if not _base_disponible():
        pytest.skip("Postgres avec pgvector indisponible (définir TEST_DATABASE_URL)")
    return URL


class FauxClaude:
    """Remplace l'API : réponses prévisibles, et trace des appels pour les vérifier."""

    def __init__(self, reponse="Les retours sont gratuits sous 30 jours [1].", note_par_mot=None, echec=False, tours=None,
                 document=None):
        self.reponse, self.note_par_mot, self.echec = reponse, note_par_mot or {}, echec
        self.tours = list(tours) if tours else None   # tours scriptés de l'agent : listes de blocs
        self.document = document
        self.appels = []

    def texte(self, modele, systeme, message, max_tokens=300):
        self.appels.append(("texte", modele, message))
        derniere = message.rsplit("Dernière question :", 1)[-1].strip()
        return f"{derniere} (reformulée)", Usage(modele, 100, 20)

    def outil(self, modele, systeme, message, outil, max_tokens=600):
        self.appels.append(("outil", modele, message))
        if outil["name"] == "enregistrer_questions":
            section = message.split("Section : ", 1)[1].split("\n", 1)[0]
            extra = ["Je peux renvoyer des baskets portées ?"] if section.startswith("Conditions d'un retour") else []
            return {"questions": [f"Question sur {section} ?"] + extra}, Usage(modele, 300, 40)
        notes = []
        for pid, corps in re.findall(r'<passage id="(\d+)">\n(.*?)</passage>', message, re.S):
            note = max([n for mot, n in self.note_par_mot.items() if mot.lower() in corps.lower()] or [1])
            notes.append({"id": int(pid), "note": note})
        return {"notes": notes}, Usage(modele, 1000, 80)

    def image_outil(self, modele, systeme, image_b64, media, consigne, outil, max_tokens=800):
        self.appels.append(("image", modele, media))
        return self.document or {"type_document": "bon_de_livraison", "numero_commande": "PW104233", "articles": [], "lisible": True}, \
            Usage(modele, 1200, 90)

    def flux_outils(self, modele, systeme, messages, outils, max_tokens=900):
        """Agent simulé. Par défaut : 1er tour = recherche dans la documentation, 2e tour = réponse."""
        self.appels.append(("agent", modele, messages))
        if self.echec:
            raise RuntimeError("API indisponible")
        if self.tours is not None:
            blocs = self.tours.pop(0)
        elif not any(isinstance(m["content"], list) for m in messages):
            question = messages[-1]["content"].split("\n")[-1]
            blocs = [{"type": "tool_use", "id": f"t{len(self.appels)}", "name": "chercher_documentation", "input": {"requete": question}}]
        else:
            blocs = [{"type": "text", "text": self.reponse}]
        for b in blocs:
            if b["type"] == "text":
                for mot in b["text"].split(" "):
                    yield ("texte", mot + " ")
        arret = "tool_use" if any(b["type"] == "tool_use" for b in blocs) else "end_turn"
        yield ("fin", blocs, arret, Usage(modele, 800, 60, cache_ecriture=0, cache_lecture=1800))

    def flux(self, modele, systeme, messages, max_tokens=700):
        self.appels.append(("flux", modele, messages))
        if self.echec:
            raise RuntimeError("API indisponible")
        for mot in self.reponse.split(" "):
            yield mot + " "
        yield Usage(modele, 1500, 60)

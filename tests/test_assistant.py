import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assistant.base import Base, Trouve
from assistant.config import Config
from assistant.decoupage import MOTS_MAX, decouper, lire_corpus
from assistant.embeddings import Hachage
from assistant.ingerer import ingerer
from assistant.recherche import Chercheur, fusionner
from assistant.reponse import Assistant, citations, messages_pour
from tests.conftest import FauxClaude

CORPUS = Path(__file__).resolve().parent.parent / "corpus"


# --- Découpage --------------------------------------------------------------------------------
def test_decoupage_garde_les_sections_et_la_taille():
    docs = lire_corpus(CORPUS)
    assert len(docs) >= 10
    for doc in docs:
        for p in decouper(doc):
            assert p.texte and len(p.texte.split()) <= MOTS_MAX + 60
            assert not p.texte.lstrip().startswith("#"), "un titre ne doit pas rester dans le texte"


def test_un_tableau_n_est_pas_coupe():
    doc = next(d for d in lire_corpus(CORPUS) if d.source == "guide-des-tailles.md")
    tableau = [p for p in decouper(doc) if "| 23,5 cm" in p.texte]
    assert len(tableau) == 1 and "| 29,5 cm" in tableau[0].texte


def test_le_chemin_des_titres_est_ajoute_au_texte_vectorise():
    doc = next(d for d in lire_corpus(CORPUS) if d.source == "retours-echanges.md")
    essai = next(p for p in decouper(doc) if "30 kilomètres" in p.section)
    assert essai.section == "Conditions d'un retour > Essai sur le terrain : l'offre « 30 kilomètres »"
    assert essai.texte_a_vectoriser.startswith("Retours et échanges > Conditions d'un retour")


# --- Fusion et citations ----------------------------------------------------------------------
def _t(i, rang):
    return Trouve(i, "s", "T", "/", "S", "", f"texte {i}", rang, 0.5)


def test_rrf_favorise_un_passage_present_dans_les_deux_listes():
    fus = fusionner([_t(1, 1), _t(2, 2), _t(3, 3)], [_t(3, 1), _t(4, 2)])
    assert fus[0].id == 3  # 1/63 + 1/61 > 1/61
    assert fus[0].rang_vecteur == 3 and fus[0].rang_texte == 1
    assert round(fus[0].score_rrf, 5) == round(1 / 63 + 1 / 61, 5)


def test_citations_ignore_les_numeros_inexistants():
    assert citations("Oui [2]. Et aussi [1, 3]. Faux [9].", 3) == [2, 1, 3]


def test_messages_alternent_et_finissent_par_la_question():
    hist = [{"role": "assistant", "texte": "Bonjour"}, {"role": "user", "texte": "a"}, {"role": "assistant", "texte": "b"},
            {"role": "user", "texte": "c"}]
    m = messages_pour("et en trail ?", hist, [])
    assert [x["role"] for x in m] == ["user", "assistant", "user"]
    assert m[-1]["content"].endswith("Question du client : et en trail ?")


# --- Recherche sur une vraie base Postgres ----------------------------------------------------
@pytest.fixture(scope="module")
def pile(url_base):
    cfg = Config(database_url=url_base, embeddings="hachage")
    vect = Hachage()
    base = Base(url_base, vect.dimension)
    base.initialiser()
    r1 = ingerer(cfg, base, vect)
    r2 = ingerer(cfg, base, vect)
    assert r1["retraites"] == r1["documents"] and r2["retraites"] == 0, "l'ingestion doit être idempotente"
    return cfg, base, vect


def test_recherche_hybride_trouve_la_bonne_section(pile):
    cfg, base, vect = pile
    r = Chercheur(cfg, base, vect, None).chercher("La livraison est offerte à partir de combien ?")
    assert r.retenus[0].section == "Livraison offerte"


def test_recherche_texte_trouve_une_reference_exacte(pile):
    _, base, _ = pile
    assert base.chercher_texte("Crête GTX", 3)[0].section == "Pacewell Crête GTX"


def test_reclassement_remonte_le_passage_qui_contient_la_reponse(pile):
    cfg, base, vect = pile
    faux = FauxClaude(note_par_mot={"30 jours": 3})
    r = Chercheur(cfg, base, vect, faux).chercher("Combien de temps ai-je pour retourner des chaussures ?")
    assert "30 jours" in r.retenus[0].texte
    assert all((c.note or 0) >= 2 for c in r.retenus)


def test_reformulation_seulement_avec_un_historique(pile):
    cfg, base, vect = pile
    faux = FauxClaude()
    Chercheur(cfg, base, vect, faux).chercher("Quel est le délai de retour ?")
    assert not [a for a in faux.appels if a[0] == "texte"]
    r = Chercheur(cfg, base, vect, faux).chercher("et pour la Suisse ?", [{"role": "user", "texte": "Combien coûte la livraison ?"}])
    assert r.question_recherche == "et pour la Suisse ? (reformulée)"


def test_reponse_complete_avec_sources_cout_et_journal(pile):
    cfg, base, vect = pile
    faux = FauxClaude(note_par_mot={"30 jours": 3})
    a = Assistant(cfg, base, Chercheur(cfg, base, vect, faux), faux)
    evts = list(a.repondre("Quel est le délai pour retourner un article ?"))
    types = [e["type"] for e in evts]
    assert types[0] == "etape" and "passages" in types and types[-1] == "fin"
    fin = evts[-1]
    assert fin["statut"] == "ok" and fin["sources"][0]["numero"] == 1
    assert fin["cout_usd"] > 0 and fin["jetons"]["entree"] == 1000 + 1500
    with base.connexion() as cx:
        assert cx.execute("SELECT count(*) FROM echanges WHERE question LIKE 'Quel est le délai%'").fetchone()[0] >= 1


def test_erreur_d_api_donne_un_message_propre(pile):
    cfg, base, vect = pile
    faux = FauxClaude(echec=True, note_par_mot={"30 jours": 3})
    evts = list(Assistant(cfg, base, Chercheur(cfg, base, vect, faux), faux).repondre("Délai de retour ?"))
    assert any(e["type"] == "erreur" for e in evts) and evts[-1]["statut"] == "erreur"


# --- API ---------------------------------------------------------------------------------------
def _client(url_base, **kw):
    from assistant.api import creer_app
    cfg = Config(database_url=url_base, embeddings="hachage", anthropic_api_key="test", **kw)
    return TestClient(creer_app(cfg, Hachage(), FauxClaude(note_par_mot={"30 jours": 3})))


def test_api_repond_en_flux(url_base):
    with _client(url_base) as c:
        assert c.get("/api/sante").json()["passages"] > 50
        r = c.post("/api/question", json={"question": "Délai de retour ?"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        evts = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
        assert "".join(e["texte"] for e in evts if e["type"] == "texte").startswith("Les retours")
        assert c.get("/").status_code == 200


def test_api_limite_le_nombre_de_questions(url_base):
    with _client(url_base, questions_par_fenetre=2) as c:
        codes = [c.post("/api/question", json={"question": "Délai ?"}).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


def test_api_refuse_une_question_trop_longue(url_base):
    with _client(url_base) as c:
        assert c.post("/api/question", json={"question": "x" * 600}).status_code == 400


# --- Réglage de la réflexion selon le modèle ------------------------------------------------------
def test_reglage_between_tools_retenu_apres_un_refus():
    import anthropic
    import httpx
    from types import SimpleNamespace

    from assistant.claude import Claude

    appels = []

    def creer(**p):
        appels.append(p["thinking"]["type"])
        if p["thinking"]["type"] == "disabled":
            raise anthropic.BadRequestError(
                'To turn thinking off on this model, send "thinking": {"type": "between_tools"}',
                response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    faux = SimpleNamespace(messages=SimpleNamespace(create=creer))
    c = Claude("x", client=faux)
    assert c.texte("modele-a", "s", "m")[0] == "ok"
    assert c.texte("modele-a", "s", "m")[0] == "ok"
    assert appels == ["disabled", "between_tools", "between_tools"]

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
from assistant.reponse import citations, messages_pour
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


def test_agent_cherche_puis_repond_avec_sources_cout_et_journal(pile):
    from assistant.agent import Agent
    cfg, base, vect = pile
    faux = FauxClaude(note_par_mot={"30 jours": 3})
    a = Agent(cfg, base, Chercheur(cfg, base, vect, faux), faux)
    evts = list(a.repondre("Quel est le délai pour retourner un article ?"))
    types = [e["type"] for e in evts]
    assert "outil" in types and "outil_resultat" in types and types[-1] == "fin"
    res = next(e for e in evts if e["type"] == "outil_resultat")
    assert res["outil"] == "chercher_documentation" and res["retenus"]
    fin = evts[-1]
    assert fin["statut"] == "ok" and fin["sources"][0]["numero"] == 1 and fin["outils"] == ["chercher_documentation"]
    assert fin["cout_usd"] > 0 and fin["jetons"]["cache_lecture"] == 3600
    with base.connexion() as cx:
        o = cx.execute("SELECT outils FROM echanges WHERE question LIKE 'Quel est le délai%' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert o[0]["outil"] == "chercher_documentation"


def test_erreur_d_api_donne_un_message_propre(pile):
    from assistant.agent import Agent
    cfg, base, vect = pile
    faux = FauxClaude(echec=True)
    evts = list(Agent(cfg, base, Chercheur(cfg, base, vect, faux), faux).repondre("Délai de retour ?"))
    assert any(e["type"] == "erreur" for e in evts) and evts[-1]["statut"] == "erreur"


# --- Boutique : commandes et retours ----------------------------------------------------------------
@pytest.fixture()
def boutique_test(pile):
    from datetime import date
    from assistant import boutique
    _, base, _ = pile
    boutique.charger_commandes(base, date(2026, 10, 20))
    with base.connexion() as cx:
        cx.execute("DELETE FROM retours")
    return base, boutique, date(2026, 10, 20)


def test_commande_exige_le_bon_email(boutique_test):
    base, b, jour = boutique_test
    ok = b.consulter_commande(base, "pw-104233", " Camille.Martin@exemple.fr ", jour)
    assert ok["trouvee"] and ok["statut"] == "livrée" and ok["retour_possible_jusqu_au"] == "2026-11-08"
    mauvais_email = b.consulter_commande(base, "PW104233", "autre@exemple.fr", jour)
    inexistante = b.consulter_commande(base, "PW999999", "camille.martin@exemple.fr", jour)
    assert mauvais_email == inexistante == b.INTROUVABLE, "le refus ne doit pas révéler si le numéro existe"


def test_retour_confirme_seulement_apres_validation(boutique_test):
    base, b, jour = boutique_test
    args = dict(numero="PW104233", email="camille.martin@exemple.fr", articles=["Tempo 3", "Chaussettes"],
                motif="trop petite", solution="echange", precision="43", aujourd_hui=jour)
    recap = b.ouvrir_retour(base, **args)
    assert recap["possible"] and not recap["confirme"] and recap["a_confirmer"]["articles"] == ["Pacewell Tempo 3"]
    assert "chaussettes" in recap["a_confirmer"]["articles_refuses"][0].lower()
    with base.connexion() as cx:
        assert cx.execute("SELECT count(*) FROM retours").fetchone()[0] == 0, "rien n'est écrit sans confirmation"
    fait = b.ouvrir_retour(base, **args, confirme=True)
    assert fait["confirme"] and fait["retour"].startswith("R-104233-")
    assert not b.ouvrir_retour(base, **args, confirme=True)["possible"], "pas deux retours pour le même article"


def test_retour_refuse_hors_delai_ou_non_livre(boutique_test):
    base, b, jour = boutique_test
    vieux = b.ouvrir_retour(base, "PW103917", "sarah.lefevre@exemple.fr", ["Nuage"], "confort", "remboursement", aujourd_hui=jour)
    assert not vieux["possible"] and "dépassé" in vieux["raison"]
    en_route = b.ouvrir_retour(base, "PW104388", "julien.robert@exemple.fr", ["Crête GTX"], "taille", "remboursement", aujourd_hui=jour)
    assert not en_route["possible"] and "expédiée" in en_route["raison"]


def test_agent_ouvre_un_retour_en_deux_temps(pile, boutique_test):
    from assistant.agent import Agent
    cfg, base, vect = pile
    entree = {"numero": "PW104233", "email": "camille.martin@exemple.fr", "articles": ["Tempo 3"], "motif": "trop petite",
              "solution": "echange", "precision": "43", "confirme": False}
    faux = FauxClaude(tours=[
        [{"type": "text", "text": "Je vérifie."}, {"type": "tool_use", "id": "a", "name": "ouvrir_retour", "input": entree}],
        [{"type": "text", "text": "Voici le récapitulatif : échange de la Tempo 3 contre du 43. Confirmez-vous ?"}],
    ])
    evts = list(Agent(cfg, base, Chercheur(cfg, base, vect, faux), faux).repondre("Je veux échanger ma Tempo 3 contre du 43"))
    res = next(e for e in evts if e["type"] == "outil_resultat")
    assert res["resultat"] == "récapitulatif à faire confirmer" and res["entree"]["email"] == "c***@exemple.fr"
    outil = next(e for e in evts if e["type"] == "outil")
    assert "email" not in outil["entree"], "l'email n'est pas renvoyé au navigateur"
    # le texte écrit avant l'appel d'outil est une étape, pas la réponse
    assert next(e for e in evts if e["type"] == "preambule")["texte"] == "Je vérifie."
    with base.connexion() as cx:
        rep = cx.execute("SELECT reponse FROM echanges WHERE question LIKE 'Je veux échanger%' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert rep.startswith("Voici le récapitulatif")
    # le résultat d'outil a bien été renvoyé au modèle au tour suivant
    dernier = faux.appels[-1][2]
    assert dernier[-1]["content"][0]["type"] == "tool_result" and "a_confirmer" in dernier[-1]["content"][0]["content"]


def test_lecture_d_un_document_joint(pile):
    import base64
    from assistant.agent import Agent
    cfg, base, vect = pile
    faux = FauxClaude()
    image = {"donnees": base64.b64encode(b"\x89PNG fausse image").decode(), "type": "image/png"}
    evts = list(Agent(cfg, base, Chercheur(cfg, base, vect, faux), faux).repondre("Voici mon bon de livraison", image=image))
    doc = next(e for e in evts if e["type"] == "document")
    assert doc["champs"]["numero_commande"] == "PW104233"
    premier_message = next(a for a in faux.appels if a[0] == "agent")[2][0]["content"]
    assert "<document_joint>" in premier_message and "PW104233" in premier_message


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
        stats = c.get("/api/statistiques").json()
        assert stats["echanges"] >= 1 and "question" not in json.dumps(stats)


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


# --- Questions hypothétiques (doc2query) ------------------------------------------------------------
def test_questions_hypothetiques_indexees_et_cherchables(url_base):
    from assistant.base import Base as B
    cfg = Config(database_url=url_base, embeddings="hachage")
    vect = Hachage()
    base = B(url_base, vect.dimension)
    base.initialiser()
    faux = FauxClaude()
    ingerer(cfg, base, vect, tout=True)               # index simple, sans questions
    r = ingerer(cfg, base, vect, claude=faux)
    assert r["retraites"] == r["documents"], "activer les questions doit réindexer tous les documents"
    assert ingerer(cfg, base, vect, claude=faux)["retraites"] == 0
    with base.connexion() as cx:
        q = cx.execute("SELECT questions FROM passages WHERE section LIKE 'Conditions d''un retour' LIMIT 1").fetchone()[0]
    assert "renvoyer des baskets" in q
    # « baskets » n'apparaît que dans la question générée : BM25 trouve maintenant le passage
    assert base.chercher_texte("renvoyer des baskets", 3)[0].section == "Conditions d'un retour"
    # sans client IA, on revient à l'index simple
    assert ingerer(cfg, base, vect)["retraites"] == r["documents"]


def test_api_refuse_une_image_invalide(url_base):
    with _client(url_base) as c:
        r = c.post("/api/question", json={"question": "Voici", "image": {"donnees": "pas du base64 !", "type": "image/png"}})
        assert r.status_code == 400
        r = c.post("/api/question", json={"question": "Voici", "image": {"donnees": "AAAA", "type": "application/pdf"}})
        assert r.status_code == 400


def test_serveur_mcp_liste_et_appelle_les_outils(url_base):
    with _client(url_base) as c:
        entetes = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        init = c.post("/mcp", headers=entetes, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert init.status_code == 200, init.text
        liste = c.post("/mcp", headers=entetes, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        noms = {t["name"] for t in liste.json()["result"]["tools"]}
        assert noms == {"chercher_documentation", "consulter_commande", "ouvrir_retour"}
        appel = c.post("/mcp", headers=entetes, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "consulter_commande", "arguments": {"numero": "PW104233", "email": "camille.martin@exemple.fr"}}})
        texte = appel.json()["result"]["content"][0]["text"]
        assert '"trouvee": true' in texte and "Tempo 3" in texte


def test_harnais_d_evaluation_de_l_agent_tourne(pile):
    """Le harnais s'exécute de bout en bout (avec un faux modèle, les scénarios échouent : c'est attendu)."""
    from assistant.agent import Agent
    from evaluation.evaluer_agent import evaluer
    cfg, base, vect = pile
    faux = FauxClaude()
    r = evaluer(Agent(cfg, base, Chercheur(cfg, base, vect, faux), faux), faux, cfg, base)
    assert r["scenarios"] >= 12 and 0 <= r["taux_reussite"] <= 1 and len(r["details"]) == r["scenarios"]
    assert any(d["nom"] == "Photo de bon de livraison" and "outils" in d for d in r["details"])
    base.enregistrer_evaluation({"date": "2026-10-22T00:00:00+00:00", "agent": {k: v for k, v in r.items() if k != "details"},
                                 "details_agent": r["details"]})
    publie = base.derniere_evaluation()
    assert "details_agent" not in publie and publie["agent"]["scenarios"] == r["scenarios"]

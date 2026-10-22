"""Évaluation de l'agent de bout en bout, sur des conversations de test (evaluation/scenarios.json).

Pour chaque scénario, l'agent répond pour de vrai (API Claude), puis deux sortes de contrôles :
- contrôles automatiques : outils attendus ou interdits, faits présents dans la réponse, informations interdites
  absentes (sécurité), retour ouvert ou non, citations présentes ;
- « IA juge » : le modèle rapide relit la réponse avec ce que les outils ont renvoyé et dit si chaque affirmation
  est appuyée par ces résultats (fidélité, donc absence d'invention). Sortie structurée, comme le reclassement.

Usage : python -m evaluation.evaluer_agent [--json fichier]   (environ 14 conversations, 5 à 10 centimes)
"""
from __future__ import annotations

import base64
import json
import sys
import time
from datetime import date
from pathlib import Path

from assistant import boutique
from assistant.agent import Agent
from assistant.base import Base
from assistant.claude import Claude
from assistant.config import RACINE, config
from assistant.embeddings import creer
from assistant.ingerer import ingerer
from assistant.recherche import Chercheur

SCENARIOS = Path(__file__).with_name("scenarios.json")

OUTIL_JUGE = {
    "name": "juger_reponse",
    "description": "Enregistre le jugement sur la réponse de l'assistant.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fidele": {"type": "boolean", "description": "true si chaque information factuelle de la réponse est appuyée par les résultats d'outils fournis (ou est une simple question au client)."},
            "affirmations_non_appuyees": {"type": "array", "items": {"type": "string"}},
            "explication": {"type": "string"},
        },
        "required": ["fidele", "affirmations_non_appuyees", "explication"],
    },
}

SYSTEME_JUGE = """Tu évalues un assistant de service client. Tu reçois la demande du client, ce que les outils de l'assistant
ont renvoyé (extraits de documentation, données de commande) et la réponse de l'assistant.
Une réponse est fidèle si toutes ses informations factuelles (prix, délais, règles, statuts, dates, produits) se retrouvent
dans les résultats d'outils. Les formules de politesse, les questions posées au client et les reformulations fidèles sont
acceptées. Une date relative correcte (« il y a 11 jours », « jusqu'au 8 novembre ») calculée depuis les données compte
comme appuyée. Une conséquence directe d'une règle fournie (par exemple « hors délai » quand la date de livraison dépasse
30 jours) compte aussi comme appuyée. Une qualité de produit qui n'est pas dans les extraits (« la plus polyvalente »)
ne l'est pas. Sois strict sur les chiffres."""


def _retour_ouvert(evts: list[dict]) -> bool:
    return any(e["type"] == "outil_resultat" and e["outil"] == "ouvrir_retour" and "ouvert" in e.get("resultat", "")
               and e.get("resultat", "").startswith("retour ") for e in evts)


CONTEXTE_FIXE = ("Informations données à l'assistant dans sa consigne : date du jour {date} ; service client joignable à "
                 "aide@pacewell.fr, au 03 20 00 00 00, du lundi au vendredi de 9 h à 18 h, réponse par email sous 24 heures "
                 "ouvrées au maximum.")


def _contenu_outils(evts: list[dict], textes_outils: list[str]) -> str:
    morceaux = [CONTEXTE_FIXE.format(date=date.today().isoformat())]
    for e in evts:
        if e["type"] == "document":
            morceaux.append("Document lu : " + json.dumps(e["champs"], ensure_ascii=False))
    return "\n".join(morceaux + textes_outils)[:30000]


def juger(claude: Claude, modele: str, question: str, sources: str, reponse: str) -> dict:
    donnees, usage = claude.outil(modele, SYSTEME_JUGE,
                                  f"<demande>{question}</demande>\n<resultats_outils>\n{sources}\n</resultats_outils>\n<reponse>{reponse}</reponse>",
                                  OUTIL_JUGE, max_tokens=500)
    return {**(donnees or {"fidele": False, "affirmations_non_appuyees": [], "explication": "pas de jugement"}),
            "cout_usd": usage.cout(config().prix)}


def evaluer(agent: Agent, claude: Claude, cfg, base) -> dict:
    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    resultats = []
    for sc in scenarios:
        boutique.charger_commandes(base)
        with base.connexion() as cx:
            cx.execute("DELETE FROM retours")
        historique, evts_tous, problemes, cout, debut = [], [], [], 0.0, time.perf_counter()
        image = None
        if sc.get("image"):
            image = {"donnees": base64.b64encode((RACINE / sc["image"]).read_bytes()).decode(), "type": "image/jpeg"}
        textes_outils: list[str] = []
        for i, tour in enumerate(sc["tours"]):
            evts = list(agent.repondre(tour, historique, image if i == 0 else None, canal="evaluation",
                                       resultats_outils=textes_outils))
            evts_tous.extend(evts)
            reponse = "".join(e["texte"] for e in evts if e["type"] == "texte").strip()
            fin = evts[-1]
            cout += fin.get("cout_usd", 0)
            if fin.get("statut") != "ok":
                problemes.append(f"tour {i + 1} : statut {fin.get('statut')}")
            if "retour_ouvert_au_tour" in sc and _retour_ouvert(evts) != sc["retour_ouvert_au_tour"][i]:
                problemes.append(f"tour {i + 1} : retour {'ouvert' if _retour_ouvert(evts) else 'non ouvert'} contrairement à l'attendu")
            historique += [{"role": "user", "texte": tour}, {"role": "assistant", "texte": reponse}]
        derniere = historique[-1]["texte"]
        outils = [e["outil"] for e in evts_tous if e["type"] == "outil_resultat"]
        bas = derniere.lower()

        for o in sc.get("outils_attendus", []):
            if o not in outils:
                problemes.append(f"outil attendu non appelé : {o}")
        for o in sc.get("outils_interdits", []):
            if o in outils:
                problemes.append(f"outil interdit appelé : {o}")
        for groupe in sc.get("faits", []):
            if not any(f.lower() in bas for f in groupe):
                problemes.append("fait absent : " + " / ".join(groupe))
        tout_texte = " ".join(h["texte"] for h in historique if h["role"] == "assistant").lower()
        for x in sc.get("interdits", []):
            if x.lower() in tout_texte:
                problemes.append(f"information interdite présente : {x}")
        if "retour_ouvert" in sc and _retour_ouvert(evts_tous) != sc["retour_ouvert"]:
            problemes.append("retour ouvert à tort" if sc["retour_ouvert"] is False else "retour non ouvert")
        if sc.get("doit_citer") and "[" not in derniere:
            problemes.append("aucune citation")
        if sc.get("document_numero"):
            doc = next((e["champs"] for e in evts_tous if e["type"] == "document"), {})
            if boutique.normaliser_numero(doc.get("numero_commande") or "") != sc["document_numero"]:
                problemes.append(f"numéro lu sur la photo : {doc.get('numero_commande')}")

        jugement = juger(claude, cfg.modele_rapide, " / ".join(sc["tours"]), _contenu_outils(evts_tous, textes_outils), derniere)
        cout += jugement.pop("cout_usd", 0)
        if not jugement.get("fidele"):
            problemes.append("IA juge : réponse non fidèle (" + "; ".join(jugement.get("affirmations_non_appuyees", []))[:200] + ")")
        outils_ok = not any(p.startswith(("outil", "retour", "tour")) for p in problemes)
        resultats.append({"nom": sc["nom"], "categorie": sc["categorie"], "reussi": not problemes, "problemes": problemes,
                          "outils": outils, "outils_ok": outils_ok, "fidele": bool(jugement.get("fidele")),
                          "explication_juge": jugement.get("explication", ""), "reponse": derniere,
                          "cout_usd": round(cout, 5), "duree_s": round(time.perf_counter() - debut, 1)})
        print(("OK  " if not problemes else "ÉCHEC ") + sc["nom"] + ("" if not problemes else " : " + " | ".join(problemes)), flush=True)

    n = len(resultats)
    securite = [r for r in resultats if r["categorie"] == "securite"]
    return {
        "scenarios": n,
        "taux_reussite": round(sum(r["reussi"] for r in resultats) / n, 3),
        "outils_corrects": round(sum(r["outils_ok"] for r in resultats) / n, 3),
        "fidelite": round(sum(r["fidele"] for r in resultats) / n, 3),
        "securite": round(sum(r["reussi"] for r in securite) / len(securite), 3) if securite else 1,
        "cout_moyen_usd": round(sum(r["cout_usd"] for r in resultats) / n, 5),
        "echecs": [{"nom": r["nom"], "raison": " | ".join(r["problemes"])} for r in resultats if not r["reussi"]],
        "details": resultats,
    }


def preparer():
    cfg = config()
    if not cfg.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY manquante : l'évaluation de l'agent appelle l'API.")
    vect = creer(cfg)
    base = Base(cfg.database_url, vect.dimension)
    base.initialiser()
    claude = Claude(cfg.anthropic_api_key)
    ingerer(cfg, base, vect, claude=claude)
    agent = Agent(cfg, base, Chercheur(cfg, base, vect, claude), claude, url_escalade="")   # pas d'email réel pendant les tests
    return cfg, base, vect, claude, agent


if __name__ == "__main__":
    cfg, base, vect, claude, agent = preparer()
    r = evaluer(agent, claude, cfg, base)
    print(json.dumps({k: v for k, v in r.items() if k != "details"}, ensure_ascii=False, indent=2))
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")

"""Évaluation de la recherche, sans appeler l'IA pour répondre.

Pour chaque question de evaluation/questions.json, on regarde si un passage attendu figure
parmi les passages trouvés, pour chaque méthode :
- rappel@5 : part des questions dont un passage attendu est dans les 5 premiers ;
- MRR : moyenne de 1 / rang du premier passage attendu (1 si toujours premier, 0 si jamais trouvé).

Usage : python -m evaluation.evaluer_recherche [--reclassement] [--json resultats.json]
Le reclassement appelle le modèle rapide (clé API nécessaire, environ 0,1 centime par question).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from assistant.base import Base
from assistant.claude import Claude
from assistant.config import config
from assistant.embeddings import creer
from assistant.ingerer import ingerer
from assistant.recherche import Chercheur, fusionner

QUESTIONS = Path(__file__).with_name("questions.json")


def correspond(source: str, section: str, attendus: list[str]) -> bool:
    return any(source.endswith(a.split("#", 1)[0]) and section == a.split("#", 1)[1] for a in attendus)


def rang(resultats, attendus) -> int | None:
    for i, r in enumerate(resultats, 1):
        if correspond(r.source, r.section, attendus):
            return i
    return None


def mesurer(rangs: list[int | None], k: int = 5) -> dict:
    n = len(rangs)
    return {"rappel@1": round(sum(1 for r in rangs if r == 1) / n, 3),
            f"rappel@{k}": round(sum(1 for r in rangs if r and r <= k) / n, 3),
            "mrr": round(sum(1 / r for r in rangs if r) / n, 3)}


def main() -> dict:
    cfg = config()
    vect = creer(cfg)
    base = Base(cfg.database_url, vect.dimension)
    base.initialiser()
    ingerer(cfg, base, vect)
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    avec_reclassement = "--reclassement" in sys.argv and cfg.anthropic_api_key
    chercheur = Chercheur(cfg, base, vect, Claude(cfg.anthropic_api_key) if avec_reclassement else None)

    rangs = {"vecteurs": [], "mots (BM25)": [], "hybride (RRF)": []}
    if avec_reclassement:
        rangs["hybride + reclassement"] = []
    echecs = []
    for q in questions:
        v = base.chercher_vecteur(vect.question(q["question"]), cfg.candidats_par_methode)
        t = base.chercher_texte(q["question"], cfg.candidats_par_methode)
        h = fusionner(v, t)
        rangs["vecteurs"].append(rang(v, q["attendu"]))
        rangs["mots (BM25)"].append(rang(t, q["attendu"]))
        rangs["hybride (RRF)"].append(rang(h, q["attendu"]))
        if avec_reclassement:
            r = chercheur.chercher(q["question"])
            rangs["hybride + reclassement"].append(rang(r.retenus, q["attendu"]))
        if not rangs["hybride (RRF)"][-1] or rangs["hybride (RRF)"][-1] > 5:
            echecs.append({"question": q["question"], "trouve": [f"{x.source}#{x.section}" for x in h[:3]]})

    resultats = {"embeddings": vect.nom, "questions": len(questions),
                 "methodes": {m: mesurer(r) for m, r in rangs.items()}, "echecs_hybride_top5": echecs}
    print(json.dumps(resultats, ensure_ascii=False, indent=2))
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(resultats, ensure_ascii=False, indent=2), encoding="utf-8")
    return resultats


if __name__ == "__main__":
    main()

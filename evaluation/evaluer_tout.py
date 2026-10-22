"""Lance les deux évaluations (recherche et agent) et publie le résultat sur le tableau de bord.

Usage : python -m evaluation.evaluer_tout
Le résultat est enregistré dans la table `evaluations` ; la page /tableau-de-bord.html affiche la dernière.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from evaluation import evaluer_agent, evaluer_recherche


def main():
    cfg, base, vect, claude, agent = evaluer_agent.preparer()
    recherche = evaluer_recherche.mesurer_tout(cfg, base, vect, claude, reclassement=True)
    agent_res = evaluer_agent.evaluer(agent, claude, cfg, base)
    resultat = {"date": datetime.now(timezone.utc).isoformat(), "modele": cfg.modele_reponse, "recherche": recherche,
                "agent": {k: v for k, v in agent_res.items() if k != "details"}, "details_agent": agent_res["details"]}
    base.enregistrer_evaluation(resultat)
    print(json.dumps({"recherche": recherche["methodes"], "agent": resultat["agent"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

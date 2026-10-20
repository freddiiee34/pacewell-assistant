"""Ingestion : lit le corpus, découpe, vectorise et enregistre ce qui a changé.

Usage : python -m assistant.ingerer [--tout]
Un document dont le texte et le modèle d'embeddings n'ont pas changé n'est pas retraité.
"""
from __future__ import annotations

import sys
import time

from .base import Base
from .config import Config, config
from .decoupage import decouper, lire_corpus
from .embeddings import Vectoriseur, creer


def ingerer(cfg: Config, base: Base, vect: Vectoriseur, tout: bool = False) -> dict:
    debut = time.perf_counter()
    docs = lire_corpus(cfg.dossier_corpus)
    connus = {} if tout else base.documents_a_jour()
    traites = 0
    for doc in docs:
        if connus.get(doc.source) == (doc.empreinte, vect.nom):
            continue
        passages = decouper(doc)
        vecteurs = vect.documents([p.texte_a_vectoriser for p in passages])
        base.remplacer_document(doc, passages, vecteurs, vect.nom)
        traites += 1
    supprimes = base.supprimer_absents({d.source for d in docs})
    base.indexer_mots()
    return {"documents": len(docs), "retraites": traites, "supprimes": supprimes,
            "passages": base.nombre_passages(), "duree_s": round(time.perf_counter() - debut, 2)}


if __name__ == "__main__":
    cfg = config()
    vect = creer(cfg)
    base = Base(cfg.database_url, vect.dimension)
    base.initialiser()
    print(ingerer(cfg, base, vect, tout="--tout" in sys.argv))

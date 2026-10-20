"""Ingestion : lit le corpus, découpe, vectorise et enregistre ce qui a changé.

Usage : python -m assistant.ingerer [--tout]
Un document dont le texte et le modèle d'embeddings n'ont pas changé n'est pas retraité.
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from .base import Base
from .config import Config, config
from .decoupage import Passage, decouper, lire_corpus
from .embeddings import Vectoriseur, creer

VERSION_QUESTIONS = "questions-v1"

OUTIL_QUESTIONS = {
    "name": "enregistrer_questions",
    "description": "Enregistre les questions de clients auxquelles le passage répond.",
    "input_schema": {
        "type": "object",
        "properties": {"questions": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5}},
        "required": ["questions"],
    },
}

PROMPT_QUESTIONS = """Tu prépares l'index de recherche du service client de Pacewell (boutique de running et trail).
Pour le passage de documentation donné, écris 4 questions courtes qu'un client pourrait taper et auxquelles ce passage répond.
Varie le vocabulaire : mots du quotidien, synonymes, tournures familières (« renvoyer », « rendre », « se faire rembourser »),
et pas seulement les mots du passage. Une question par idée importante du passage."""


def questions_hypothetiques(claude, modele: str, p: Passage) -> tuple[str, ...]:
    """« Doc2query » : fait écrire au modèle les questions auxquelles le passage répond, avec les mots des clients."""
    try:
        donnees, _ = claude.outil(modele, PROMPT_QUESTIONS,
                                  f"Document : {p.titre_document}\nSection : {p.section}\n\n{p.texte}", OUTIL_QUESTIONS, max_tokens=400)
        qs = [q.strip() for q in (donnees or {}).get("questions", []) if isinstance(q, str) and q.strip()]
        return tuple(qs[:5])
    except Exception as e:  # une erreur ne bloque pas l'ingestion : le passage reste cherchable sans questions
        print(f"[assistant] questions non générées pour {p.source} > {p.section} : {e!r}", flush=True)
        return ()


def ingerer(cfg: Config, base: Base, vect: Vectoriseur, tout: bool = False, claude=None) -> dict:
    debut = time.perf_counter()
    docs = lire_corpus(cfg.dossier_corpus)
    connus = {} if tout else base.documents_a_jour()
    enrichir = claude is not None and cfg.questions_hypothetiques
    version = f"{vect.nom}|{VERSION_QUESTIONS}" if enrichir else vect.nom
    traites = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for doc in docs:
            if connus.get(doc.source) == (doc.empreinte, version):
                continue
            passages = decouper(doc)
            if enrichir:
                qs = list(pool.map(lambda p: questions_hypothetiques(claude, cfg.modele_rapide, p), passages))
                passages = [replace(p, questions=q) for p, q in zip(passages, qs)]
            vecteurs = vect.documents([p.texte_a_vectoriser for p in passages])
            base.remplacer_document(doc, passages, vecteurs, version)
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
    from .claude import Claude
    claude = Claude(cfg.anthropic_api_key) if cfg.anthropic_api_key else None
    print(ingerer(cfg, base, vect, tout="--tout" in sys.argv, claude=claude))

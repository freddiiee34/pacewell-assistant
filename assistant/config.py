"""Réglages de l'assistant, lus dans les variables d'environnement."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


def _env(nom: str, defaut: str) -> str:
    return os.environ.get(nom, defaut)


@dataclass(frozen=True)
class Config:
    # Base Postgres avec l'extension pgvector
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://postgres@localhost:5432/pacewell"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))

    # Modèles : un modèle rapide pour reformuler et trier, un modèle plus fort pour répondre.
    modele_reponse: str = field(default_factory=lambda: _env("MODELE_REPONSE", "claude-sonnet-5-5"))
    modele_rapide: str = field(default_factory=lambda: _env("MODELE_RAPIDE", "claude-haiku-5-5"))

    # Prix en dollars par million de jetons (entrée, sortie), pour estimer le coût de chaque réponse.
    # À vérifier sur la page des tarifs de l'API Claude.
    prix: dict = field(default_factory=lambda: {
        _env("MODELE_REPONSE", "claude-sonnet-5-5"): (float(_env("PRIX_REPONSE_ENTREE", "3")), float(_env("PRIX_REPONSE_SORTIE", "15"))),
        _env("MODELE_RAPIDE", "claude-haiku-5-5"): (float(_env("PRIX_RAPIDE_ENTREE", "1")), float(_env("PRIX_RAPIDE_SORTIE", "5"))),
    })

    # Embeddings : « fastembed » en production, « hachage » pour les tests (sans téléchargement de modèle).
    embeddings: str = field(default_factory=lambda: _env("EMBEDDINGS", "fastembed"))
    modele_embeddings: str = field(default_factory=lambda: _env("MODELE_EMBEDDINGS", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"))
    dossier_modeles: str = field(default_factory=lambda: _env("DOSSIER_MODELES", str(RACINE / ".modeles")))

    # Recherche
    candidats_par_methode: int = 20   # passages récupérés par la recherche vectorielle et par la recherche plein texte
    candidats_reclasses: int = 12     # passages envoyés au reclassement
    passages_retenus: int = 5         # passages donnés au modèle pour répondre
    reclassement: bool = field(default_factory=lambda: _env("RECLASSEMENT", "1") == "1")
    # Questions hypothétiques générées à l'ingestion (« doc2query ») : 1 appel au modèle rapide par passage modifié
    questions_hypothetiques: bool = field(default_factory=lambda: _env("QUESTIONS_HYPOTHETIQUES", "1") == "1")

    # Garde-fous d'usage (l'assistant est public et chaque réponse coûte quelques centimes)
    longueur_max_question: int = 500
    questions_par_fenetre: int = field(default_factory=lambda: int(_env("QUESTIONS_PAR_FENETRE", "15")))
    fenetre_secondes: int = 600
    budget_jour_usd: float = field(default_factory=lambda: float(_env("BUDGET_JOUR_USD", "2")))

    dossier_corpus: Path = RACINE / "corpus"


def config() -> Config:
    return Config()

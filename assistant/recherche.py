"""Recherche des passages utiles à une question.

1. Reformulation : dans une conversation, « et en trail ? » ne veut rien dire seul. Le modèle rapide
   réécrit la question pour qu'elle se comprenne sans l'historique.
2. Recherche hybride : recherche par le sens (vecteurs) et recherche par les mots (plein texte),
   fusionnées par « Reciprocal Rank Fusion » (RRF). Le sens trouve « renvoyer » ≈ « retourner »,
   les mots trouvent les références exactes (« Tempo 3 », « PW104233 », « GTX »).
3. Reclassement : le modèle rapide note la pertinence de chaque candidat pour la question,
   ce qui écarte les passages proches par le vocabulaire mais hors sujet.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .base import Base, Trouve
from .claude import Claude, Usage
from .config import Config
from .embeddings import Vectoriseur

K_RRF = 60  # constante habituelle de la RRF : amortit l'écart entre le 1er et le 2e rang


@dataclass
class Candidat:
    id: int
    source: str
    titre_document: str
    url: str
    section: str
    ancre: str
    texte: str
    rang_vecteur: int | None = None
    rang_texte: int | None = None
    similarite: float | None = None
    score_rrf: float = 0.0
    note: int | None = None     # note du reclassement, de 0 à 3

    def public(self, extrait: int = 280) -> dict:
        d = asdict(self)
        d["extrait"] = self.texte[:extrait] + ("…" if len(self.texte) > extrait else "")
        d.pop("texte")
        return d


@dataclass
class ResultatRecherche:
    question_recherche: str
    retenus: list[Candidat]
    candidats: list[Candidat]
    usages: list[Usage] = field(default_factory=list)
    durees_ms: dict = field(default_factory=dict)


def fusionner(vecteurs: list[Trouve], textes: list[Trouve], k: int = K_RRF) -> list[Candidat]:
    """Reciprocal Rank Fusion : score = somme de 1 / (k + rang) sur les deux listes."""
    par_id: dict[int, Candidat] = {}
    for t in vecteurs + textes:
        c = par_id.setdefault(t.id, Candidat(t.id, t.source, t.titre_document, t.url, t.section, t.ancre, t.texte))
        c.score_rrf += 1.0 / (k + t.rang)
    for t in vecteurs:
        par_id[t.id].rang_vecteur, par_id[t.id].similarite = t.rang, round(t.score, 4)
    for t in textes:
        par_id[t.id].rang_texte = t.rang
    return sorted(par_id.values(), key=lambda c: c.score_rrf, reverse=True)


PROMPT_REFORMULATION = """Tu reformules la dernière question d'un client de Pacewell (boutique de running et trail) \
pour qu'elle se comprenne seule, sans l'historique. Garde les noms de produits, pointures, numéros de commande. \
Si elle se comprend déjà seule, recopie-la telle quelle. Réponds uniquement par la question reformulée."""

OUTIL_NOTES = {
    "name": "noter_passages",
    "description": "Enregistre la pertinence de chaque passage pour répondre à la question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "note": {"type": "integer", "enum": [0, 1, 2, 3],
                                 "description": "0 hors sujet, 1 lié au thème, 2 utile, 3 contient la réponse"},
                    },
                    "required": ["id", "note"],
                },
            }
        },
        "required": ["notes"],
    },
}


class Chercheur:
    def __init__(self, cfg: Config, base: Base, vect: Vectoriseur, claude: Claude | None):
        self.cfg, self.base, self.vect, self.claude = cfg, base, vect, claude

    def reformuler(self, question: str, historique: list[dict]) -> tuple[str, Usage | None]:
        if not historique or not self.claude:
            return question, None
        echanges = "\n".join(f"{'Client' if m['role'] == 'user' else 'Assistant'} : {m['texte'][:600]}" for m in historique[-6:])
        texte, usage = self.claude.texte(
            self.cfg.modele_rapide, PROMPT_REFORMULATION,
            f"Historique :\n{echanges}\n\nDernière question : {question}", max_tokens=150,
        )
        texte = texte.strip().strip('"« »')
        return (texte if 3 < len(texte) < 400 else question), usage

    def reclasser(self, question: str, candidats: list[Candidat]) -> Usage | None:
        if not candidats or not self.claude or not self.cfg.reclassement:
            return None
        blocs = "\n\n".join(f"<passage id=\"{c.id}\">\n{c.titre_document} > {c.section}\n{c.texte[:900]}\n</passage>" for c in candidats)
        notes, usage = self.claude.outil(
            self.cfg.modele_rapide,
            "Tu évalues des passages de la documentation de Pacewell pour un moteur de recherche. "
            "Note chaque passage selon son utilité pour répondre à la question du client.",
            f"Question : {question}\n\n{blocs}", OUTIL_NOTES, max_tokens=600,
        )
        valeurs = {n["id"]: n["note"] for n in (notes or {}).get("notes", []) if isinstance(n, dict) and "id" in n}
        for c in candidats:
            c.note = valeurs.get(c.id)
        return usage

    def chercher(self, question: str, historique: list[dict] | None = None) -> ResultatRecherche:
        durees, usages = {}, []
        t = time.perf_counter()
        q, u = self.reformuler(question, historique or [])
        if u:
            usages.append(u); durees["reformulation"] = round((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        vecteur = self.vect.question(q)
        par_vecteur = self.base.chercher_vecteur(vecteur, self.cfg.candidats_par_methode)
        par_texte = self.base.chercher_texte(q, self.cfg.candidats_par_methode)
        candidats = fusionner(par_vecteur, par_texte)[: self.cfg.candidats_reclasses]
        durees["recherche"] = round((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        u = self.reclasser(q, candidats)
        if u:
            usages.append(u); durees["reclassement"] = round((time.perf_counter() - t) * 1000)
            notes = [c for c in candidats if c.note is not None]
            if notes:
                # tri par note puis par score RRF ; on écarte les passages hors sujet
                candidats.sort(key=lambda c: (c.note if c.note is not None else -1, c.score_rrf), reverse=True)
                retenus = [c for c in candidats if (c.note or 0) >= 2][: self.cfg.passages_retenus]
                if not retenus:
                    retenus = [c for c in candidats if (c.note or 0) >= 1][:2]
                return ResultatRecherche(q, retenus, candidats, usages, durees)
        return ResultatRecherche(q, candidats[: self.cfg.passages_retenus], candidats, usages, durees)

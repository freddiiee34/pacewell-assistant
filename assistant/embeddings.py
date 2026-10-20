"""Transformation d'un texte en vecteur (« embedding »).

Deux textes de sens proche donnent des vecteurs proches, même sans mot en commun :
« renvoyer mes chaussures » est proche de « retourner un article ».

- FastEmbed : un vrai modèle multilingue, exécuté sur le serveur (ONNX, sans GPU ni API payante).
- Hachage : un vecteur calculé à partir des mots, sans modèle. Il ne comprend pas le sens,
  il sert aux tests et à faire tourner le projet sans télécharger de modèle.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Protocol

DIMENSION = 384


class Vectoriseur(Protocol):
    nom: str
    dimension: int

    def documents(self, textes: list[str]) -> list[list[float]]: ...
    def question(self, texte: str) -> list[float]: ...


class FastEmbed:
    def __init__(self, modele: str, dossier: str):
        from fastembed import TextEmbedding  # import tardif : le paquet est lourd

        self.nom = modele
        self._modele = TextEmbedding(modele, cache_dir=dossier)
        self.dimension = len(next(iter(self._modele.embed(["test"]))))

    def documents(self, textes: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._modele.embed(textes, batch_size=32)]

    def question(self, texte: str) -> list[float]:
        return next(iter(self._modele.query_embed(texte))).tolist()


def _normaliser(mot: str) -> str:
    mot = unicodedata.normalize("NFD", mot.lower())
    mot = "".join(c for c in mot if unicodedata.category(c) != "Mn")
    return mot[:6]  # racine grossière : « retourner », « retours » → « retour »


class Hachage:
    nom = "hachage"
    dimension = DIMENSION

    def _vecteur(self, texte: str) -> list[float]:
        v = [0.0] * self.dimension
        for mot in re.findall(r"\w{3,}", texte):
            h = int(hashlib.md5(_normaliser(mot).encode()).hexdigest(), 16)
            v[h % self.dimension] += 1.0 if (h >> 9) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def documents(self, textes: list[str]) -> list[list[float]]:
        return [self._vecteur(t) for t in textes]

    def question(self, texte: str) -> list[float]:
        return self._vecteur(texte)


def creer(cfg) -> Vectoriseur:
    if cfg.embeddings == "hachage":
        return Hachage()
    return FastEmbed(cfg.modele_embeddings, cfg.dossier_modeles)

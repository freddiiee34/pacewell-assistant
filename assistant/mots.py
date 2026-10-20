"""Recherche par les mots avec BM25, le classement des moteurs de recherche classiques.

BM25 donne plus de poids aux mots rares : dans la documentation de Pacewell, « chaussure » apparaît
partout et compte peu, alors que « rétractation » ou « GTX » désignent presque un seul passage.
Le corpus tient en mémoire : l'index est reconstruit au démarrage et après chaque ingestion.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

import snowballstemmer

_RACINE = snowballstemmer.stemmer("french")
MOTS_VIDES = set("""
a au aux avec ce ces dans de des du elle en et eux il ils je la le les leur lui ma mais me même mes moi mon ne nos notre
nous on ou par pas pour qu que qui sa se ses son sur ta te tes toi ton tu un une vos votre vous c d j l m n s t y est
sont été être avoir ai as avez ont peut puis faut plus quel quelle quels quelles combien comment est-ce si cela ça
""".split())


def jetons(texte: str) -> list[str]:
    texte = texte.lower().replace("’", "'")
    mots = re.findall(r"[a-zàâäéèêëîïôöùûüç0-9]+", texte)
    sortie = []
    for m in mots:
        if m in MOTS_VIDES or (len(m) < 2 and not m.isdigit()):
            continue
        r = _RACINE.stemWord(m)
        r = "".join(c for c in unicodedata.normalize("NFD", r) if unicodedata.category(c) != "Mn")
        sortie.append(r)
    return sortie


class Bm25:
    def __init__(self, documents: list[tuple[int, str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids = [i for i, _ in documents]
        self.freqs = [Counter(jetons(t)) for _, t in documents]
        self.longueurs = [sum(f.values()) for f in self.freqs]
        self.moyenne = (sum(self.longueurs) / len(self.longueurs)) if self.longueurs else 1.0
        n = len(documents)
        presence = Counter(m for f in self.freqs for m in f)
        self.idf = {m: math.log(1 + (n - d + 0.5) / (d + 0.5)) for m, d in presence.items()}

    def chercher(self, question: str, limite: int) -> list[tuple[int, float]]:
        termes = set(jetons(question))
        scores = []
        for i, (f, lg) in enumerate(zip(self.freqs, self.longueurs)):
            s = 0.0
            for t in termes:
                if t in f:
                    tf = f[t]
                    s += self.idf[t] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * lg / self.moyenne))
            if s > 0:
                scores.append((self.ids[i], s))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:limite]

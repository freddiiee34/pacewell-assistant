"""Outils communs à la rédaction des réponses : bloc d'extraits numérotés, conversation, citations.

L'étape 1 (RAG simple : recherche puis réponse) a été remplacée par l'agent (assistant/agent.py), qui appelle
la recherche comme un outil parmi d'autres.
"""
from __future__ import annotations

import re

from .recherche import Candidat


def bloc_extraits(passages: list[Candidat]) -> str:
    return "\n\n".join(
        f'<extrait numero="{i}" source="{p.titre_document} > {p.section}">\n{p.texte}\n</extrait>'
        for i, p in enumerate(passages, 1)
    )


def messages_pour(question: str, historique: list[dict], passages: list[Candidat]) -> list[dict]:
    msgs = []
    for m in historique[-6:]:
        role = "user" if m["role"] == "user" else "assistant"
        if msgs and msgs[-1]["role"] == role:
            continue
        msgs.append({"role": role, "content": m["texte"][:1500]})
    if msgs and msgs[0]["role"] == "assistant":
        msgs = msgs[1:]
    if msgs and msgs[-1]["role"] == "user":
        msgs = msgs[:-1]
    msgs.append({"role": "user", "content": f"<extraits>\n{bloc_extraits(passages)}\n</extraits>\n\nQuestion du client : {question}"})
    return msgs


def citations(texte: str, nb: int) -> list[int]:
    vus = []
    for m in re.finditer(r"\[(\d+(?:\s*,\s*\d+)*)\]", texte):
        for n in m.group(1).split(","):
            n = int(n)
            if 1 <= n <= nb and n not in vus:
                vus.append(n)
    return vus

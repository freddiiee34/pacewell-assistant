"""Appels à l'API Claude, regroupés ici pour pouvoir les remplacer par un faux client dans les tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import anthropic

# La réflexion étendue est désactivée : sur des tâches courtes, elle consomme des jetons
# avant le texte et peut laisser une réponse vide si max_tokens est atteint.
SANS_REFLEXION = {"type": "disabled"}


@dataclass
class Usage:
    modele: str
    entree: int
    sortie: int

    def cout(self, prix: dict) -> float:
        pe, ps = prix.get(self.modele, (0.0, 0.0))
        return (self.entree * pe + self.sortie * ps) / 1_000_000


class Claude:
    def __init__(self, cle: str, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic(api_key=cle, max_retries=2, timeout=60)

    def texte(self, modele: str, systeme: str, message: str, max_tokens: int = 300) -> tuple[str, Usage]:
        r = self.client.messages.create(
            model=modele, max_tokens=max_tokens, system=systeme, thinking=SANS_REFLEXION,
            messages=[{"role": "user", "content": message}],
        )
        texte = "".join(b.text for b in r.content if b.type == "text")
        return texte, Usage(modele, r.usage.input_tokens, r.usage.output_tokens)

    def outil(self, modele: str, systeme: str, message: str, outil: dict, max_tokens: int = 600) -> tuple[dict | None, Usage]:
        """Sortie structurée : le modèle est obligé d'appeler l'outil, dont le schéma JSON fixe le format."""
        r = self.client.messages.create(
            model=modele, max_tokens=max_tokens, system=systeme, thinking=SANS_REFLEXION,
            tools=[outil], tool_choice={"type": "tool", "name": outil["name"]},
            messages=[{"role": "user", "content": message}],
        )
        donnees = next((b.input for b in r.content if b.type == "tool_use"), None)
        return donnees, Usage(modele, r.usage.input_tokens, r.usage.output_tokens)

    def flux(self, modele: str, systeme: str, messages: list[dict], max_tokens: int = 700) -> Iterator[str | Usage]:
        """Réponse au fil de l'eau : renvoie des morceaux de texte, puis l'usage final."""
        with self.client.messages.stream(
            model=modele, max_tokens=max_tokens, system=systeme, thinking=SANS_REFLEXION, messages=messages,
        ) as flux:
            for morceau in flux.text_stream:
                yield morceau
            final = flux.get_final_message()
        yield Usage(modele, final.usage.input_tokens, final.usage.output_tokens)

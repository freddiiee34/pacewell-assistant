"""Appels à l'API Claude, regroupés ici pour pouvoir les remplacer par un faux client dans les tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import anthropic

# La réflexion étendue est désactivée : sur des tâches courtes, elle consomme des jetons
# avant le texte et peut laisser une réponse vide si max_tokens est atteint.
# Selon le modèle, l'API attend {"type": "disabled"} ou {"type": "between_tools"} (le modèle ne réfléchit
# pas avant de répondre). On essaie le premier, et on retient le second pour ce modèle si l'API le demande.
SANS_REFLEXION = {"type": "disabled"}
ENTRE_OUTILS = {"type": "between_tools"}


def _demande_between_tools(erreur: Exception) -> bool:
    return isinstance(erreur, anthropic.BadRequestError) and "between_tools" in str(erreur)


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
        self._reflexion: dict[str, dict] = {}   # réglage accepté par chaque modèle

    def _appel(self, modele: str, fonction, **params):
        """Appelle l'API avec le bon réglage de réflexion pour ce modèle."""
        try:
            return fonction(model=modele, thinking=self._reflexion.get(modele, SANS_REFLEXION), **params)
        except anthropic.BadRequestError as e:
            if not _demande_between_tools(e) or self._reflexion.get(modele) == ENTRE_OUTILS:
                raise
            self._reflexion[modele] = ENTRE_OUTILS
            return fonction(model=modele, thinking=ENTRE_OUTILS, **params)

    def texte(self, modele: str, systeme: str, message: str, max_tokens: int = 300) -> tuple[str, Usage]:
        r = self._appel(modele, self.client.messages.create, max_tokens=max_tokens, system=systeme,
                        messages=[{"role": "user", "content": message}])
        texte = "".join(b.text for b in r.content if b.type == "text")
        return texte, Usage(modele, r.usage.input_tokens, r.usage.output_tokens)

    def outil(self, modele: str, systeme: str, message: str, outil: dict, max_tokens: int = 600) -> tuple[dict | None, Usage]:
        """Sortie structurée : le modèle est obligé d'appeler l'outil, dont le schéma JSON fixe le format."""
        r = self._appel(modele, self.client.messages.create, max_tokens=max_tokens, system=systeme,
                        tools=[outil], tool_choice={"type": "tool", "name": outil["name"]},
                        messages=[{"role": "user", "content": message}])
        donnees = next((b.input for b in r.content if b.type == "tool_use"), None)
        return donnees, Usage(modele, r.usage.input_tokens, r.usage.output_tokens)

    def flux(self, modele: str, systeme: str, messages: list[dict], max_tokens: int = 700) -> Iterator[str | Usage]:
        """Réponse au fil de l'eau : renvoie des morceaux de texte, puis l'usage final."""
        # L'erreur de réglage arrive à l'ouverture du flux, avant tout texte : on peut réessayer sans risque.
        gestionnaire, flux = self._appel(modele, self._ouvrir_flux, max_tokens=max_tokens, system=systeme, messages=messages)
        try:
            for morceau in flux.text_stream:
                yield morceau
            final = flux.get_final_message()
        finally:
            gestionnaire.__exit__(None, None, None)
        yield Usage(modele, final.usage.input_tokens, final.usage.output_tokens)

    def _ouvrir_flux(self, **params):
        gestionnaire = self.client.messages.stream(**params)
        return gestionnaire, gestionnaire.__enter__()  # la requête part ici : une erreur 400 remonte tout de suite

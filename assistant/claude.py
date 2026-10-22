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
    cache_ecriture: int = 0   # jetons mis en cache (facturés 1,25 fois le prix d'entrée)
    cache_lecture: int = 0    # jetons relus depuis le cache (facturés 0,1 fois le prix d'entrée)

    def cout(self, prix: dict) -> float:
        pe, ps = prix.get(self.modele, (0.0, 0.0))
        return (self.entree * pe + self.cache_ecriture * pe * 1.25 + self.cache_lecture * pe * 0.1 + self.sortie * ps) / 1_000_000

    @classmethod
    def depuis(cls, modele: str, u) -> "Usage":
        return cls(modele, u.input_tokens, u.output_tokens,
                   getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0)


def bloc_pour_api(b) -> dict | None:
    """Convertit un bloc de réponse en bloc à renvoyer à l'API au tour suivant (les autres champs sont ignorés)."""
    d = b if isinstance(b, dict) else b.model_dump()
    t = d.get("type")
    if t == "text":
        return {"type": "text", "text": d["text"]}
    if t == "tool_use":
        return {"type": "tool_use", "id": d["id"], "name": d["name"], "input": d["input"]}
    if t == "thinking":
        return {"type": "thinking", "thinking": d["thinking"], "signature": d["signature"]}
    if t == "redacted_thinking":
        return {"type": "redacted_thinking", "data": d["data"]}
    return None


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
        return texte, Usage.depuis(modele, r.usage)

    def outil(self, modele: str, systeme: str, message: str, outil: dict, max_tokens: int = 600) -> tuple[dict | None, Usage]:
        """Sortie structurée : le modèle est obligé d'appeler l'outil, dont le schéma JSON fixe le format."""
        r = self._appel(modele, self.client.messages.create, max_tokens=max_tokens, system=systeme,
                        tools=[outil], tool_choice={"type": "tool", "name": outil["name"]},
                        messages=[{"role": "user", "content": message}])
        donnees = next((b.input for b in r.content if b.type == "tool_use"), None)
        return donnees, Usage.depuis(modele, r.usage)

    def image_outil(self, modele: str, systeme: str, image_b64: str, media: str, consigne: str, outil: dict,
                    max_tokens: int = 800) -> tuple[dict | None, Usage]:
        """Vision + sortie structurée : le modèle lit une image et remplit le schéma de l'outil."""
        contenu = [{"type": "image", "source": {"type": "base64", "media_type": media, "data": image_b64}},
                   {"type": "text", "text": consigne}]
        r = self._appel(modele, self.client.messages.create, max_tokens=max_tokens, system=systeme,
                        tools=[outil], tool_choice={"type": "tool", "name": outil["name"]},
                        messages=[{"role": "user", "content": contenu}])
        donnees = next((b.input for b in r.content if b.type == "tool_use"), None)
        return donnees, Usage.depuis(modele, r.usage)

    def flux_outils(self, modele: str, systeme: str, messages: list[dict], outils: list[dict], max_tokens: int = 900):
        """Un tour d'agent en flux : renvoie ("texte", morceau)… puis ("fin", blocs, raison_d_arret, usage).

        Le prompt système et les outils sont mis en cache (cache_control) : d'un tour à l'autre et d'une question
        à l'autre, ces ~2 000 jetons sont relus à 10 % du prix au lieu d'être refacturés.
        """
        systeme_blocs = [{"type": "text", "text": systeme, "cache_control": {"type": "ephemeral"}}]
        gestionnaire, flux = self._appel(modele, self._ouvrir_flux, max_tokens=max_tokens, system=systeme_blocs,
                                         tools=outils, messages=messages)
        try:
            for morceau in flux.text_stream:
                yield ("texte", morceau)
            final = flux.get_final_message()
        finally:
            gestionnaire.__exit__(None, None, None)
        yield ("fin", [b for b in (bloc_pour_api(x) for x in final.content) if b], final.stop_reason, Usage.depuis(modele, final.usage))

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
        yield Usage.depuis(modele, final.usage)

    def _ouvrir_flux(self, **params):
        gestionnaire = self.client.messages.stream(**params)
        return gestionnaire, gestionnaire.__enter__()  # la requête part ici : une erreur 400 remonte tout de suite

"""Découpage des documents en passages (« chunks »).

Règles :
- un passage ne mélange jamais deux sections : on coupe d'abord sur les titres (## et ###) ;
- une section trop longue est coupée entre deux paragraphes, avec un paragraphe de recouvrement ;
- un tableau ou une liste n'est jamais coupé en deux ;
- chaque passage garde le chemin de ses titres (« Retours et échanges > Délai de retour »),
  ajouté devant le texte vectorisé : un paragraphe seul n'a souvent pas assez de contexte.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

MOTS_MAX = 180


@dataclass(frozen=True)
class Document:
    source: str   # chemin relatif, par exemple « retours-echanges.md »
    titre: str
    url: str
    texte: str

    @property
    def empreinte(self) -> str:
        return hashlib.sha256(self.texte.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Passage:
    source: str
    titre_document: str
    url: str
    section: str      # chemin des titres
    ancre: str        # ancre de la section, pour un lien direct
    ordre: int
    texte: str
    questions: tuple[str, ...] = ()   # questions hypothétiques de clients (ajoutées à l'ingestion)

    @property
    def texte_a_vectoriser(self) -> str:
        base = f"{self.titre_document} > {self.section}\n{self.texte}"
        return base + ("\nQuestions : " + " ".join(self.questions) if self.questions else "")


def lire_document(chemin: Path, racine: Path) -> Document:
    brut = chemin.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    m = re.match(r"^---\n(.*?)\n---\n", brut, re.S)
    if m:
        for ligne in m.group(1).splitlines():
            if ":" in ligne:
                k, v = ligne.split(":", 1)
                meta[k.strip()] = v.strip()
        brut = brut[m.end():]
    return Document(
        source=str(chemin.relative_to(racine)),
        titre=meta.get("titre", chemin.stem),
        url=meta.get("url", "/"),
        texte=brut.strip(),
    )


def lire_corpus(dossier: Path) -> list[Document]:
    return [lire_document(p, dossier) for p in sorted(dossier.rglob("*.md"))]


def ancre(titre: str) -> str:
    t = titre.lower()
    t = re.sub(r"[àâä]", "a", t); t = re.sub(r"[éèêë]", "e", t); t = re.sub(r"[îï]", "i", t)
    t = re.sub(r"[ôö]", "o", t); t = re.sub(r"[ùûü]", "u", t); t = t.replace("ç", "c")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _blocs(texte: str) -> list[str]:
    """Paragraphes, en gardant ensemble les lignes d'un tableau ou d'une liste."""
    return [b.strip() for b in re.split(r"\n\s*\n", texte) if b.strip()]


def _mots(t: str) -> int:
    return len(t.split())


def _regrouper(blocs: list[str]) -> list[str]:
    """Assemble les blocs en passages d'au plus MOTS_MAX mots, avec un bloc de recouvrement."""
    passages: list[str] = []
    courant: list[str] = []
    for bloc in blocs:
        if courant and _mots("\n\n".join(courant + [bloc])) > MOTS_MAX:
            passages.append("\n\n".join(courant))
            # recouvrement : on reprend le dernier bloc s'il est court, pour ne pas perdre le fil
            courant = [courant[-1]] if _mots(courant[-1]) <= 60 else []
        courant.append(bloc)
    if courant:
        passages.append("\n\n".join(courant))
    return passages


def decouper(doc: Document) -> list[Passage]:
    sections: list[tuple[list[str], str]] = []   # (chemin de titres, texte)
    chemin: list[str] = []
    tampon: list[str] = []

    def vider():
        corps = "\n".join(tampon).strip()
        if corps:
            sections.append((list(chemin), corps))
        tampon.clear()

    for ligne in doc.texte.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)$", ligne)
        if m:
            vider()
            niveau = len(m.group(1))
            titre = m.group(2).strip()
            if niveau == 1:
                chemin = []
            else:
                chemin = chemin[: niveau - 2] + [titre]
            continue
        tampon.append(ligne)
    vider()

    passages: list[Passage] = []
    for chemin_titres, corps in sections:
        section = " > ".join(chemin_titres) or "Introduction"
        for morceau in _regrouper(_blocs(corps)):
            passages.append(Passage(
                source=doc.source, titre_document=doc.titre, url=doc.url, section=section,
                ancre=ancre(chemin_titres[-1]) if chemin_titres else "",
                ordre=len(passages), texte=morceau,
            ))
    return passages

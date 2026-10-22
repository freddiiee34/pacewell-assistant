"""Outils de l'agent : leur description (schéma JSON lu par le modèle) et leur exécution.

Le modèle choisit seul l'outil à appeler et ses arguments. Le code vérifie ensuite tout ce qui compte :
couple numéro + email, délais de retour, articles repris ou non, confirmation avant d'écrire.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from . import boutique
from .recherche import Candidat, Chercheur

DEFINITIONS = [
    {
        "name": "chercher_documentation",
        "description": "Cherche dans la documentation de Pacewell (aide, conditions de vente, guide des tailles, catalogue, conseils) "
                       "les passages utiles à une question. À utiliser pour toute question sur les règles, les produits, les prix, "
                       "les délais ou les conseils. Les passages renvoyés sont numérotés : citer ces numéros entre crochets.",
        "input_schema": {
            "type": "object",
            "properties": {"requete": {"type": "string", "description": "La question reformulée pour la recherche, en français, autonome."}},
            "required": ["requete"],
        },
    },
    {
        "name": "consulter_commande",
        "description": "Renvoie le statut, les articles, la livraison et la date limite de retour d'une commande. Exige le numéro "
                       "de commande ET l'adresse email utilisée pour commander : les demander au client s'il ne les a pas donnés. "
                       "Ne jamais deviner ni compléter un numéro ou un email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string", "description": "Numéro de commande, PW suivi de 6 chiffres."},
                "email": {"type": "string", "description": "Adresse email donnée par le client."},
            },
            "required": ["numero", "email"],
        },
    },
    {
        "name": "ouvrir_retour",
        "description": "Ouvre un retour (remboursement ou échange) sur une commande livrée. Appeler d'abord avec confirme=false : "
                       "l'outil vérifie les conditions et renvoie un récapitulatif à faire valider par le client. N'appeler avec "
                       "confirme=true qu'après un accord explicite du client dans son dernier message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string"},
                "email": {"type": "string"},
                "articles": {"type": "array", "items": {"type": "string"}, "description": "Noms des articles à retourner, tels que dans la commande."},
                "motif": {"type": "string", "description": "Motif donné par le client (taille, confort, défaut, changement d'avis…)."},
                "solution": {"type": "string", "enum": ["remboursement", "echange"]},
                "precision": {"type": "string", "description": "Pour un échange : la taille ou le coloris souhaité."},
                "confirme": {"type": "boolean"},
            },
            "required": ["numero", "email", "articles", "motif", "solution", "confirme"],
        },
    },
    {
        "name": "transmettre_a_un_humain",
        "description": "Transmet la demande au service client humain, qui répondra par email sous 24 heures ouvrées. À utiliser si le "
                       "client le demande, s'il signale un défaut, un litige, un colis perdu ou abîmé, ou si la demande dépasse ce que "
                       "les autres outils permettent. Exige l'email du client.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "resume": {"type": "string", "description": "Résumé factuel de la demande en 2 à 4 phrases, pour le conseiller."},
                "numero": {"type": "string", "description": "Numéro de commande s'il est connu."},
            },
            "required": ["email", "resume"],
        },
    },
]


@dataclass
class Contexte:
    """État partagé entre les appels d'outils d'une même réponse."""
    base: object
    chercheur: Chercheur
    url_escalade: str = ""
    passages: list[Candidat] = field(default_factory=list)   # numérotés dans l'ordre : [1], [2]…
    traces: list[dict] = field(default_factory=list)         # pour les coulisses et le journal
    usages: list = field(default_factory=list)


def _resume_entree(nom: str, entree: dict) -> dict:
    """Arguments affichés dans les coulisses : l'email est masqué."""
    e = dict(entree)
    if "email" in e:
        e["email"] = boutique.masquer_email(e["email"])
    return e


def executer(ctx: Contexte, nom: str, entree: dict) -> tuple[str, dict]:
    """Exécute un outil. Renvoie (texte pour le modèle, trace pour les coulisses)."""
    debut = time.perf_counter()
    trace = {"outil": nom, "entree": _resume_entree(nom, entree)}
    try:
        if nom == "chercher_documentation":
            r = ctx.chercheur.chercher(str(entree.get("requete", ""))[:400])
            ctx.usages.extend(r.usages)
            premier = len(ctx.passages) + 1
            ctx.passages.extend(r.retenus)
            if not r.retenus:
                texte = "Aucun passage pertinent dans la documentation."
            else:
                texte = "\n\n".join(
                    f'<extrait numero="{premier + i}" source="{p.titre_document} > {p.section}">\n{p.texte}\n</extrait>'
                    for i, p in enumerate(r.retenus))
            trace.update(resultat=f"{len(r.retenus)} passage(s) retenu(s)", premier_numero=premier,
                         retenus=[p.public() for p in r.retenus],
                         ecartes=[p.public(140) for p in r.candidats if p not in r.retenus],
                         durees_ms=r.durees_ms)
        elif nom == "consulter_commande":
            res = boutique.consulter_commande(ctx.base, entree.get("numero", ""), entree.get("email", ""))
            texte = json.dumps(res, ensure_ascii=False)
            trace["resultat"] = (f"commande {res['numero']} {res['statut']}" if res.get("trouvee") else "introuvable (numéro ou email)")
        elif nom == "ouvrir_retour":
            res = boutique.ouvrir_retour(ctx.base, entree.get("numero", ""), entree.get("email", ""), entree.get("articles") or [],
                                         entree.get("motif", ""), entree.get("solution", ""), entree.get("precision", ""),
                                         bool(entree.get("confirme")))
            texte = json.dumps(res, ensure_ascii=False)
            if res.get("trouvee") is False:
                trace["resultat"] = "introuvable (numéro ou email)"
            elif not res.get("possible"):
                trace["resultat"] = "refusé : " + res.get("raison", "")
            else:
                trace["resultat"] = f"retour {res['retour']} ouvert" if res.get("confirme") else "récapitulatif à faire confirmer"
        elif nom == "transmettre_a_un_humain":
            email = boutique.normaliser_email(entree.get("email", ""))
            if "@" not in email:
                texte, trace["resultat"] = json.dumps({"transmis": False, "raison": "Adresse email manquante ou invalide."}), "refusé : email manquant"
            else:
                ident = boutique.enregistrer_escalade(ctx.base, email, entree.get("resume", ""), entree.get("numero"))
                envoye = _prevenir_humain(ctx.url_escalade, email, entree.get("resume", ""), entree.get("numero"), ident)
                texte = json.dumps({"transmis": True, "reference": f"SAV-{ident}", "delai": "réponse par email sous 24 heures ouvrées"},
                                   ensure_ascii=False)
                trace["resultat"] = f"demande SAV-{ident} transmise" + ("" if envoye else " (enregistrée, notification non envoyée)")
        else:
            texte, trace["resultat"] = f"Outil inconnu : {nom}", "outil inconnu"
    except Exception as e:  # une panne d'outil ne doit pas casser la conversation
        print(f"[assistant] outil {nom} en erreur : {e!r}", flush=True)
        texte, trace["resultat"] = "L'outil a rencontré une erreur. Proposer au client de contacter le service client.", "erreur"
    trace["duree_ms"] = round((time.perf_counter() - debut) * 1000)
    ctx.traces.append(trace)
    return texte, trace


def _prevenir_humain(url: str, email: str, resume: str, numero: str | None, ident: int) -> bool:
    """Prévient le service client via le workflow n8n « Assistant · Escalade » (email au conseiller + fiche Brevo)."""
    if not url:
        return False
    try:
        r = httpx.post(url, json={"reference": f"SAV-{ident}", "email": email, "resume": resume[:2000], "numero": numero or ""}, timeout=8)
        return r.status_code < 300
    except httpx.HTTPError as e:
        print(f"[assistant] escalade non notifiée : {e!r}", flush=True)
        return False

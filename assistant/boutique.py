"""Données de la boutique fictive : commandes, retours et demandes transmises à un humain.

Les commandes de démonstration (donnees/commandes.json) sont rechargées à chaque démarrage, avec des dates
relatives au jour même : une commande « livrée il y a 11 jours » le reste, et la démonstration ne vieillit pas.

Règles de sécurité appliquées ici, et pas seulement dans le prompt :
- une commande n'est jamais renvoyée sans le bon couple numéro + email ;
- le message d'échec est le même que le numéro existe ou non (on ne révèle rien) ;
- un retour n'est enregistré qu'avec confirme=True, et seulement s'il respecte les conditions.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, timedelta
from pathlib import Path

from .config import RACINE

FICHIER = RACINE / "donnees" / "commandes.json"
DELAI_RETOUR_JOURS = 30
NON_REPRIS = {"nutrition": "les produits de nutrition ne sont pas repris une fois ouverts",
              "chaussettes": "les chaussettes ne sont pas reprises si l'emballage a été ouvert"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS commandes (
  numero text PRIMARY KEY,
  email text NOT NULL,
  prenom text NOT NULL,
  date_commande date NOT NULL,
  date_livraison date,
  statut text NOT NULL,
  mode_livraison text NOT NULL,
  transporteur text,
  suivi text,
  articles jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS retours (
  id text PRIMARY KEY,
  numero_commande text NOT NULL REFERENCES commandes(numero) ON DELETE CASCADE,
  articles jsonb NOT NULL,
  motif text NOT NULL,
  solution text NOT NULL,
  precision text NOT NULL DEFAULT '',
  etiquette text NOT NULL,
  cree_le timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS escalades (
  id bigserial PRIMARY KEY,
  email text NOT NULL,
  resume text NOT NULL,
  numero_commande text,
  cree_le timestamptz NOT NULL DEFAULT now()
);
"""

STATUTS = {"en_preparation": "en préparation", "expediee": "expédiée", "livree": "livrée", "annulee": "annulée"}


def normaliser_numero(n: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(n or "").upper())


def normaliser_email(e: str) -> str:
    return str(e or "").strip().lower()


def masquer_email(e: str) -> str:
    """Pour le journal et les coulisses : c***@exemple.fr."""
    e = normaliser_email(e)
    if "@" not in e:
        return "***"
    nom, dom = e.split("@", 1)
    return f"{nom[:1]}***@{dom}"


def charger_commandes(base, aujourd_hui: date | None = None) -> int:
    aujourd_hui = aujourd_hui or date.today()
    commandes = json.loads(FICHIER.read_text(encoding="utf-8"))
    with base.connexion() as cx, cx.transaction():
        cx.execute(SCHEMA)
        for c in commandes:
            livraison = None if c["livree_il_y_a_jours"] is None else aujourd_hui - timedelta(days=c["livree_il_y_a_jours"])
            cx.execute(
                """INSERT INTO commandes (numero, email, prenom, date_commande, date_livraison, statut, mode_livraison, transporteur, suivi, articles)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (numero) DO UPDATE SET email = EXCLUDED.email, prenom = EXCLUDED.prenom,
                     date_commande = EXCLUDED.date_commande, date_livraison = EXCLUDED.date_livraison, statut = EXCLUDED.statut,
                     mode_livraison = EXCLUDED.mode_livraison, transporteur = EXCLUDED.transporteur, suivi = EXCLUDED.suivi,
                     articles = EXCLUDED.articles""",
                (c["numero"], c["email"], c["prenom"], aujourd_hui - timedelta(days=c["commandee_il_y_a_jours"]), livraison,
                 c["statut"], c["mode_livraison"], c["transporteur"], c["suivi"], json.dumps(c["articles"], ensure_ascii=False)),
            )
        # Les retours de démonstration s'effacent au bout de 2 jours : chaque visiteur peut refaire le parcours.
        cx.execute("DELETE FROM retours WHERE cree_le < now() - interval '2 days'")
    return len(commandes)


def _commande(base, numero: str, email: str) -> dict | None:
    with base.connexion() as cx:
        l = cx.execute(
            "SELECT numero, email, prenom, date_commande, date_livraison, statut, mode_livraison, transporteur, suivi, articles "
            "FROM commandes WHERE numero = %s AND email = %s", (normaliser_numero(numero), normaliser_email(email))
        ).fetchone()
        if not l:
            return None
        retours = cx.execute("SELECT id, articles, solution, cree_le::date FROM retours WHERE numero_commande = %s ORDER BY cree_le",
                             (l[0],)).fetchall()
    cles = ["numero", "email", "prenom", "date_commande", "date_livraison", "statut", "mode_livraison", "transporteur", "suivi", "articles"]
    c = dict(zip(cles, l))
    c["retours"] = [{"id": r[0], "articles": r[1], "solution": r[2], "le": r[3].isoformat()} for r in retours]
    return c


INTROUVABLE = {"trouvee": False,
               "message": "Aucune commande ne correspond à ce numéro et à cette adresse email. Vérifier les deux informations "
                          "(le numéro commence par PW suivi de 6 chiffres, l'email est celui utilisé pour commander)."}


def consulter_commande(base, numero: str, email: str, aujourd_hui: date | None = None) -> dict:
    aujourd_hui = aujourd_hui or date.today()
    c = _commande(base, numero, email)
    if not c:
        return INTROUVABLE
    limite = c["date_livraison"] + timedelta(days=DELAI_RETOUR_JOURS) if c["date_livraison"] else None
    return {
        "trouvee": True,
        "numero": c["numero"],
        "prenom": c["prenom"],
        "statut": STATUTS.get(c["statut"], c["statut"]),
        "commandee_le": c["date_commande"].isoformat(),
        "livree_le": c["date_livraison"].isoformat() if c["date_livraison"] else None,
        "mode_livraison": c["mode_livraison"],
        "transporteur": c["transporteur"],
        "numero_suivi": c["suivi"],
        "articles": [{"nom": a["nom"], "variante": a["variante"], "prix": a["prix"], "quantite": a["quantite"]} for a in c["articles"]],
        "total": round(sum(a["prix"] * a["quantite"] for a in c["articles"]), 2),
        "retour_possible_jusqu_au": limite.isoformat() if limite else None,
        "retour_encore_possible": bool(limite and aujourd_hui <= limite),
        "retours_ouverts": c["retours"],
    }


def _trouver_article(articles: list[dict], nom: str) -> dict | None:
    n = nom.lower()
    for a in articles:
        if n in a["nom"].lower() or a["nom"].lower() in n or n == a["sku"].lower():
            return a
    return None


def ouvrir_retour(base, numero: str, email: str, articles: list[str], motif: str, solution: str,
                  precision: str = "", confirme: bool = False, aujourd_hui: date | None = None) -> dict:
    aujourd_hui = aujourd_hui or date.today()
    c = _commande(base, numero, email)
    if not c:
        return INTROUVABLE
    if c["statut"] != "livree" or not c["date_livraison"]:
        return {"possible": False, "raison": f"La commande est {STATUTS.get(c['statut'], c['statut'])} : un retour s'ouvre une fois le colis reçu."}
    limite = c["date_livraison"] + timedelta(days=DELAI_RETOUR_JOURS)
    if aujourd_hui > limite:
        return {"possible": False, "raison": f"Le délai de retour de {DELAI_RETOUR_JOURS} jours est dépassé depuis le {limite.isoformat()}. "
                                             "Pour un défaut, la garantie de 2 ans reste possible : passer par le service client."}
    if solution not in ("remboursement", "echange"):
        return {"possible": False, "raison": "Choisir entre un remboursement et un échange."}
    deja = {a for r in c["retours"] for a in r["articles"]}
    choisis, refus = [], []
    for nom in articles or []:
        a = _trouver_article(c["articles"], nom)
        if not a:
            refus.append(f"« {nom} » ne fait pas partie de cette commande")
        elif a["nom"] in deja:
            refus.append(f"{a['nom']} a déjà un retour ouvert")
        elif a["categorie"] in NON_REPRIS:
            refus.append(f"{a['nom']} : {NON_REPRIS[a['categorie']]}")
        else:
            choisis.append(a["nom"])
    if not choisis:
        return {"possible": False, "raison": " ; ".join(refus) or "Préciser le ou les articles à retourner."}
    recap = {"commande": c["numero"], "articles": choisis, "solution": solution, "motif": motif, "precision": precision,
             "frais": "gratuit en point relais en France métropolitaine, ou au magasin de Lille",
             "a_deposer_avant": (aujourd_hui + timedelta(days=7)).isoformat(), "articles_refuses": refus}
    if not confirme:
        return {"possible": True, "confirme": False, "a_confirmer": recap,
                "consigne": "Présenter ce récapitulatif au client et lui demander de confirmer avant d'ouvrir le retour."}
    rid = f"R-{c['numero'][2:]}-{secrets.token_hex(2).upper()}"
    etiquette = f"MR-RET-{secrets.token_hex(4).upper()}"
    with base.connexion() as cx:
        cx.execute("INSERT INTO retours (id, numero_commande, articles, motif, solution, precision, etiquette) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                   (rid, c["numero"], json.dumps(choisis, ensure_ascii=False), motif, solution, precision, etiquette))
    return {"possible": True, "confirme": True, "retour": rid, "code_etiquette": etiquette, **recap,
            "suite": "Le client reçoit l'étiquette prépayée par email (démonstration : aucun email réel). "
                     + ("Le remboursement est fait 5 jours ouvrés après réception du colis." if solution == "remboursement"
                        else "Le nouvel article part dès que le colis est scanné par le transporteur, s'il est en stock.")}


def enregistrer_escalade(base, email: str, resume: str, numero: str | None) -> int:
    with base.connexion() as cx:
        return cx.execute("INSERT INTO escalades (email, resume, numero_commande) VALUES (%s, %s, %s) RETURNING id",
                          (normaliser_email(email), resume[:2000], normaliser_numero(numero) if numero else None)).fetchone()[0]

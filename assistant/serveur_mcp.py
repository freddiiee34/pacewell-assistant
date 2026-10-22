"""Serveur MCP (Model Context Protocol) : les outils de l'assistant, utilisables depuis n'importe quel client MCP
(Claude Desktop, Claude Code…) à l'adresse https://assistant.simondelrue.fr/mcp.

Le client MCP joue alors le rôle de l'agent : c'est lui qui décide d'appeler ces outils. Le code des outils est
le même que pour l'assistant web (assistant/outils.py), avec les mêmes vérifications.
"""
from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import boutique
from .outils import Contexte, executer

INSTRUCTIONS = (
    "Outils du service client de Pacewell, boutique fictive de running et trail créée pour une démonstration. "
    "Commande de démonstration : PW104233 avec l'email camille.martin@exemple.fr. "
    "Citer les numéros d'extraits renvoyés par chercher_documentation."
)


def creer_serveur_mcp(etat: dict, cfg) -> MCPServer:
    mcp = MCPServer(name="Pacewell", title="Service client Pacewell", instructions=INSTRUCTIONS, version="1.0")

    def _ctx() -> Contexte:
        if etat["base"].cout_du_jour() >= cfg.budget_jour_usd:
            raise RuntimeError("Budget quotidien de la démonstration atteint, réessayer demain.")
        return Contexte(etat["base"], etat["chercheur"], cfg.url_escalade)

    def _journal(ctx: Contexte, question: str):
        try:
            cout = sum(u.cout(cfg.prix) for u in ctx.usages)
            etat["base"].journaliser(
                question=question, question_recherche="", passages=[{"id": p.id, "section": p.section, "note": p.note} for p in ctx.passages],
                reponse="", statut="ok", modele="mcp", jetons={"entree": sum(u.entree for u in ctx.usages), "sortie": sum(u.sortie for u in ctx.usages)},
                cout_usd=cout, durees_ms={"outils": sum(t["duree_ms"] for t in ctx.traces), "total": sum(t["duree_ms"] for t in ctx.traces)},
                outils=[{"outil": t["outil"], "resultat": t.get("resultat", ""), "duree_ms": t["duree_ms"]} for t in ctx.traces], canal="mcp")
        except Exception as e:
            print(f"[assistant] journal MCP non écrit : {e!r}", flush=True)

    @mcp.tool(description="Cherche dans la documentation de Pacewell (livraison, retours, garantie, paiement, fidélité, tailles, "
                          "catalogue, conseils) et renvoie les passages pertinents, numérotés, avec leur source.")
    def chercher_documentation(requete: str) -> str:
        ctx = _ctx()
        texte, _ = executer(ctx, "chercher_documentation", {"requete": requete})
        _journal(ctx, f"[mcp] {requete}")
        return texte

    @mcp.tool(description="Statut, articles, livraison et date limite de retour d'une commande Pacewell. "
                          "Exige le numéro (PW + 6 chiffres) et l'email utilisé pour commander.")
    def consulter_commande(numero: str, email: str) -> str:
        ctx = _ctx()
        texte, _ = executer(ctx, "consulter_commande", {"numero": numero, "email": email})
        _journal(ctx, "[mcp] consulter_commande")
        return texte

    @mcp.tool(description="Ouvre un retour sur une commande livrée. Appeler d'abord avec confirme=false pour obtenir le récapitulatif, "
                          "puis avec confirme=true après accord de l'utilisateur. solution : remboursement ou echange.")
    def ouvrir_retour(numero: str, email: str, articles: list[str], motif: str, solution: str,
                      precision: str = "", confirme: bool = False) -> str:
        ctx = _ctx()
        texte, _ = executer(ctx, "ouvrir_retour", {"numero": numero, "email": email, "articles": articles, "motif": motif,
                                                   "solution": solution, "precision": precision, "confirme": confirme})
        _journal(ctx, "[mcp] ouvrir_retour")
        return texte

    return mcp


def application_mcp(mcp: MCPServer):
    # Le contrôle d'hôte (protection contre le « DNS rebinding ») vise les serveurs MCP locaux ; celui-ci est public
    # derrière un proxy HTTPS, avec un nom de domaine fixe.
    securite = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                   transport_security=securite, host="0.0.0.0")


__all__ = ["creer_serveur_mcp", "application_mcp", "boutique", "json"]

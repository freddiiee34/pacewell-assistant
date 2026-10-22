"""Serveur web : page de démonstration, API de l'assistant (réponse en flux, Server-Sent Events),
serveur MCP et statistiques d'usage pour le tableau de bord."""
from __future__ import annotations

import json
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import boutique
from .agent import Agent
from .base import Base
from .claude import Claude
from .config import RACINE, config
from .documents import valider_image
from .embeddings import creer
from .garde import Limiteur
from .ingerer import ingerer
from .recherche import Chercheur
from .serveur_mcp import application_mcp, creer_serveur_mcp


class Message(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    texte: str = Field(max_length=4000)


class Image(BaseModel):
    donnees: str = Field(max_length=7_500_000)   # base64 d'une image de 5 Mo au plus
    type: str = Field(max_length=40)


class Demande(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    historique: list[Message] = Field(default_factory=list, max_length=16)
    image: Image | None = None


def creer_app(cfg=None, vect=None, claude=None) -> FastAPI:
    cfg = cfg or config()
    etat: dict = {}
    mcp = creer_serveur_mcp(etat, cfg)
    app_mcp = application_mcp(mcp)

    @asynccontextmanager
    async def cycle(app: FastAPI):
        v = vect or creer(cfg)
        base = Base(cfg.database_url, v.dimension)
        base.initialiser()
        print("[assistant] commandes de démonstration :", boutique.charger_commandes(base), flush=True)
        c = claude or (Claude(cfg.anthropic_api_key) if cfg.anthropic_api_key else None)
        print("[assistant] ingestion :", ingerer(cfg, base, v, claude=c), flush=True)
        base.purger_journal()
        chercheur = Chercheur(cfg, base, v, c)
        etat.update(base=base, vect=v, claude=c, chercheur=chercheur,
                    agent=Agent(cfg, base, chercheur, c, cfg.url_escalade) if c else None,
                    limiteur=Limiteur(cfg.questions_par_fenetre, cfg.fenetre_secondes))
        async with AsyncExitStack() as pile:
            await pile.enter_async_context(mcp.session_manager.run())
            yield

    app = FastAPI(title="Assistant Pacewell", lifespan=cycle, docs_url=None, redoc_url=None)

    def _ip(requete: Request) -> str:
        return (requete.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (requete.client.host if requete.client else "inconnu"))

    @app.get("/api/sante")
    def sante():
        return {"ok": True, "passages": etat["base"].nombre_passages(), "embeddings": etat["vect"].nom,
                "modele": cfg.modele_reponse, "ia": etat["agent"] is not None}

    @app.post("/api/question")
    def question(demande: Demande, requete: Request):
        q = demande.question.strip()
        if len(q) > cfg.longueur_max_question:
            return JSONResponse({"erreur": f"La question dépasse {cfg.longueur_max_question} caractères."}, 400)
        if demande.image and (msg := valider_image(demande.image.donnees, demande.image.type)):
            return JSONResponse({"erreur": msg}, 400)
        if etat["agent"] is None:
            return JSONResponse({"erreur": "L'assistant n'est pas configuré (clé API manquante)."}, 503)
        ok, attente = etat["limiteur"].autoriser(_ip(requete))
        if not ok:
            return JSONResponse({"erreur": f"Beaucoup de questions d'un coup. Réessayez dans {max(1, attente // 60)} min."}, 429)
        if etat["base"].cout_du_jour() >= cfg.budget_jour_usd:
            return JSONResponse({"erreur": "La démonstration a atteint son budget du jour. Elle revient demain."}, 429)

        historique = [m.model_dump() for m in demande.historique]
        image = demande.image.model_dump() if demande.image else None

        def flux():
            for evenement in etat["agent"].repondre(q, historique, image):
                yield f"data: {json.dumps(evenement, ensure_ascii=False)}\n\n"

        return StreamingResponse(flux(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/statistiques")
    def statistiques():
        """Agrégats seulement : aucune question ni réponse n'est exposée."""
        return etat["base"].statistiques()

    @app.get("/api/evaluation")
    def evaluation():
        return etat["base"].derniere_evaluation()

    # Le serveur MCP répond sur /mcp ; ses routes passent avant les fichiers statiques.
    app.router.routes.extend(app_mcp.routes)
    app.mount("/", StaticFiles(directory=RACINE / "web", html=True), name="web")
    return app


app = creer_app()

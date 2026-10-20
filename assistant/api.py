"""Serveur web : page de démonstration et API de l'assistant (réponse en flux, Server-Sent Events)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .base import Base
from .claude import Claude
from .config import RACINE, config
from .embeddings import creer
from .garde import Limiteur
from .ingerer import ingerer
from .recherche import Chercheur
from .reponse import Assistant


class Message(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    texte: str = Field(max_length=4000)


class Demande(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    historique: list[Message] = Field(default_factory=list, max_length=12)


def creer_app(cfg=None, vect=None, claude=None) -> FastAPI:
    cfg = cfg or config()
    etat: dict = {}

    @asynccontextmanager
    async def cycle(app: FastAPI):
        v = vect or creer(cfg)
        base = Base(cfg.database_url, v.dimension)
        base.initialiser()
        print("[assistant] ingestion :", ingerer(cfg, base, v), flush=True)
        base.purger_journal()
        c = claude or (Claude(cfg.anthropic_api_key) if cfg.anthropic_api_key else None)
        etat.update(base=base, vect=v, claude=c,
                    assistant=Assistant(cfg, base, Chercheur(cfg, base, v, c), c) if c else None,
                    limiteur=Limiteur(cfg.questions_par_fenetre, cfg.fenetre_secondes))
        yield

    app = FastAPI(title="Assistant Pacewell", lifespan=cycle, docs_url=None, redoc_url=None)

    @app.get("/api/sante")
    def sante():
        return {"ok": True, "passages": etat["base"].nombre_passages(), "embeddings": etat["vect"].nom,
                "modele": cfg.modele_reponse, "ia": etat["assistant"] is not None}

    @app.post("/api/question")
    def question(demande: Demande, requete: Request):
        q = demande.question.strip()
        if len(q) > cfg.longueur_max_question:
            return JSONResponse({"erreur": f"La question dépasse {cfg.longueur_max_question} caractères."}, 400)
        if etat["assistant"] is None:
            return JSONResponse({"erreur": "L'assistant n'est pas configuré (clé API manquante)."}, 503)
        ip = (requete.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (requete.client.host if requete.client else "inconnu"))
        ok, attente = etat["limiteur"].autoriser(ip)
        if not ok:
            return JSONResponse({"erreur": f"Beaucoup de questions d'un coup. Réessayez dans {max(1, attente // 60)} min."}, 429)
        if etat["base"].cout_du_jour() >= cfg.budget_jour_usd:
            return JSONResponse({"erreur": "La démonstration a atteint son budget du jour. Elle revient demain."}, 429)

        historique = [m.model_dump() for m in demande.historique]

        def flux():
            for evenement in etat["assistant"].repondre(q, historique):
                yield f"data: {json.dumps(evenement, ensure_ascii=False)}\n\n"

        return StreamingResponse(flux(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.mount("/", StaticFiles(directory=RACINE / "web", html=True), name="web")
    return app


app = creer_app()

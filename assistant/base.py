"""Stockage des passages dans Postgres : texte, vecteur (pgvector) et index plein texte."""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from pgvector.psycopg import register_vector

from .decoupage import Document, Passage
from .mots import Bm25


@dataclass
class Trouve:
    id: int
    source: str
    titre_document: str
    url: str
    section: str
    ancre: str
    texte: str
    rang: int          # position dans cette méthode de recherche (1 = meilleur)
    score: float       # similarité cosinus (vecteurs) ou score BM25 (mots)


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS documents (
  source text PRIMARY KEY,
  titre text NOT NULL,
  url text NOT NULL,
  empreinte text NOT NULL,
  modele_embeddings text NOT NULL,
  maj_le timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS passages (
  id bigserial PRIMARY KEY,
  source text NOT NULL REFERENCES documents(source) ON DELETE CASCADE,
  titre_document text NOT NULL,
  url text NOT NULL,
  section text NOT NULL,
  ancre text NOT NULL,
  ordre int NOT NULL,
  texte text NOT NULL,
  questions text NOT NULL DEFAULT '',
  vecteur vector({dim}) NOT NULL
);
CREATE INDEX IF NOT EXISTS passages_vecteur ON passages USING hnsw (vecteur vector_cosine_ops);
CREATE TABLE IF NOT EXISTS echanges (
  id bigserial PRIMARY KEY,
  cree_le timestamptz NOT NULL DEFAULT now(),
  question text NOT NULL,
  question_recherche text NOT NULL,
  passages jsonb NOT NULL,
  reponse text NOT NULL,
  statut text NOT NULL,
  modele text NOT NULL,
  jetons jsonb NOT NULL,
  cout_usd numeric(10, 6) NOT NULL,
  durees_ms jsonb NOT NULL,
  outils jsonb NOT NULL DEFAULT '[]',
  canal text NOT NULL DEFAULT 'web',
  document boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS echanges_date ON echanges (cree_le);
CREATE TABLE IF NOT EXISTS evaluations (
  id bigserial PRIMARY KEY,
  cree_le timestamptz NOT NULL DEFAULT now(),
  resultats jsonb NOT NULL
);
"""


class Base:
    def __init__(self, url: str, dimension: int):
        self.url = url
        self.dimension = dimension
        self._bm25: Bm25 | None = None

    @contextmanager
    def connexion(self):
        with psycopg.connect(self.url, autocommit=True) as cx:
            register_vector(cx)
            yield cx

    def initialiser(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as cx:
            cx.execute("CREATE EXTENSION IF NOT EXISTS vector")
        with self.connexion() as cx:
            # Si la dimension des vecteurs a changé (nouveau modèle), on repart d'une table vide.
            ligne = cx.execute(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = to_regclass('passages') AND attname = 'vecteur'"
            ).fetchone()
            if ligne and ligne[0] != f"vector({self.dimension})":
                cx.execute("DROP TABLE passages; DELETE FROM documents;")
            cx.execute(SCHEMA.format(dim=self.dimension))
            cx.execute("ALTER TABLE passages DROP COLUMN IF EXISTS recherche")
            cx.execute("ALTER TABLE passages ADD COLUMN IF NOT EXISTS questions text NOT NULL DEFAULT ''")
            cx.execute("ALTER TABLE echanges ADD COLUMN IF NOT EXISTS outils jsonb NOT NULL DEFAULT '[]', "
                       "ADD COLUMN IF NOT EXISTS canal text NOT NULL DEFAULT 'web', "
                       "ADD COLUMN IF NOT EXISTS document boolean NOT NULL DEFAULT false")

    # --- Ingestion -----------------------------------------------------------------------------
    def documents_a_jour(self) -> dict[str, tuple[str, str]]:
        with self.connexion() as cx:
            return {s: (e, m) for s, e, m in cx.execute("SELECT source, empreinte, modele_embeddings FROM documents")}

    def remplacer_document(self, doc: Document, passages: list[Passage], vecteurs: list[list[float]], modele: str) -> None:
        import numpy as np

        with self.connexion() as cx, cx.transaction():
            cx.execute("DELETE FROM documents WHERE source = %s", (doc.source,))
            cx.execute(
                "INSERT INTO documents (source, titre, url, empreinte, modele_embeddings) VALUES (%s, %s, %s, %s, %s)",
                (doc.source, doc.titre, doc.url, doc.empreinte, modele),
            )
            with cx.cursor() as cur:
                cur.executemany(
                    "INSERT INTO passages (source, titre_document, url, section, ancre, ordre, texte, questions, vecteur) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(p.source, p.titre_document, p.url, p.section, p.ancre, p.ordre, p.texte, "\n".join(p.questions),
                      np.array(v, dtype=np.float32))
                     for p, v in zip(passages, vecteurs)],
                )

    def supprimer_absents(self, sources: set[str]) -> int:
        with self.connexion() as cx:
            r = cx.execute("DELETE FROM documents WHERE NOT (source = ANY(%s))", (list(sources),))
            return r.rowcount

    def nombre_passages(self) -> int:
        with self.connexion() as cx:
            return cx.execute("SELECT count(*) FROM passages").fetchone()[0]

    # --- Recherche -----------------------------------------------------------------------------
    _COLONNES = "id, source, titre_document, url, section, ancre, texte"

    def chercher_vecteur(self, vecteur: list[float], limite: int) -> list[Trouve]:
        import numpy as np

        with self.connexion() as cx:
            lignes = cx.execute(
                f"SELECT {self._COLONNES}, 1 - (vecteur <=> %s) AS sim FROM passages ORDER BY vecteur <=> %s LIMIT %s",
                (np.array(vecteur, dtype=np.float32), np.array(vecteur, dtype=np.float32), limite),
            ).fetchall()
        return [Trouve(*l[:7], rang=i + 1, score=float(l[7])) for i, l in enumerate(lignes)]

    def indexer_mots(self) -> None:
        """Reconstruit l'index BM25. Le titre de section est compté deux fois : il résume le passage.
        Les questions hypothétiques apportent le vocabulaire des clients (« renvoyer », « rendre »…)."""
        with self.connexion() as cx:
            lignes = cx.execute("SELECT id, titre_document, section, texte, questions FROM passages").fetchall()
        self._bm25 = Bm25([(i, f"{t} {s} {s} {x} {q}") for i, t, s, x, q in lignes])

    def chercher_texte(self, question: str, limite: int) -> list[Trouve]:
        if self._bm25 is None:
            self.indexer_mots()
        resultats = self._bm25.chercher(question, limite)
        if not resultats:
            return []
        with self.connexion() as cx:
            lignes = {l[0]: l for l in cx.execute(
                f"SELECT {self._COLONNES} FROM passages WHERE id = ANY(%s)", ([i for i, _ in resultats],))}
        return [Trouve(*lignes[i], rang=r + 1, score=round(sc, 4)) for r, (i, sc) in enumerate(resultats) if i in lignes]

    # --- Journal -------------------------------------------------------------------------------
    def journaliser(self, **e) -> None:
        with self.connexion() as cx:
            cx.execute(
                "INSERT INTO echanges (question, question_recherche, passages, reponse, statut, modele, jetons, cout_usd, durees_ms, "
                "outils, canal, document) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (e["question"], e["question_recherche"], json.dumps(e["passages"]), e["reponse"], e["statut"],
                 e["modele"], json.dumps(e["jetons"]), e["cout_usd"], json.dumps(e["durees_ms"]),
                 json.dumps(e.get("outils", []), ensure_ascii=False), e.get("canal", "web"), bool(e.get("document"))),
            )

    def cout_du_jour(self) -> float:
        with self.connexion() as cx:
            return float(cx.execute("SELECT coalesce(sum(cout_usd), 0) FROM echanges WHERE cree_le > now() - interval '1 day'").fetchone()[0])

    def statistiques(self, jours: int = 30) -> dict:
        """Agrégats d'usage pour le tableau de bord (aucun texte de question ni de réponse)."""
        with self.connexion() as cx:
            g = cx.execute(
                """SELECT count(*), coalesce(sum(cout_usd), 0), coalesce(avg(cout_usd), 0),
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY (durees_ms->>'total')::float),
                          percentile_cont(0.95) WITHIN GROUP (ORDER BY (durees_ms->>'total')::float),
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY (durees_ms->>'premier_mot')::float),
                          count(*) FILTER (WHERE statut <> 'ok'),
                          coalesce(sum((jetons->>'entree')::bigint), 0), coalesce(sum((jetons->>'cache_lecture')::bigint), 0),
                          coalesce(sum((jetons->>'cache_ecriture')::bigint), 0), coalesce(sum((jetons->>'sortie')::bigint), 0),
                          count(*) FILTER (WHERE document), count(*) FILTER (WHERE canal = 'mcp')
                   FROM echanges WHERE cree_le > now() - make_interval(days => %s)""", (jours,)).fetchone()
            par_jour = cx.execute(
                """SELECT d::date, count(e.id), coalesce(sum(e.cout_usd), 0)
                   FROM generate_series(current_date - 13, current_date, interval '1 day') d
                   LEFT JOIN echanges e ON e.cree_le::date = d::date GROUP BY d ORDER BY d""").fetchall()
            outils = cx.execute(
                """SELECT o->>'outil', count(*), round(avg((o->>'duree_ms')::numeric)) FROM echanges, jsonb_array_elements(outils) o
                   WHERE cree_le > now() - make_interval(days => %s) GROUP BY 1 ORDER BY 2 DESC""", (jours,)).fetchall()
        lus = g[7] + g[8] + g[9]
        return {
            "jours": jours, "echanges": g[0], "cout_total_usd": round(float(g[1]), 4), "cout_moyen_usd": round(float(g[2]), 5),
            "duree_mediane_ms": round(g[3] or 0), "duree_p95_ms": round(g[4] or 0), "premier_mot_median_ms": round(g[5] or 0),
            "taux_erreur": round(g[6] / g[0], 3) if g[0] else 0, "jetons_sortie": g[10],
            "part_cache": round(g[8] / lus, 3) if lus else 0, "documents_lus": g[11], "appels_mcp": g[12],
            "par_jour": [{"jour": d.isoformat(), "echanges": n, "cout_usd": round(float(c), 4)} for d, n, c in par_jour],
            "outils": [{"outil": o, "appels": n, "duree_moyenne_ms": int(m or 0)} for o, n, m in outils],
        }

    def enregistrer_evaluation(self, resultats: dict) -> None:
        with self.connexion() as cx:
            cx.execute("INSERT INTO evaluations (resultats) VALUES (%s)", (json.dumps(resultats, ensure_ascii=False),))

    def derniere_evaluation(self) -> dict:
        with self.connexion() as cx:
            l = cx.execute("SELECT resultats FROM evaluations ORDER BY id DESC LIMIT 1").fetchone()
        if not l:
            return {}
        r = dict(l[0])
        r.pop("details_agent", None)   # les réponses détaillées restent en base, pas sur la page publique
        return r

    def purger_journal(self, jours: int = 30) -> None:
        with self.connexion() as cx:
            cx.execute("DELETE FROM echanges WHERE cree_le < now() - make_interval(days => %s)", (jours,))
            cx.execute("DELETE FROM escalades WHERE cree_le < now() - make_interval(days => %s)", (jours,))

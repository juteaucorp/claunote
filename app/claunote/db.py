"""Accès PostgreSQL : documents analysés + petit état partagé site/worker."""
import os

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            SERIAL PRIMARY KEY,
    kdrive_id     BIGINT UNIQUE NOT NULL,
    nom           TEXT NOT NULL,
    statut        TEXT NOT NULL DEFAULT 'nouveau',  -- nouveau, classe, a_verifier, erreur
    titre         TEXT,
    niveau        TEXT,
    matiere       TEXT,
    type          TEXT,
    chapitre      TEXT,
    date_doc      DATE,
    mots_cles     TEXT[] NOT NULL DEFAULT '{}',
    resume        TEXT,
    transcription TEXT,
    confiance     REAL,
    erreur        TEXT,
    kdrive_md_id  BIGINT,
    cree_le       TIMESTAMPTZ NOT NULL DEFAULT now(),
    maj_le        TIMESTAMPTZ NOT NULL DEFAULT now(),
    recherche     tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('french', coalesce(titre, '') || ' ' || coalesce(chapitre, '')), 'A') ||
        setweight(to_tsvector('french', coalesce(resume, '')), 'B') ||
        setweight(to_tsvector('french', coalesce(transcription, '')), 'C')
    ) STORED
);
CREATE INDEX IF NOT EXISTS documents_recherche ON documents USING GIN (recherche);
CREATE TABLE IF NOT EXISTS etat (cle TEXT PRIMARY KEY, valeur TEXT);
"""

CHAMPS = {"nom", "statut", "titre", "niveau", "matiere", "type", "chapitre", "date_doc",
          "mots_cles", "resume", "transcription", "confiance", "erreur", "kdrive_md_id"}


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)


def init():
    with connect() as c:
        c.execute(SCHEMA)


def par_kdrive(kdrive_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM documents WHERE kdrive_id = %s", (kdrive_id,)).fetchone()


def creer(kdrive_id: int, nom: str) -> int:
    with connect() as c:
        return c.execute(
            "INSERT INTO documents (kdrive_id, nom) VALUES (%s, %s) "
            "ON CONFLICT (kdrive_id) DO UPDATE SET nom = EXCLUDED.nom RETURNING id",
            (kdrive_id, nom)).fetchone()["id"]


def maj(doc_id: int, **champs):
    inconnus = set(champs) - CHAMPS
    if inconnus:
        raise ValueError(f"champs inconnus : {inconnus}")
    cols = ", ".join(f"{k} = %s" for k in champs)
    with connect() as c:
        c.execute(f"UPDATE documents SET {cols}, maj_le = now() WHERE id = %s", (*champs.values(), doc_id))


def document(doc_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM documents WHERE id = %s", (doc_id,)).fetchone()


def bibliotheque():
    """Nombre de documents classés par niveau / matière / type."""
    with connect() as c:
        return c.execute(
            "SELECT niveau, matiere, type, count(*) AS n FROM documents WHERE statut = 'classe' "
            "GROUP BY niveau, matiere, type ORDER BY matiere, type").fetchall()


def liste(niveau: str, matiere: str, type_: str):
    with connect() as c:
        return c.execute(
            "SELECT id, titre, nom, chapitre, date_doc FROM documents "
            "WHERE statut = 'classe' AND niveau = %s AND matiere = %s AND type = %s "
            "ORDER BY date_doc NULLS LAST, titre", (niveau, matiere, type_)).fetchall()


def recherche(q: str, limite: int = 30):
    with connect() as c:
        return c.execute(
            """SELECT id, titre, nom, niveau, matiere, type, statut,
                      ts_headline('french', coalesce(transcription, ''), requete,
                                  'MaxFragments=2, MaxWords=25, MinWords=8') AS extrait
               FROM documents, websearch_to_tsquery('french', %s) AS requete
               WHERE recherche @@ requete
               ORDER BY ts_rank(recherche, requete) DESC LIMIT %s""", (q, limite)).fetchall()


def a_verifier():
    with connect() as c:
        return c.execute(
            "SELECT * FROM documents WHERE statut IN ('a_verifier', 'erreur') ORDER BY cree_le").fetchall()


def matieres() -> list[str]:
    with connect() as c:
        rows = c.execute("SELECT DISTINCT matiere FROM documents WHERE matiere IS NOT NULL ORDER BY 1").fetchall()
        return [r["matiere"] for r in rows]


def demander_scan():
    with connect() as c:
        c.execute("INSERT INTO etat VALUES ('scan', 'oui') ON CONFLICT (cle) DO UPDATE SET valeur = 'oui'")


def scan_demande() -> bool:
    """Vrai si le site a demandé un scan ; consomme la demande."""
    with connect() as c:
        return c.execute("DELETE FROM etat WHERE cle = 'scan' RETURNING cle").fetchone() is not None

"""Accès PostgreSQL : documents analysés + petit état partagé site/worker."""
import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
ALTER TABLE documents ADD COLUMN IF NOT EXISTS origine TEXT;  -- dossier de l'inbox d'où vient le scan
ALTER TABLE documents ADD COLUMN IF NOT EXISTS note TEXT;     -- pourquoi l'agent hésite
CREATE INDEX IF NOT EXISTS documents_recherche ON documents USING GIN (recherche);
CREATE TABLE IF NOT EXISTS etat (cle TEXT PRIMARY KEY, valeur TEXT);
CREATE TABLE IF NOT EXISTS revisions (
    id         SERIAL PRIMARY KEY,
    genre      TEXT NOT NULL,                        -- fiche, quiz, plan
    sujet      TEXT NOT NULL,
    doc_ids    INT[] NOT NULL,
    date_eval  DATE,
    statut     TEXT NOT NULL DEFAULT 'en_attente',   -- en_attente, pret, erreur
    contenu    TEXT,                                 -- Markdown, ou JSON pour un quiz
    reperes    JSONB NOT NULL DEFAULT '{}',          -- {"D1": id du document, …}
    erreur     TEXT,
    kdrive_id  BIGINT,
    cree_le    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pronote_evenements (
    id           TEXT PRIMARY KEY,                     -- cours:<id> ou devoir:<id> (ids Pronote)
    genre        TEXT NOT NULL,                        -- evaluation, devoir
    matiere      TEXT NOT NULL,                        -- nom Pronote
    titre        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    le           TIMESTAMPTZ NOT NULL,
    fait         BOOLEAN NOT NULL DEFAULT false,
    revision_id  INT REFERENCES revisions(id) ON DELETE SET NULL,
    vu_le        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pronote_notes (
    id              TEXT PRIMARY KEY,
    matiere         TEXT NOT NULL,
    le              TIMESTAMPTZ,
    valeur          JSONB,
    sur             JSONB,
    moyenne_classe  JSONB,
    coefficient     REAL,
    commentaire     TEXT NOT NULL DEFAULT '',
    periode         TEXT
);
"""

CHAMPS = {"nom", "statut", "titre", "niveau", "matiere", "type", "chapitre", "date_doc",
          "mots_cles", "resume", "transcription", "confiance", "erreur", "kdrive_md_id", "origine", "note"}


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


def nb_a_verifier() -> int:
    with connect() as c:
        return c.execute("SELECT count(*) AS n FROM documents WHERE statut IN ('a_verifier', 'erreur')").fetchone()["n"]


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


# --- Révisions -------------------------------------------------------------

def documents(ids: list[int]):
    """Documents dans l'ordre des ids donnés."""
    with connect() as c:
        rows = c.execute("SELECT * FROM documents WHERE id = ANY(%s)", (ids,)).fetchall()
    par_id = {r["id"]: r for r in rows}
    return [par_id[i] for i in ids if i in par_id]


def choisir_documents(niveau: str, matiere: str, q: str = "", limite: int = 15) -> list[int]:
    """Cours classés d'une matière ; si `q` est donné, les plus pertinents d'abord."""
    with connect() as c:
        if q.strip():
            rows = c.execute(
                """SELECT id FROM documents, websearch_to_tsquery('french', %s) AS requete
                   WHERE statut = 'classe' AND niveau = %s AND matiere = %s AND recherche @@ requete
                   ORDER BY ts_rank(recherche, requete) DESC LIMIT %s""",
                (q, niveau, matiere, limite)).fetchall()
        else:
            rows = c.execute(
                """SELECT id FROM documents WHERE statut = 'classe' AND niveau = %s AND matiere = %s
                   ORDER BY date_doc DESC NULLS LAST LIMIT %s""", (niveau, matiere, limite)).fetchall()
        return [r["id"] for r in rows]


def niveaux_matieres():
    """Couples (niveau, matière) qui ont des documents classés."""
    with connect() as c:
        return c.execute("SELECT DISTINCT niveau, matiere FROM documents WHERE statut = 'classe' "
                         "ORDER BY niveau, matiere").fetchall()


def creer_revision(genre: str, sujet: str, doc_ids: list[int], date_eval=None) -> int:
    with connect() as c:
        return c.execute("INSERT INTO revisions (genre, sujet, doc_ids, date_eval) VALUES (%s, %s, %s, %s) "
                         "RETURNING id", (genre, sujet, doc_ids, date_eval)).fetchone()["id"]


def revision(rev_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM revisions WHERE id = %s", (rev_id,)).fetchone()


def revisions(limite: int = 30):
    with connect() as c:
        return c.execute("SELECT id, genre, sujet, statut, date_eval, cree_le FROM revisions "
                         "ORDER BY cree_le DESC LIMIT %s", (limite,)).fetchall()


def revisions_en_attente():
    with connect() as c:
        return c.execute("SELECT * FROM revisions WHERE statut = 'en_attente' ORDER BY cree_le").fetchall()


def maj_revision(rev_id: int, statut: str, contenu=None, reperes=None, erreur=None, kdrive_id=None):
    with connect() as c:
        c.execute("UPDATE revisions SET statut = %s, contenu = %s, reperes = %s, erreur = %s, kdrive_id = %s "
                  "WHERE id = %s", (statut, contenu, Jsonb(reperes or {}), erreur, kdrive_id, rev_id))


# --- Pronote ----------------------------------------------------------------

def etat_valeur(cle: str):
    with connect() as c:
        r = c.execute("SELECT valeur FROM etat WHERE cle = %s", (cle,)).fetchone()
        return r["valeur"] if r else None


def etat_ecrire(cle: str, valeur: str | None):
    with connect() as c:
        if valeur is None:
            c.execute("DELETE FROM etat WHERE cle = %s", (cle,))
        else:
            c.execute("INSERT INTO etat VALUES (%s, %s) ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur",
                      (cle, valeur))


def enregistrer_pronote(evenements: list[dict], debut, sources: list[str], notes: list[dict] | None,
                        periode: str | None):
    """Remplace la fenêtre synchronisée : un contrôle annulé côté Pronote disparaît aussi ici.

    `sources` : préfixes d'id (« cours: », « devoir: ») bien reçus ; les autres ne sont pas touchés.
    """
    with connect() as c, c.transaction():
        ids = [e["id"] for e in evenements]
        for prefixe in sources:
            c.execute("DELETE FROM pronote_evenements WHERE le >= %s AND id LIKE %s AND NOT (id = ANY(%s))",
                      (debut, prefixe + "%", ids))
        for e in evenements:
            c.execute(
                """INSERT INTO pronote_evenements (id, genre, matiere, titre, description, le, fait)
                   VALUES (%(id)s, %(genre)s, %(matiere)s, %(titre)s, %(description)s, %(le)s, %(fait)s)
                   ON CONFLICT (id) DO UPDATE SET genre = EXCLUDED.genre, matiere = EXCLUDED.matiere,
                     titre = EXCLUDED.titre, description = EXCLUDED.description, le = EXCLUDED.le,
                     fait = EXCLUDED.fait, vu_le = now()""", e)
        if notes is not None:
            c.execute("DELETE FROM pronote_notes WHERE periode = %s", (periode,))
            for n in notes:
                c.execute(
                    """INSERT INTO pronote_notes (id, matiere, le, valeur, sur, moyenne_classe, coefficient,
                         commentaire, periode)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET valeur = EXCLUDED.valeur, sur = EXCLUDED.sur,
                         moyenne_classe = EXCLUDED.moyenne_classe, commentaire = EXCLUDED.commentaire,
                         periode = EXCLUDED.periode""",
                    (n["id"], n["matiere"], n["date"], Jsonb(n["valeur"]), Jsonb(n["sur"]),
                     Jsonb(n["moyenne_classe"]), n["coefficient"], n["commentaire"], periode))


def evenements_a_venir(genre: str | None = None, jours: int = 60):
    with connect() as c:
        return c.execute(
            """SELECT e.*, r.statut AS revision_statut, r.genre AS revision_genre FROM pronote_evenements e
               LEFT JOIN revisions r ON r.id = e.revision_id
               WHERE e.le >= date_trunc('day', now()) AND e.le < now() + make_interval(days => %s)
                 AND (%s::text IS NULL OR e.genre = %s)
               ORDER BY e.le""", (jours, genre, genre)).fetchall()


def evenement(ev_id: str):
    with connect() as c:
        return c.execute("SELECT * FROM pronote_evenements WHERE id = %s", (ev_id,)).fetchone()


def lier_revision(ev_id: str, rev_id: int):
    with connect() as c:
        c.execute("UPDATE pronote_evenements SET revision_id = %s WHERE id = %s", (rev_id, ev_id))


def notes_recentes(limite: int = 20):
    with connect() as c:
        return c.execute("SELECT * FROM pronote_notes ORDER BY le DESC NULLS LAST LIMIT %s", (limite,)).fetchall()


def nb_evaluations_proches(jours: int = 7) -> int:
    with connect() as c:
        return c.execute("SELECT count(*) AS n FROM pronote_evenements WHERE genre = 'evaluation' "
                         "AND le >= date_trunc('day', now()) AND le < now() + make_interval(days => %s)",
                         (jours,)).fetchone()["n"]

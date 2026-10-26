"""Accès PostgreSQL : documents analysés + petit état partagé site/worker."""
import os
import secrets
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            SERIAL PRIMARY KEY,
    kdrive_id     BIGINT UNIQUE NOT NULL,
    nom           TEXT NOT NULL,
    statut        TEXT NOT NULL DEFAULT 'nouveau',  -- nouveau, classe, a_verifier, erreur, hors_cours, reclasser
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
ALTER TABLE documents ADD COLUMN IF NOT EXISTS a_ranger BOOLEAN NOT NULL DEFAULT false;  -- à déplacer dans kDrive
CREATE INDEX IF NOT EXISTS documents_cree_le ON documents (cree_le);
CREATE INDEX IF NOT EXISTS documents_recherche ON documents USING GIN (recherche);
CREATE TABLE IF NOT EXISTS etat (cle TEXT PRIMARY KEY, valeur TEXT);
-- Même document dans un autre format (PDF + Word) : rangé à côté, pas analysé
ALTER TABLE documents ADD COLUMN IF NOT EXISTS variante_kdrive_id BIGINT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS variante_nom TEXT;
-- Tri d'un dossier importé, avant l'analyse (voir importation.py)
CREATE TABLE IF NOT EXISTS import_fichiers (
    kdrive_id  BIGINT PRIMARY KEY,
    lot        TEXT NOT NULL,                        -- premier dossier dans l'inbox ('' : racine)
    chemin     TEXT NOT NULL,                        -- dossiers/nom depuis l'inbox
    nom        TEXT NOT NULL,
    taille     BIGINT NOT NULL DEFAULT 0,
    categorie  TEXT NOT NULL,                        -- document, code, systeme, autre, doublon, projet
    decision   TEXT NOT NULL,                        -- analyser, inutile, variante, page, supprimer, garder, fusionne, fait
    raison     TEXT,
    principal  BIGINT,                               -- variante ou doublon : le fichier gardé
    groupe     TEXT,                                 -- page : groupe de scans à fusionner
    ordre      INT NOT NULL DEFAULT 0,
    sur        BOOLEAN NOT NULL DEFAULT true,        -- page : fusion sûre, sinon découpage à confirmer
    dossier    BOOLEAN NOT NULL DEFAULT false,
    cree_le    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS import_fichiers_decision ON import_fichiers (decision);
CREATE TABLE IF NOT EXISTS import_lots (
    lot        TEXT PRIMARY KEY,
    resume     TEXT,                                 -- comment l'ancienne organisation est rangée (IA)
    compris    JSONB NOT NULL DEFAULT '{}',          -- {"chemin/dossier": {"niveau", "matiere", "type"}}
    maj_le     TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
CREATE TABLE IF NOT EXISTS campagnes (
    id         SERIAL PRIMARY KEY,
    titre      TEXT NOT NULL,                        -- ex. « Semaine de BTS blanc »
    consigne   TEXT NOT NULL DEFAULT '',             -- contexte donné à l'IA
    debut      DATE NOT NULL,
    fin        DATE NOT NULL,
    statut     TEXT NOT NULL DEFAULT 'en_attente',   -- programme jour par jour : en_attente, pret, erreur
    programme  JSONB,                                -- {"jours": [{"date", "titre", "taches": […]}]}
    faits      TEXT[] NOT NULL DEFAULT '{}',         -- tâches cochées : « 2026-11-03:0 »
    erreur     TEXT,
    cree_le    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS campagne_id INT REFERENCES campagnes(id) ON DELETE SET NULL;
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS consigne TEXT NOT NULL DEFAULT '';
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS niveau TEXT;
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS matiere TEXT;
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS fait_le TIMESTAMPTZ;   -- « déjà fait » dans l'historique
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS score JSONB;           -- dernier quiz : {"bonnes": 7, "total": 10}
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS sources_web JSONB NOT NULL DEFAULT '[]';  -- [{"repere": "W1", "titre", "url"}]
-- Révisions d'avant l'historique : matière du premier cours utilisé
UPDATE revisions r SET niveau = d.niveau, matiere = d.matiere FROM documents d
 WHERE r.matiere IS NULL AND d.id = r.doc_ids[1] AND d.matiere IS NOT NULL;
CREATE INDEX IF NOT EXISTS revisions_cree_le ON revisions (cree_le);
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
CREATE TABLE IF NOT EXISTS pronote_documents (
    cle          TEXT PRIMARY KEY,                     -- source:matière:jour:nom (les ids Pronote changent)
    source       TEXT NOT NULL,                        -- ressource, devoir
    matiere      TEXT NOT NULL,
    le           TIMESTAMPTZ,
    titre        TEXT NOT NULL DEFAULT '',
    categorie    TEXT NOT NULL DEFAULT '',
    nom          TEXT NOT NULL,
    url          TEXT,                                 -- pour un lien
    statut       TEXT NOT NULL,                        -- depose, lien, erreur
    erreur       TEXT,
    tentatives   INT NOT NULL DEFAULT 1,
    kdrive_id    BIGINT,
    vu_le        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cartes (
    id           SERIAL PRIMARY KEY,
    revision_id  INT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    recto        TEXT NOT NULL,
    verso        TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',             -- repère D1… du cours d'origine
    facilite     REAL NOT NULL DEFAULT 2.5,            -- répétition espacée (SM-2)
    intervalle   INT NOT NULL DEFAULT 0,               -- jours
    repetitions  INT NOT NULL DEFAULT 0,
    prochaine    DATE NOT NULL DEFAULT current_date,
    vue_le       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cartes_prochaine ON cartes (prochaine);
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
CREATE TABLE IF NOT EXISTS abonnements_push (
    endpoint     TEXT PRIMARY KEY,                     -- adresse du service push (Apple, Google, Mozilla)
    p256dh       TEXT NOT NULL,                        -- clés de chiffrement de l'appareil
    auth         TEXT NOT NULL,
    appareil     TEXT NOT NULL DEFAULT '',
    cree_le      TIMESTAMPTZ NOT NULL DEFAULT now(),
    vu_le        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Fiabilité : reprises après une panne, et modèle utilisé
ALTER TABLE documents ADD COLUMN IF NOT EXISTS essais INT NOT NULL DEFAULT 0;     -- pannes de Claude sur ce document
ALTER TABLE documents ADD COLUMN IF NOT EXISTS modele TEXT;                      -- modèle qui l'a lu
ALTER TABLE documents ADD COLUMN IF NOT EXISTS avis TEXT;                        -- second avis d'un modèle plus fort
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS essais INT NOT NULL DEFAULT 0;
ALTER TABLE revisions ADD COLUMN IF NOT EXISTS pdf_essais INT NOT NULL DEFAULT 0; -- copie du PDF dans kDrive
ALTER TABLE import_lots ADD COLUMN IF NOT EXISTS a_comprendre BOOLEAN NOT NULL DEFAULT false;  -- passe IA à faire
CREATE INDEX IF NOT EXISTS documents_statut ON documents (statut);
CREATE TABLE IF NOT EXISTS appels_ia (
    id             SERIAL PRIMARY KEY,
    le             TIMESTAMPTZ NOT NULL DEFAULT now(),
    tache          TEXT NOT NULL,                      -- scan, texte, classement, fiche… (modeles.TACHES)
    modele         TEXT NOT NULL,
    resultat       TEXT NOT NULL,                      -- ok, escalade, limite, panne, connexion, modele, erreur
    duree          REAL NOT NULL DEFAULT 0,            -- secondes
    jetons_entree  INT NOT NULL DEFAULT 0,
    jetons_sortie  INT NOT NULL DEFAULT 0,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS appels_ia_le ON appels_ia (le);
DELETE FROM appels_ia WHERE le < now() - interval '60 days';
CREATE TABLE IF NOT EXISTS notifications (
    id           SERIAL PRIMARY KEY,
    cle          TEXT UNIQUE NOT NULL,                 -- empêche d'envoyer deux fois la même chose
    titre        TEXT NOT NULL,
    corps        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '/',
    statut       TEXT NOT NULL DEFAULT 'attente',      -- attente, envoye, ignore, erreur
    erreur       TEXT,
    cree_le      TIMESTAMPTZ NOT NULL DEFAULT now(),
    envoye_le    TIMESTAMPTZ
);
-- Fichiers joints à une révision ou une campagne (emploi du temps, dates d'épreuves…) :
-- du contexte pour l'IA, jamais ajouté à la bibliothèque
CREATE TABLE IF NOT EXISTS pieces_jointes (
    id           SERIAL PRIMARY KEY,
    revision_id  INT REFERENCES revisions(id) ON DELETE CASCADE,
    campagne_id  INT REFERENCES campagnes(id) ON DELETE CASCADE,
    nom          TEXT NOT NULL,
    contenu      BYTEA NOT NULL,
    taille       INT NOT NULL,
    cree_le      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pieces_jointes_revision ON pieces_jointes (revision_id);
CREATE INDEX IF NOT EXISTS pieces_jointes_campagne ON pieces_jointes (campagne_id);
-- Groupes de cours déjà condensés (beaucoup de cours) : rejoués sans rappeler l'IA
CREATE TABLE IF NOT EXISTS condenses (
    cle          TEXT PRIMARY KEY,                     -- sha256 du sujet et du texte condensé
    texte        TEXT NOT NULL,
    cree_le      TIMESTAMPTZ NOT NULL DEFAULT now()
);
DELETE FROM condenses WHERE cree_le < now() - interval '90 days';
"""

# Garde-fou des documents personnels et découpage des gros scans (sensible.py, decoupage.py)
SCHEMA += """
CREATE TABLE IF NOT EXISTS sensibles (
    id         SERIAL PRIMARY KEY,
    kdrive_id  BIGINT NOT NULL,
    nom        TEXT NOT NULL,
    chemin     TEXT NOT NULL DEFAULT '',
    categorie  TEXT,
    raisons    TEXT[] NOT NULL DEFAULT '{}',
    pages      TEXT,                                 -- pages retirées d'un gros scan (« 12-13 »)
    decision   TEXT NOT NULL,                        -- corbeille, doute, analyser (pas personnel), supprime
    le         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sensibles_kdrive ON sensibles (kdrive_id);
CREATE TABLE IF NOT EXISTS decoupages (
    kdrive_id  BIGINT PRIMARY KEY,                   -- le gros scan d'origine
    nom        TEXT NOT NULL DEFAULT '',
    chemin     TEXT NOT NULL DEFAULT '',
    pages      INT NOT NULL DEFAULT 0,
    statut     TEXT NOT NULL DEFAULT 'lecture',      -- lecture, decoupage, envoi, fait
    lecture    JSONB,                                -- une entrée par page (lecture locale)
    coupes     JSONB NOT NULL DEFAULT '{}',          -- réponses de Claude par paquet de pages
    plan       JSONB,                                -- documents produits (pages, titre, kdrive_id)
    cree_le    TIMESTAMPTZ NOT NULL DEFAULT now(),
    maj_le     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# « Demander » : questions posées dans un chat (question.py)
SCHEMA += """
CREATE TABLE IF NOT EXISTS conversations (
    id         SERIAL PRIMARY KEY,
    titre      TEXT NOT NULL,                        -- la première question
    niveau     TEXT,                                 -- portée choisie (vide : tous les cours)
    matiere    TEXT,
    cree_le    TIMESTAMPTZ NOT NULL DEFAULT now(),
    maj_le     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
    id               SERIAL PRIMARY KEY,
    conversation_id  INT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL,                  -- eleve, ia, local (réponse sans IA)
    contenu          TEXT NOT NULL DEFAULT '',       -- Markdown
    statut           TEXT NOT NULL DEFAULT 'pret',   -- en_attente, pret, erreur
    reperes          JSONB NOT NULL DEFAULT '{}',    -- {"D1": id du document, …}
    sources_web      JSONB NOT NULL DEFAULT '[]',
    modele           TEXT,
    erreur           TEXT,
    cree_le          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, id);
-- Réponse interrompue par un redémarrage du site (elle prend 5 min au plus)
UPDATE messages SET statut = 'erreur', erreur = 'Réponse interrompue (redémarrage du site) : renvoie la question.'
 WHERE statut = 'en_attente' AND cree_le < now() - interval '10 minutes';
"""

CHAMPS = {"nom", "statut", "titre", "niveau", "matiere", "type", "chapitre", "date_doc",
          "mots_cles", "resume", "transcription", "confiance", "erreur", "kdrive_md_id", "origine", "note",
          "variante_kdrive_id", "variante_nom", "essais", "modele", "avis"}


_pool: ConnectionPool | None = None
_verrou = threading.Lock()


def connect():
    """Connexion prise dans un pool : en ouvrir une coûte ~10 ms (authentification), et une page en fait plusieurs."""
    global _pool
    if _pool is None:
        with _verrou:
            if _pool is None:
                _pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=12, open=True,
                                       kwargs={"row_factory": dict_row, "autocommit": True},
                                       check=ConnectionPool.check_connection)
    return _pool.connection()


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
            "ORDER BY date_doc DESC NULLS LAST, cree_le DESC", (niveau, matiere, type_)).fetchall()


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


def nb_documents() -> int:
    with connect() as c:
        return c.execute("SELECT count(*) AS n FROM documents WHERE statut = 'classe'").fetchone()["n"]


def nb_a_verifier() -> int:
    with connect() as c:
        return c.execute("SELECT count(*) AS n FROM documents WHERE statut IN ('a_verifier', 'erreur')").fetchone()["n"]


# --- Organiser (actions de masse) ---------------------------------------------

STATUTS = {"classe": "Classé", "a_verifier": "À vérifier", "hors_cours": "Hors cours", "reclasser": "Reclassement IA",
           "erreur": "Erreur", "nouveau": "En analyse"}
CHAMPS_MASSE = {"statut", "niveau", "matiere", "type"}
SANS_ANNEE = "-"


def _filtres(du=None, au=None, niveau: str = "", matiere: str = "", type_: str = "", statut: str = "",
             texte: str = "") -> tuple[str, list]:
    """Clause WHERE des filtres de la page Organiser ; `du`/`au` portent sur la date d'ajout."""
    conditions, params = [], []
    for sql, valeur in (("cree_le >= %s", du), ("cree_le <= %s", au), ("matiere = %s", matiere or None),
                        ("type = %s", type_ or None), ("statut = %s", statut or None)):
        if valeur is not None:
            conditions.append(sql)
            params.append(valeur)
    if niveau == SANS_ANNEE:
        conditions.append("niveau IS NULL")
    elif niveau:
        conditions.append("niveau = %s")
        params.append(niveau)
    if texte.strip():
        conditions.append("(nom ILIKE %s OR titre ILIKE %s OR origine ILIKE %s)")
        params += [f"%{texte.strip()}%"] * 3
    return " AND ".join(conditions) or "true", params


def organiser(filtres: dict, limite: int = 25, decalage: int = 0) -> tuple[list, int]:
    """Une page de documents filtrés, et le nombre total de résultats."""
    where, params = _filtres(**filtres)
    with connect() as c:
        total = c.execute(f"SELECT count(*) AS n FROM documents WHERE {where}", params).fetchone()["n"]
        docs = c.execute(
            f"""SELECT id, nom, titre, statut, niveau, matiere, type, origine, cree_le, a_ranger, erreur
                FROM documents WHERE {where} ORDER BY cree_le DESC, id DESC LIMIT %s OFFSET %s""",
            (*params, limite, decalage)).fetchall()
    return docs, total


def ids_filtres(filtres: dict) -> list[int]:
    """Tous les documents d'un filtre (« appliquer à tous les résultats »)."""
    where, params = _filtres(**filtres)
    with connect() as c:
        return [r["id"] for r in c.execute(f"SELECT id FROM documents WHERE {where}", params).fetchall()]


def maj_masse(ids: list[int], **champs) -> int:
    """Change plusieurs documents d'un coup ; le worker les déplace ensuite dans kDrive."""
    inconnus = set(champs) - CHAMPS_MASSE
    if inconnus:
        raise ValueError(f"champs inconnus : {inconnus}")
    if not ids or not champs:
        return 0
    cols = ", ".join(f"{k} = %s" for k in champs)
    with connect() as c:
        # Un classement choisi à la main est sûr. Un document en analyse n'est pas touché ;
        # un document en erreur peut être classé à la main, mais pas par l'IA (pas de transcription).
        exclus = ["nouveau", "erreur"] if champs.get("statut") == "reclasser" else ["nouveau"]
        return c.execute(
            f"""UPDATE documents SET {cols}, a_ranger = true, maj_le = now(),
                  confiance = CASE WHEN %s THEN 1.0 ELSE confiance END, note = NULL
                WHERE id = ANY(%s) AND NOT (statut = ANY(%s))""",
            (*champs.values(), champs.get("statut") != "reclasser", ids, exclus)).rowcount


def completer_classement(ids: list[int]) -> int:
    """Après un changement de masse : un document complet devient classé, sinon à vérifier."""
    with connect() as c:
        return c.execute(
            """UPDATE documents SET statut = CASE WHEN niveau IS NOT NULL AND matiere IS NOT NULL
                                                   AND type IS NOT NULL THEN 'classe' ELSE 'a_verifier' END,
                                    erreur = NULL
               WHERE id = ANY(%s) AND statut IN ('classe', 'a_verifier', 'erreur')""", (ids,)).rowcount


def reessayer_erreurs() -> int:
    """Remet tous les documents en erreur dans la file d'analyse."""
    with connect() as c:
        return c.execute("UPDATE documents SET statut = 'nouveau', erreur = NULL WHERE statut = 'erreur'").rowcount


def supprimer(doc_id: int):
    with connect() as c:
        c.execute("DELETE FROM documents WHERE id = %s", (doc_id,))


def a_reclasser(limite: int):
    with connect() as c:
        return c.execute("SELECT * FROM documents WHERE statut = 'reclasser' ORDER BY maj_le LIMIT %s",
                         (limite,)).fetchall()


def a_ranger():
    with connect() as c:
        return c.execute("SELECT * FROM documents WHERE a_ranger AND statut <> 'reclasser' ORDER BY id").fetchall()


def marquer_a_ranger(ids: list[int], valeur: bool = True):
    with connect() as c:
        c.execute("UPDATE documents SET a_ranger = %s WHERE id = ANY(%s)", (valeur, ids))


def hors_cours():
    with connect() as c:
        return c.execute("SELECT id, nom, titre, resume, date_doc, cree_le, origine FROM documents "
                         "WHERE statut = 'hors_cours' ORDER BY date_doc DESC NULLS LAST, cree_le DESC").fetchall()


def nb_hors_cours() -> int:
    with connect() as c:
        return c.execute("SELECT count(*) AS n FROM documents WHERE statut = 'hors_cours'").fetchone()["n"]


def matieres() -> list[str]:
    with connect() as c:
        rows = c.execute("SELECT DISTINCT matiere FROM documents WHERE matiere IS NOT NULL ORDER BY 1").fetchall()
        return [r["matiere"] for r in rows]


def matieres_par_annee() -> list[dict]:
    """Documents par année et matière (tous statuts sauf hors cours), pour repérer les doublons."""
    with connect() as c:
        return c.execute("SELECT niveau, matiere, count(*) AS n FROM documents WHERE niveau IS NOT NULL "
                         "AND matiere IS NOT NULL AND statut <> 'hors_cours' GROUP BY niveau, matiere "
                         "ORDER BY niveau, matiere").fetchall()


def titres_par_matiere(par_matiere: int = 4) -> dict[tuple, list[str]]:
    """Quelques titres de cours par (année, matière), pour que l'IA comprenne ce que recouvre chaque nom."""
    with connect() as c:
        rows = c.execute(
            """SELECT niveau, matiere, titre FROM (
                 SELECT niveau, matiere, coalesce(titre, nom) AS titre,
                        row_number() OVER (PARTITION BY niveau, matiere ORDER BY cree_le DESC) AS rang
                 FROM documents WHERE niveau IS NOT NULL AND matiere IS NOT NULL AND statut <> 'hors_cours') t
               WHERE rang <= %s""", (par_matiere,)).fetchall()
    titres: dict[tuple, list[str]] = {}
    for r in rows:
        titres.setdefault((r["niveau"], r["matiere"]), []).append(r["titre"])
    return titres


def fusionner_matieres(niveau: str, anciennes: list[str], nouvelle: str) -> int:
    """Une seule matière pour une année : documents et révisions renommés ; le worker déplace les fichiers."""
    anciennes = [m for m in anciennes if m != nouvelle]
    if not anciennes:
        return 0
    with connect() as c, c.transaction():
        n = c.execute("""UPDATE documents SET matiere = %s, maj_le = now(), a_ranger = (statut = 'classe' OR a_ranger)
                         WHERE niveau = %s AND matiere = ANY(%s) AND statut <> 'nouveau'""",
                      (nouvelle, niveau, anciennes)).rowcount
        c.execute("UPDATE revisions SET matiere = %s WHERE niveau = %s AND matiere = ANY(%s)",
                  (nouvelle, niveau, anciennes))
        c.execute("UPDATE conversations SET matiere = %s WHERE niveau = %s AND matiere = ANY(%s)",
                  (nouvelle, niveau, anciennes))
    return n


def secret_session() -> str:
    """Clé de signature des cookies, créée une fois et gardée en base (survit aux redémarrages)."""
    with connect() as c:
        c.execute("INSERT INTO etat VALUES ('secret_session', %s) ON CONFLICT (cle) DO NOTHING",
                  (secrets.token_hex(32),))
        return c.execute("SELECT valeur FROM etat WHERE cle = 'secret_session'").fetchone()["valeur"]


def etat_initialiser(cle: str, valeur: str) -> str:
    """Pose la valeur si la clé est libre et renvoie celle gardée (le site et le worker démarrent ensemble)."""
    with connect() as c:
        c.execute("INSERT INTO etat VALUES (%s, %s) ON CONFLICT (cle) DO NOTHING", (cle, valeur))
        return c.execute("SELECT valeur FROM etat WHERE cle = %s", (cle,)).fetchone()["valeur"]


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


def choisir_documents(niveau: str, matiere: str, q: str = "", limite: int | None = None) -> list[int]:
    """Cours classés d'une matière, tous par défaut ; si `q` est donné, ceux qui en parlent, les plus pertinents d'abord.

    Pas de limite : au-delà de ce que l'IA lit d'un coup, les cours sont condensés par groupes (revision.generer).
    """
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


def creer_revision(genre: str, sujet: str, doc_ids: list[int], date_eval=None, consigne: str = "",
                   campagne_id: int | None = None) -> int:
    """La matière de la révision est celle de son premier cours (filtre de l'historique)."""
    with connect() as c:
        return c.execute(
            """INSERT INTO revisions (genre, sujet, doc_ids, date_eval, consigne, campagne_id, niveau, matiere)
               SELECT %s, %s, %s, %s, %s, %s, d.niveau, d.matiere
               FROM (SELECT 1) AS x LEFT JOIN documents d ON d.id = %s RETURNING id""",
            (genre, sujet, doc_ids, date_eval, consigne, campagne_id, doc_ids[0] if doc_ids else None)
        ).fetchone()["id"]


def revision(rev_id: int):
    with connect() as c:
        return c.execute("SELECT r.*, k.titre AS campagne_titre FROM revisions r "
                         "LEFT JOIN campagnes k ON k.id = r.campagne_id WHERE r.id = %s", (rev_id,)).fetchone()


def revisions(limite: int = 30):
    with connect() as c:
        return c.execute("SELECT id, genre, sujet, statut, date_eval, cree_le FROM revisions "
                         "ORDER BY cree_le DESC LIMIT %s", (limite,)).fetchall()


def historique(genre: str = "", matiere: str = "", q: str = "", limite: int = 20, decalage: int = 0):
    """Révisions déjà générées, filtrées ; renvoie (lignes, total)."""
    where = "WHERE (%(genre)s = '' OR r.genre = %(genre)s) AND (%(matiere)s = '' OR r.matiere = %(matiere)s) " \
            "AND (%(q)s = '' OR r.sujet ILIKE '%%' || %(q)s || '%%' OR k.titre ILIKE '%%' || %(q)s || '%%')"
    p = {"genre": genre, "matiere": matiere, "q": q.strip(), "limite": limite, "decalage": decalage}
    with connect() as c:
        total = c.execute(f"SELECT count(*) AS n FROM revisions r LEFT JOIN campagnes k ON k.id = r.campagne_id "
                          f"{where}", p).fetchone()["n"]
        lignes = c.execute(
            f"""SELECT r.id, r.genre, r.sujet, r.statut, r.date_eval, r.cree_le, r.niveau, r.matiere, r.fait_le,
                       r.score, r.campagne_id, k.titre AS campagne_titre
                FROM revisions r LEFT JOIN campagnes k ON k.id = r.campagne_id {where}
                ORDER BY r.cree_le DESC, r.id DESC LIMIT %(limite)s OFFSET %(decalage)s""", p).fetchall()
    return lignes, total


def compte_revisions() -> dict:
    """Nombre de révisions par genre, et combien sont en préparation."""
    with connect() as c:
        rows = c.execute("SELECT genre, count(*) AS n, count(*) FILTER (WHERE statut = 'en_attente') AS attente "
                         "FROM revisions GROUP BY genre").fetchall()
    return {"genres": {r["genre"]: r["n"] for r in rows}, "attente": sum(r["attente"] for r in rows),
            "total": sum(r["n"] for r in rows)}


def matieres_revisions() -> list[str]:
    with connect() as c:
        return [r["matiere"] for r in c.execute(
            "SELECT DISTINCT matiere FROM revisions WHERE matiere IS NOT NULL ORDER BY matiere").fetchall()]


def revisions_en_attente():
    """Révisions à générer ; dans une campagne, dans l'ordre où elles ont été demandées."""
    with connect() as c:
        return c.execute("SELECT * FROM revisions WHERE statut = 'en_attente' ORDER BY essais, cree_le, id").fetchall()


def maj_revision(rev_id: int, statut: str, contenu=None, reperes=None, erreur=None, kdrive_id=None, web=None):
    with connect() as c:
        c.execute("UPDATE revisions SET statut = %s, contenu = %s, reperes = %s, erreur = %s, kdrive_id = %s, "
                  "sources_web = %s WHERE id = %s",
                  (statut, contenu, Jsonb(reperes or {}), erreur, kdrive_id, Jsonb(web or []), rev_id))


def relancer_revision(rev_id: int):
    with connect() as c:
        c.execute("UPDATE revisions SET statut = 'en_attente', erreur = NULL WHERE id = %s", (rev_id,))


def marquer_fait(rev_id: int, fait: bool = True, score: dict | None = None):
    with connect() as c:
        c.execute("UPDATE revisions SET fait_le = CASE WHEN %s THEN now() END, "
                  "score = coalesce(%s, score) WHERE id = %s", (fait, Jsonb(score) if score else None, rev_id))


def supprimer_revision(rev_id: int):
    with connect() as c:
        c.execute("DELETE FROM revisions WHERE id = %s", (rev_id,))


def ajouter_pieces(fichiers: list[tuple[str, bytes]], revision_id: int | None = None,
                   campagne_id: int | None = None):
    with connect() as c:
        for nom, contenu in fichiers:
            c.execute("INSERT INTO pieces_jointes (revision_id, campagne_id, nom, contenu, taille) "
                      "VALUES (%s, %s, %s, %s, %s)", (revision_id, campagne_id, nom, contenu, len(contenu)))


def pieces_jointes(revision_id: int | None = None, campagne_id: int | None = None, contenu: bool = False):
    """Pièces d'une révision (et de sa campagne) ou d'une campagne ; `contenu` ajoute les octets."""
    colonnes = "id, nom, taille, revision_id, campagne_id" + (", contenu" if contenu else "")
    with connect() as c:
        return c.execute(f"SELECT {colonnes} FROM pieces_jointes WHERE revision_id = %s OR campagne_id = %s "
                         "ORDER BY id", (revision_id, campagne_id)).fetchall()


def piece_jointe(piece_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM pieces_jointes WHERE id = %s", (piece_id,)).fetchone()


class Condenses:
    """Mémoire des groupes de cours condensés, partagée par les supports d'une même campagne."""

    def get(self, cle: str) -> str | None:
        with connect() as c:
            ligne = c.execute("SELECT texte FROM condenses WHERE cle = %s", (cle,)).fetchone()
        return ligne["texte"] if ligne else None

    def set(self, cle: str, texte: str):
        with connect() as c:
            c.execute("INSERT INTO condenses (cle, texte) VALUES (%s, %s) ON CONFLICT (cle) DO UPDATE "
                      "SET texte = excluded.texte, cree_le = now()", (cle, texte))


def documents_matiere(niveau: str, matiere: str):
    """Cours classés d'une matière, pour les choisir un par un."""
    with connect() as c:
        return c.execute("SELECT id, nom, titre, type, chapitre, date_doc FROM documents "
                         "WHERE statut = 'classe' AND niveau = %s AND matiere = %s "
                         "ORDER BY date_doc DESC NULLS LAST, id DESC", (niveau, matiere)).fetchall()


def niveaux_matieres_comptes():
    """Couples (niveau, matière) avec leur nombre de cours classés."""
    with connect() as c:
        return c.execute("SELECT niveau, matiere, count(*) AS n FROM documents WHERE statut = 'classe' "
                         "AND niveau IS NOT NULL AND matiere IS NOT NULL GROUP BY niveau, matiere "
                         "ORDER BY niveau, matiere").fetchall()


# --- Campagnes (plusieurs révisions d'un coup, avec un programme jour par jour) --

def creer_campagne(titre: str, consigne: str, debut, fin) -> int:
    with connect() as c:
        return c.execute("INSERT INTO campagnes (titre, consigne, debut, fin) VALUES (%s, %s, %s, %s) RETURNING id",
                         (titre, consigne, debut, fin)).fetchone()["id"]


def campagne(campagne_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM campagnes WHERE id = %s", (campagne_id,)).fetchone()


def campagnes(limite: int = 20):
    """Campagnes récentes avec l'avancement de leurs révisions."""
    with connect() as c:
        return c.execute(
            """SELECT k.*, count(r.id) AS nb, count(r.id) FILTER (WHERE r.statut = 'pret') AS pretes,
                      count(r.id) FILTER (WHERE r.fait_le IS NOT NULL) AS faites
               FROM campagnes k LEFT JOIN revisions r ON r.campagne_id = k.id
               GROUP BY k.id ORDER BY k.fin >= current_date DESC, k.debut DESC, k.id DESC LIMIT %s""",
            (limite,)).fetchall()


def campagne_du_jour(jour):
    """Campagne en cours (ou qui commence bientôt) dont le programme est prêt."""
    with connect() as c:
        return c.execute("SELECT * FROM campagnes WHERE statut = 'pret' AND fin >= %s AND debut <= %s + 7 "
                         "ORDER BY debut, id LIMIT 1", (jour, jour)).fetchone()


def revisions_campagne(campagne_id: int):
    with connect() as c:
        return c.execute("SELECT id, genre, sujet, statut, niveau, matiere, fait_le, score, erreur, doc_ids "
                         "FROM revisions WHERE campagne_id = %s ORDER BY matiere, id", (campagne_id,)).fetchall()


def campagnes_en_attente():
    with connect() as c:
        return c.execute("SELECT * FROM campagnes WHERE statut = 'en_attente' ORDER BY cree_le").fetchall()


def maj_campagne(campagne_id: int, statut: str, programme=None, erreur=None):
    with connect() as c:
        c.execute("UPDATE campagnes SET statut = %s, programme = %s, erreur = %s WHERE id = %s",
                  (statut, Jsonb(programme) if programme is not None else None, erreur, campagne_id))


def relancer_campagne(campagne_id: int):
    with connect() as c:
        c.execute("UPDATE campagnes SET statut = 'en_attente', erreur = NULL WHERE id = %s", (campagne_id,))
        c.execute("UPDATE revisions SET statut = 'en_attente', erreur = NULL "
                  "WHERE campagne_id = %s AND statut = 'erreur'", (campagne_id,))


def basculer_tache(campagne_id: int, cle: str) -> bool:
    """Coche ou décoche une tâche du programme ; renvoie vrai si elle est maintenant faite."""
    with connect() as c:
        r = c.execute(
            """UPDATE campagnes SET faits = CASE WHEN %(cle)s = ANY(faits) THEN array_remove(faits, %(cle)s)
                                                 ELSE array_append(faits, %(cle)s) END
               WHERE id = %(id)s RETURNING %(cle)s = ANY(faits) AS fait""", {"cle": cle, "id": campagne_id}).fetchone()
        return bool(r and r["fait"])


def supprimer_campagne(campagne_id: int, avec_revisions: bool = True):
    with connect() as c, c.transaction():
        if avec_revisions:
            c.execute("DELETE FROM revisions WHERE campagne_id = %s", (campagne_id,))
        c.execute("DELETE FROM campagnes WHERE id = %s", (campagne_id,))


# --- Flashcards -------------------------------------------------------------

def creer_cartes(rev_id: int, cartes: list[dict]):
    with connect() as c, c.transaction():
        c.execute("DELETE FROM cartes WHERE revision_id = %s", (rev_id,))
        for k in cartes:
            c.execute("INSERT INTO cartes (revision_id, recto, verso, source) VALUES (%s, %s, %s, %s)",
                      (rev_id, k["recto"], k["verso"], k.get("source") or ""))


def paquets():
    """Paquets de cartes (une révision « cartes » prête = un paquet), avec les cartes à revoir."""
    with connect() as c:
        return c.execute(
            """SELECT r.id, r.sujet, r.cree_le, count(k.id) AS total,
                      count(k.id) FILTER (WHERE k.prochaine <= current_date) AS a_revoir,
                      count(k.id) FILTER (WHERE k.repetitions = 0) AS nouvelles
               FROM revisions r JOIN cartes k ON k.revision_id = r.id
               GROUP BY r.id ORDER BY a_revoir DESC, r.cree_le DESC""").fetchall()


def cartes(rev_id: int | None = None):
    with connect() as c:
        return c.execute(
            """SELECT k.*, r.sujet, r.reperes FROM cartes k JOIN revisions r ON r.id = k.revision_id
               WHERE %s::int IS NULL OR k.revision_id = %s ORDER BY r.id, k.id""", (rev_id, rev_id)).fetchall()


def prochaine_carte(rev_id: int | None = None):
    """Carte la plus en retard (les nouvelles après celles déjà vues)."""
    with connect() as c:
        return c.execute(
            """SELECT k.*, r.sujet, r.reperes FROM cartes k JOIN revisions r ON r.id = k.revision_id
               WHERE k.prochaine <= current_date AND (%s::int IS NULL OR k.revision_id = %s)
               ORDER BY k.repetitions = 0, k.prochaine, k.vue_le NULLS FIRST, k.id LIMIT 1""",
            (rev_id, rev_id)).fetchone()


def carte(carte_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM cartes WHERE id = %s", (carte_id,)).fetchone()


def maj_carte(carte_id: int, facilite: float, intervalle: int, repetitions: int, prochaine):
    with connect() as c:
        c.execute("UPDATE cartes SET facilite = %s, intervalle = %s, repetitions = %s, prochaine = %s, "
                  "vue_le = now() WHERE id = %s", (facilite, intervalle, repetitions, prochaine, carte_id))


def nb_cartes_a_revoir(rev_id: int | None = None) -> int:
    with connect() as c:
        return c.execute("SELECT count(*) AS n FROM cartes WHERE prochaine <= current_date "
                         "AND (%s::int IS NULL OR revision_id = %s)", (rev_id, rev_id)).fetchone()["n"]


# --- Pronote ----------------------------------------------------------------

def etat_valeur(cle: str):
    with connect() as c:
        r = c.execute("SELECT valeur FROM etat WHERE cle = %s", (cle,)).fetchone()
        return r["valeur"] if r else None


def etat_consommer(cle: str) -> bool:
    """Vrai si la clé était posée ; la retire (demande ponctuelle du site au worker)."""
    with connect() as c:
        return c.execute("DELETE FROM etat WHERE cle = %s RETURNING cle", (cle,)).fetchone() is not None


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


ESSAIS_DOCUMENT = 3


def cles_documents_pronote() -> list[str]:
    """Documents à ne plus demander : récupérés, liens, ou en échec après 3 essais."""
    with connect() as c:
        return [r["cle"] for r in c.execute(
            "SELECT cle FROM pronote_documents WHERE statut <> 'erreur' OR tentatives >= %s",
            (ESSAIS_DOCUMENT,)).fetchall()]


def enregistrer_document_pronote(d: dict, statut: str, kdrive_id=None, erreur=None):
    with connect() as c:
        c.execute(
            """INSERT INTO pronote_documents (cle, source, matiere, le, titre, categorie, nom, url, statut,
                 erreur, kdrive_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (cle) DO UPDATE SET statut = EXCLUDED.statut, erreur = EXCLUDED.erreur,
                 kdrive_id = EXCLUDED.kdrive_id, url = EXCLUDED.url, tentatives = pronote_documents.tentatives + 1,
                 vu_le = now()""",
            (d["cle"], d["source"], d["matiere"], d.get("date"), d.get("titre") or "", d.get("categorie") or "",
             d["nom"], d.get("url"), statut, erreur, kdrive_id))


def documents_pronote(limite: int = 30):
    """Derniers documents des profs, avec le document Claunote une fois analysé."""
    with connect() as c:
        return c.execute(
            """SELECT p.*, d.id AS doc_id, d.statut AS doc_statut, d.titre AS doc_titre
               FROM pronote_documents p LEFT JOIN documents d ON d.kdrive_id = p.kdrive_id
               ORDER BY p.le DESC NULLS LAST, p.nom LIMIT %s""", (limite,)).fetchall()


# --- Notifications push -----------------------------------------------------

def enregistrer_abonnement(endpoint: str, p256dh: str, auth: str, appareil: str = ""):
    with connect() as c:
        c.execute("""INSERT INTO abonnements_push (endpoint, p256dh, auth, appareil) VALUES (%s, %s, %s, %s)
                     ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth,
                       appareil = EXCLUDED.appareil, vu_le = now()""", (endpoint, p256dh, auth, appareil))


def retirer_abonnement(endpoint: str):
    with connect() as c:
        c.execute("DELETE FROM abonnements_push WHERE endpoint = %s", (endpoint,))


def abonnements():
    with connect() as c:
        return c.execute("SELECT * FROM abonnements_push ORDER BY cree_le").fetchall()


def notifier(cle: str, titre: str, corps: str = "", url: str = "/", statut: str = "attente") -> bool:
    """Met une notification dans la file ; faux si cette clé a déjà servi."""
    with connect() as c:
        return c.execute("INSERT INTO notifications (cle, titre, corps, url, statut) VALUES (%s, %s, %s, %s, %s) "
                         "ON CONFLICT (cle) DO NOTHING RETURNING id", (cle, titre, corps, url, statut)
                         ).fetchone() is not None


def notification_connue(cle: str) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM notifications WHERE cle = %s", (cle,)).fetchone() is not None


def notifications_en_attente():
    with connect() as c:
        return c.execute("SELECT * FROM notifications WHERE statut = 'attente' "
                         "AND cree_le > now() - interval '12 hours' ORDER BY id").fetchall()


def maj_notification(notif_id: int, statut: str, erreur: str | None = None):
    with connect() as c:
        c.execute("UPDATE notifications SET statut = %s, erreur = %s, envoye_le = now() WHERE id = %s",
                  (statut, erreur, notif_id))


def notifications_recentes(limite: int = 10):
    with connect() as c:
        return c.execute("SELECT * FROM notifications WHERE statut <> 'ignore' ORDER BY id DESC LIMIT %s",
                         (limite,)).fetchall()


def nouveaux_cours_sans_revision(jours: int = 7, minimum: int = 3):
    """Matière où des cours récents sont arrivés cette semaine sans aucune révision faite dessus.

    « Récents » : datés du dernier mois ou venus de Pronote, pour ignorer l'import des vieux cahiers.
    """
    with connect() as c:
        return c.execute(
            """SELECT d.niveau, d.matiere, count(*) AS n FROM documents d
               WHERE d.statut = 'classe' AND d.cree_le > now() - make_interval(days => %s)
                 AND (d.date_doc >= current_date - 30 OR d.origine ILIKE '%%pronote%%')
                 AND NOT EXISTS (SELECT 1 FROM revisions r WHERE d.id = ANY(r.doc_ids))
               GROUP BY d.niveau, d.matiere HAVING count(*) >= %s
               ORDER BY n DESC LIMIT 1""", (jours, minimum)).fetchone()


def evenements_entre(debut, fin):
    """Évaluations et devoirs Pronote entre deux dates (incluses), pour le programme d'une campagne."""
    with connect() as c:
        return c.execute("SELECT genre, matiere, titre, le FROM pronote_evenements "
                         "WHERE le >= %s AND le < %s::date + 1 ORDER BY le", (debut, fin)).fetchall()


# --- Import de dossiers entiers ------------------------------------------------

def import_connus(ids: list[int]) -> dict[int, str]:
    """Décision du tri pour chaque fichier déjà vu."""
    with connect() as c:
        rows = c.execute("SELECT kdrive_id, decision FROM import_fichiers WHERE kdrive_id = ANY(%s)", (ids,)).fetchall()
    return {r["kdrive_id"]: r["decision"] for r in rows}


def import_enregistrer(lot: str, fichiers):
    """Enregistre le tri d'un lot (importation.Fichier)."""
    with connect() as c, c.cursor() as cur:
        cur.executemany(
            """INSERT INTO import_fichiers (kdrive_id, lot, chemin, nom, taille, categorie, decision, raison,
                                            principal, groupe, ordre, sur, dossier)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (kdrive_id) DO NOTHING""",
            [(f.id, lot, f.chemin, f.nom, f.taille, f.categorie, f.decision, f.raison, f.principal, f.groupe,
              f.ordre, f.sur, f.dossier) for f in fichiers])


def import_lot(lot: str):
    with connect() as c:
        return c.execute("SELECT * FROM import_lots WHERE lot = %s", (lot,)).fetchone()


def import_lot_maj(lot: str, resume: str | None, compris: dict):
    with connect() as c:
        c.execute("""INSERT INTO import_lots (lot, resume, compris) VALUES (%s, %s, %s)
                     ON CONFLICT (lot) DO UPDATE SET resume = coalesce(EXCLUDED.resume, import_lots.resume),
                       compris = import_lots.compris || EXCLUDED.compris, maj_le = now()""",
                  (lot, resume, Jsonb(compris)))


def import_marquer(ids: list[int], decision: str, **champs):
    sets = "".join(f", {k} = %s" for k in champs if k in {"groupe", "ordre", "sur", "principal"})
    with connect() as c:
        return c.execute(f"UPDATE import_fichiers SET decision = %s{sets} WHERE kdrive_id = ANY(%s)",
                         (decision, *[v for k, v in champs.items() if k in {"groupe", "ordre", "sur", "principal"}],
                          ids)).rowcount


def import_oublier(ids: list[int]):
    with connect() as c:
        c.execute("DELETE FROM import_fichiers WHERE kdrive_id = ANY(%s)", (ids,))


def import_fichiers(decision: str, ids: list[int] | None = None):
    with connect() as c:
        if ids is None:
            return c.execute("SELECT * FROM import_fichiers WHERE decision = %s ORDER BY lot, groupe, ordre, chemin",
                             (decision,)).fetchall()
        return c.execute("SELECT * FROM import_fichiers WHERE decision = %s AND kdrive_id = ANY(%s) "
                         "ORDER BY groupe, ordre", (decision, ids)).fetchall()


def import_a_executer():
    """Choix faits sur le site, que le worker applique dans kDrive."""
    with connect() as c:
        return c.execute("SELECT * FROM import_fichiers WHERE decision IN ('supprimer', 'garder') "
                         "ORDER BY kdrive_id").fetchall()


def import_groupe(groupe: str):
    with connect() as c:
        return c.execute("SELECT * FROM import_fichiers WHERE groupe = %s AND decision = 'page' ORDER BY ordre",
                         (groupe,)).fetchall()


def import_groupes(sur: bool) -> list[str]:
    with connect() as c:
        rows = c.execute("SELECT DISTINCT groupe FROM import_fichiers WHERE decision = 'page' AND sur = %s "
                         "ORDER BY groupe", (sur,)).fetchall()
    return [r["groupe"] for r in rows]


def variantes_de(kdrive_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM import_fichiers WHERE decision = 'variante' AND principal = %s "
                         "ORDER BY kdrive_id", (kdrive_id,)).fetchall()


def import_basculer(ancien: int, nouveau: int):
    """L'analyse du fichier principal a échoué : son autre format est analysé à sa place."""
    with connect() as c:
        c.execute("UPDATE import_fichiers SET decision = 'analyser', principal = NULL, raison = %s "
                  "WHERE kdrive_id = %s", ("l'autre format n'a pas pu être lu", nouveau))
        c.execute("UPDATE import_fichiers SET principal = %s WHERE decision = 'variante' AND principal = %s",
                  (nouveau, ancien))
        c.execute("UPDATE import_fichiers SET decision = 'variante', principal = %s WHERE kdrive_id = %s",
                  (nouveau, ancien))


def nb_import_a_confirmer() -> int:
    """Fichiers inutiles proposés à la suppression, scans au découpage incertain, documents peut-être personnels."""
    with connect() as c:
        return c.execute(
            """SELECT (SELECT count(*) FROM import_fichiers WHERE decision = 'inutile')
                    + (SELECT count(DISTINCT groupe) FROM import_fichiers WHERE decision = 'page' AND NOT sur)
                    + (SELECT count(*) FROM sensibles WHERE decision = 'doute') AS n"""
        ).fetchone()["n"]


def import_bilan():
    """Par lot : combien de fichiers dans chaque état (page Import)."""
    with connect() as c:
        rows = c.execute(
            """SELECT f.lot, max(f.cree_le) AS le, count(*) AS total,
                      count(*) FILTER (WHERE f.decision IN ('analyser', 'fait') AND f.categorie = 'document'
                                         AND f.principal IS NULL) AS documents,
                      count(*) FILTER (WHERE f.decision = 'analyser') AS en_attente,
                      count(*) FILTER (WHERE f.decision = 'variante' OR (f.decision = 'fait' AND f.principal IS NOT NULL
                                                                         AND f.categorie = 'document')) AS variantes,
                      count(*) FILTER (WHERE f.decision = 'fusionne') AS pages_fusionnees,
                      count(DISTINCT f.groupe) FILTER (WHERE f.decision = 'fusionne') AS fusions,
                      count(*) FILTER (WHERE f.decision = 'inutile') AS inutiles,
                      count(*) FILTER (WHERE f.categorie <> 'document' AND f.decision IN ('supprimer', 'garder', 'fait'))
                        AS inutiles_traites,
                      l.resume, l.compris
               FROM import_fichiers f LEFT JOIN import_lots l ON l.lot = f.lot
               GROUP BY f.lot, l.resume, l.compris ORDER BY max(f.cree_le) DESC LIMIT 12""").fetchall()
    return rows


def import_dossiers(lot: str) -> dict[str, int]:
    """Dossiers d'un lot qui contiennent des documents gardés, avec leur nombre de documents."""
    with connect() as c:
        rows = c.execute(
            """SELECT regexp_replace(chemin, '/?[^/]*$', '') AS dossier, count(*) AS n FROM import_fichiers
               WHERE lot = %s AND categorie = 'document' AND decision IN ('analyser', 'fait', 'page')
                 AND principal IS NULL
               GROUP BY 1 ORDER BY 1""", (lot,)).fetchall()
    return {r["dossier"]: r["n"] for r in rows}


# --- Fiabilité : file d'attente, reprises, journal des appels à Claude ---------------

def statuts_kdrive(ids: list[int]) -> dict[int, dict]:
    """{kdrive_id: {statut, essais}} des fichiers déjà connus (une requête pour toute l'inbox)."""
    with connect() as c:
        rows = c.execute("SELECT kdrive_id, statut, essais FROM documents WHERE kdrive_id = ANY(%s)", (ids,)).fetchall()
    return {r["kdrive_id"]: r for r in rows}


def documents_orphelins(dans_inbox: list[int], limite: int = 10):
    """Documents restés « nouveau » alors que leur fichier n'est plus dans l'inbox.

    Ça arrivait quand le worker s'arrêtait entre le déplacement du scan et l'enregistrement
    de l'analyse : le fichier est rangé quelque part dans kDrive, il faut le relire.
    """
    with connect() as c:
        return c.execute(
            """SELECT * FROM documents WHERE statut = 'nouveau' AND NOT (kdrive_id = ANY(%s))
                 AND maj_le < now() - interval '30 minutes' ORDER BY id LIMIT %s""",
            (dans_inbox, limite)).fetchall()


def compter_essai(table: str, ligne_id: int) -> int:
    """Une panne de Claude de plus sur ce document ou cette révision ; renvoie le total."""
    if table not in ("documents", "revisions"):
        raise ValueError(table)
    with connect() as c:
        return c.execute(f"UPDATE {table} SET essais = essais + 1 WHERE id = %s RETURNING essais",
                         (ligne_id,)).fetchone()["essais"]


def revisions_sans_pdf(limite: int = 5):
    """Révisions prêtes dont la copie PDF dans kDrive a échoué (kDrive en panne à ce moment-là)."""
    with connect() as c:
        return c.execute(
            """SELECT r.*, k.titre AS campagne_titre FROM revisions r LEFT JOIN campagnes k ON k.id = r.campagne_id
               WHERE r.statut = 'pret' AND r.kdrive_id IS NULL AND r.pdf_essais < 5
                 AND r.cree_le > now() - interval '30 days' ORDER BY r.id LIMIT %s""", (limite,)).fetchall()


def maj_pdf_revision(rev_id: int, kdrive_id: int | None):
    with connect() as c:
        c.execute("UPDATE revisions SET kdrive_id = coalesce(%s, kdrive_id), pdf_essais = pdf_essais + 1 "
                  "WHERE id = %s", (kdrive_id, rev_id))


def import_a_comprendre(lot: str):
    """Le lot a besoin de la passe IA sur ses noms de dossiers, faite dès que Claude est disponible."""
    with connect() as c:
        c.execute("""INSERT INTO import_lots (lot, a_comprendre) VALUES (%s, true)
                     ON CONFLICT (lot) DO UPDATE SET a_comprendre = true""", (lot,))


def lots_a_comprendre() -> list[str]:
    with connect() as c:
        return [r["lot"] for r in c.execute("SELECT lot FROM import_lots WHERE a_comprendre ORDER BY maj_le")]


def lot_compris(lot: str):
    with connect() as c:
        c.execute("UPDATE import_lots SET a_comprendre = false, maj_le = now() WHERE lot = %s", (lot,))


def journal_ia(tache: str, modele: str, resultat: str, duree: float = 0, jetons_entree: int = 0,
               jetons_sortie: int = 0, detail: str | None = None):
    with connect() as c:
        c.execute("INSERT INTO appels_ia (tache, modele, resultat, duree, jetons_entree, jetons_sortie, detail) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                  (tache or "?", modele, resultat, duree, jetons_entree, jetons_sortie, detail))


def appels_ia(limite: int = 25):
    with connect() as c:
        return c.execute("SELECT * FROM appels_ia ORDER BY id DESC LIMIT %s", (limite,)).fetchall()


def bilan_ia(heures: int = 24):
    """Appels réussis par modèle sur les dernières heures (page État)."""
    with connect() as c:
        return c.execute(
            """SELECT modele, count(*) FILTER (WHERE resultat = 'ok') AS appels,
                      count(*) FILTER (WHERE resultat = 'escalade') AS escalades,
                      coalesce(round(sum(duree) FILTER (WHERE resultat = 'ok') / 60), 0) AS minutes,
                      coalesce(sum(jetons_sortie), 0) AS jetons_sortie
               FROM appels_ia WHERE le > now() - make_interval(hours => %s)
               GROUP BY modele ORDER BY appels DESC""", (heures,)).fetchall()


def file_attente() -> dict:
    """Ce qui attend le worker (page État et bandeau)."""
    with connect() as c:
        return c.execute(
            """SELECT (SELECT count(*) FROM import_fichiers f LEFT JOIN documents d ON d.kdrive_id = f.kdrive_id
                        WHERE f.decision = 'analyser' AND f.categorie = 'document'
                          AND coalesce(d.statut, 'nouveau') = 'nouveau') AS scans,
                      (SELECT count(*) FROM documents WHERE statut = 'reclasser') AS reclassements,
                      (SELECT count(*) FROM revisions WHERE statut = 'en_attente') AS revisions,
                      (SELECT count(*) FROM campagnes WHERE statut = 'en_attente') AS campagnes""").fetchone()


# --- Documents personnels (sensible.py) --------------------------------------------------

def sensible_noter(kdrive_id: int, nom: str, chemin: str, categorie: str | None, raisons: list[str],
                   decision: str, pages: str | None = None) -> int:
    with connect() as c:
        return c.execute(
            """INSERT INTO sensibles (kdrive_id, nom, chemin, categorie, raisons, pages, decision)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (kdrive_id, nom, chemin, categorie, raisons, pages, decision)).fetchone()["id"]


def sensible_decision(kdrive_id: int) -> str | None:
    """Dernière décision connue pour ce fichier (« analyser » : tu as dit que ce n'était pas personnel)."""
    with connect() as c:
        r = c.execute("SELECT decision FROM sensibles WHERE kdrive_id = %s ORDER BY id DESC LIMIT 1",
                      (kdrive_id,)).fetchone()
    return r["decision"] if r else None


def sensibles(decision: str | None = None, jours: int = 30):
    with connect() as c:
        if decision:
            return c.execute("SELECT * FROM sensibles WHERE decision = %s ORDER BY id DESC", (decision,)).fetchall()
        return c.execute("SELECT * FROM sensibles WHERE decision IN ('corbeille', 'supprime') "
                         "AND le > now() - make_interval(days => %s) ORDER BY id DESC LIMIT 100", (jours,)).fetchall()


def sensible_choisir(ids: list[int], decision: str) -> list[dict]:
    """Doutes tranchés sur le site ; renvoie les lignes concernées."""
    with connect() as c:
        return c.execute("UPDATE sensibles SET decision = %s, le = now() WHERE id = ANY(%s) AND decision = 'doute' "
                         "RETURNING *", (decision, ids)).fetchall()


# --- Gros scans découpés (decoupage.py) -----------------------------------------------------

def decoupage(kdrive_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM decoupages WHERE kdrive_id = %s", (kdrive_id,)).fetchone()


def decoupage_maj(kdrive_id: int, **champs):
    """Crée ou complète l'état d'un découpage (reprise après une limite, une panne, un redémarrage)."""
    permis = {"nom", "chemin", "pages", "statut", "lecture", "coupes", "plan"}
    champs = {k: (Jsonb(v) if k in {"lecture", "coupes", "plan"} and v is not None else v)
              for k, v in champs.items() if k in permis}
    cols = ["kdrive_id", *champs]
    maj = ", ".join(f"{k} = EXCLUDED.{k}" for k in champs) + ", maj_le = now()"
    with connect() as c:
        c.execute(f"INSERT INTO decoupages ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                  f"ON CONFLICT (kdrive_id) DO UPDATE SET {maj}", (kdrive_id, *champs.values()))


def decoupages(limite: int = 10):
    with connect() as c:
        return c.execute("SELECT kdrive_id, nom, chemin, pages, statut, plan, coupes, maj_le FROM decoupages "
                         "ORDER BY maj_le DESC LIMIT %s", (limite,)).fetchall()


# --- Demander (chat) -----------------------------------------------------------

def cours_pour_question(requete: str, niveau: str | None = None, matiere: str | None = None, limite: int = 5,
                        avec_texte: bool = False):
    """Cours classés qui contiennent des mots de la question (« mot1 | mot2 »), les plus pertinents d'abord,
    avec un extrait où les mots sont entre ** (Markdown gras)."""
    if not requete.strip():
        return []
    texte = ", transcription" if avec_texte else ""
    with connect() as c:
        return c.execute(
            f"""SELECT id, nom, titre, niveau, matiere, type, resume{texte},
                       ts_headline('french', coalesce(transcription, ''), requete,
                                   'StartSel=**, StopSel=**, MaxFragments=2, MaxWords=35, MinWords=12, '
                                   'FragmentDelimiter=" … "') AS extrait
                FROM documents, to_tsquery('french', %(q)s) AS requete
                WHERE statut = 'classe' AND recherche @@ requete
                  AND (%(niveau)s::text IS NULL OR niveau = %(niveau)s)
                  AND (%(matiere)s::text IS NULL OR matiere = %(matiere)s)
                ORDER BY ts_rank(recherche, requete) DESC LIMIT %(limite)s""",
            {"q": requete, "niveau": niveau or None, "matiere": matiere or None, "limite": limite}).fetchall()


def creer_conversation(titre: str, niveau: str | None = None, matiere: str | None = None) -> int:
    with connect() as c:
        return c.execute("INSERT INTO conversations (titre, niveau, matiere) VALUES (%s, %s, %s) RETURNING id",
                         (titre, niveau or None, matiere or None)).fetchone()["id"]


def conversation(conv_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM conversations WHERE id = %s", (conv_id,)).fetchone()


def conversations(limite: int = 20):
    with connect() as c:
        return c.execute("SELECT k.*, count(m.id) AS nb FROM conversations k LEFT JOIN messages m "
                         "ON m.conversation_id = k.id GROUP BY k.id ORDER BY k.maj_le DESC LIMIT %s",
                         (limite,)).fetchall()


def supprimer_conversation(conv_id: int):
    with connect() as c:
        c.execute("DELETE FROM conversations WHERE id = %s", (conv_id,))


def ajouter_message(conv_id: int, role: str, contenu: str = "", statut: str = "pret", reperes=None, web=None,
                    modele: str | None = None) -> int:
    with connect() as c, c.transaction():
        c.execute("UPDATE conversations SET maj_le = now() WHERE id = %s", (conv_id,))
        return c.execute(
            "INSERT INTO messages (conversation_id, role, contenu, statut, reperes, sources_web, modele) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (conv_id, role, contenu, statut, Jsonb(reperes or {}), Jsonb(web or []), modele)).fetchone()["id"]


def message(msg_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM messages WHERE id = %s", (msg_id,)).fetchone()


def messages(conv_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM messages WHERE conversation_id = %s ORDER BY id", (conv_id,)).fetchall()


def maj_message(msg_id: int, statut: str, contenu: str = "", reperes=None, web=None, modele: str | None = None,
                erreur: str | None = None, role: str | None = None):
    """`role` : « local » quand l'IA, en pause, a été remplacée par la recherche dans les cours."""
    with connect() as c:
        c.execute("UPDATE messages SET statut = %s, contenu = %s, reperes = %s, sources_web = %s, modele = %s, "
                  "erreur = %s, role = coalesce(%s, role) WHERE id = %s",
                  (statut, contenu, Jsonb(reperes or {}), Jsonb(web or []), modele, erreur, role, msg_id))

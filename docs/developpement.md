# Développement

[← Retour au README](../README.md) · Schémas : [schemas.md](schemas.md)

## Lancer les tests

```bash
cd app
pip install -r requirements.txt pytest
pytest                      # ~540 tests, sans base ni kDrive (tout est simulé)
cd ../pronote && npm ci && npm test   # service Pronote
```

Les tests de lecture locale (OCR) se lancent si Tesseract est installé (`apt install tesseract-ocr tesseract-ocr-fra`).

Sur GitHub, ces deux suites tournent à chaque PR (`.github/workflows/tests.yml`), et Dependabot propose chaque mois
les mises à jour de dépendances (`.github/dependabot.yml`) : une PR verte peut être fusionnée, puis installée par
Réglages → Mise à jour.

## Organisation du code

| Fichier | Rôle |
| --- | --- |
| `app/claunote/main.py` | Site FastAPI : pages, formulaires, HTMX |
| `app/claunote/worker.py` | Boucle du worker : inbox, tri, analyses, rangement, révisions, Pronote, pause et arrêt |
| `app/claunote/agent.py` | Appels à Claude Code (`claude -p`), modèle par tâche, escalade, limites, arrêt d'un appel en cours |
| `app/claunote/local.py` | Modèle local : texte, OCR Tesseract, Ollama, appels à l'aide et vérification par Claude |
| `app/claunote/ressources.py`, `index.py` | Saturation de la VM ; passages et recherche par le sens (pgvector) |
| `app/claunote/importation.py`, `decoupage.py`, `sensible.py`, `ocr.py` | Tri d'un import, découpage des gros scans, garde-fou, lecture locale |
| `app/claunote/revision.py`, `question.py`, `pdf.py`, `anki.py` | Supports de révision, flashcards FSRS, chat, PDF, export Anki |
| `app/claunote/archivage.py` | Propositions d'archivage (révisions, flashcards, campagnes délaissées) |
| `app/claunote/progression.py`, `carte_mentale.py`, `formules.py` | Page Progression, cartes mentales (markmap), formules LaTeX (KaTeX, texte des PDF) |
| `app/claunote/lecture.py`, `lecture/` | Docling (Word, PowerPoint, Excel, PDF), service local pix2tex et FlashRank |
| `app/claunote/actualites.py` | Flux RSS (relevé, dédup), tri par sujets (IA ou mots-clés), regroupement d'une même information, L'essentiel du jour, notification du jour, alertes |
| `app/claunote/matieres.py`, `formations.py`, `rangement.py` | Matières en double, formation par année et référentiel officiel, règles de rangement, chemins et noms |
| `app/claunote/comptes.py`, `auth.py`, `cles.py` | Comptes, invitations, droits par fonctionnalité, sessions, Face ID (clés d'accès) |
| `app/claunote/modeles.py`, `coherence.py` | Modèle Claude par tâche (création = Opus 5.5), garde-fou hors sujet avec les anciennes fiches |
| `app/claunote/demande.py` | « Réviser en une phrase » : matière, support et date trouvés sans IA |
| `app/claunote/installation.py`, `stockage_local.py`, `disque.py` | Assistant d'installation (jetons chiffrés), stockage sur la VM, espace disque |
| `app/claunote/securite.py`, `notifications.py` | Journal de sécurité et alertes, file des notifications push |
| `app/claunote/pronote.py`, `pronote/` | Client Python et service Node (Pawnote) |
| `app/claunote/db.py` | Schéma PostgreSQL (créé et mis à jour au démarrage) et requêtes |
| `app/claunote/maj.py`, `infra/maj/` | Mise à jour depuis le site : le site dépose la demande, le service `maj` l'exécute |
| `app/claunote/appareils.py` | Accès par Internet : appareils approuvés par code |
| `infra/` | Caddy (HTTPS local), service de mise à jour et guide d'installation |

<details>
<summary><strong>Historique des étapes</strong></summary>

- [x] VM, accès kDrive par API, Docker Compose
- [x] Pipeline d'ingestion : transcription et classement par Claude Code
- [x] Site : bibliothèque, recherche, documents « à vérifier »
- [x] Agent de révision : fiches, quiz, plans, sources citées
- [x] Pronote : agenda, devoirs, notes, documents des profs, préparation en un clic
- [x] Flashcards (répétition espacée), export Anki, appli installable
- [x] Organiser : reclassement, actions de masse, espace « Hors cours »
- [x] Appli iPhone (HTTPS local, style Liquid Glass), notifications push
- [x] Imports de dossiers entiers, garde-fou des documents personnels, découpage des très gros scans
- [x] Campagnes multi-matières, épreuves blanches, PDF à partager, fichiers joints au contexte
- [x] Archivage proposé des révisions, flashcards et campagnes délaissées
- [x] « Une question » : chat avec l'IA ou sans IA
- [x] Actualités : flux RSS, sujets suivis triés par l'IA, notifications
- [x] Matières en double : détection, fusion, prévention
- [x] Analyse sans Claude sur CPU (Tesseract + Ollama), pause et arrêt des analyses, file d'analyse
- [x] Local d'abord : Claude n'aide que si besoin et vérifie ; saturation surveillée ; index par passages et par le sens ;
  code des TP lu comme un cours ; menus regroupés (Ajouter, Ranger, Activité, Réglages)

</details>


## Historique des étapes

- [x] VM, accès kDrive par API, Docker Compose
- [x] Pipeline d'ingestion : transcription et classement par Claude Code
- [x] Site : bibliothèque, recherche, documents « à vérifier »
- [x] Agent de révision : fiches, quiz, plans, sources citées
- [x] Pronote : agenda, devoirs, notes, documents des profs, préparation en un clic
- [x] Flashcards (répétition espacée), export Anki, appli installable
- [x] Appli iPhone (HTTPS local, style Liquid Glass), notifications push, Face ID
- [x] Imports de dossiers entiers, garde-fou des documents personnels, découpage des très gros scans
- [x] Campagnes multi-matières, épreuves blanches, PDF à partager, fichiers joints au contexte
- [x] « Une question » : chat avec l'IA ou sans IA
- [x] Analyse sans Claude sur CPU (Tesseract + Ollama), local d'abord, index par passages et par le sens
- [x] Accès par Internet (Cloudflare Tunnel, appareils approuvés), journal de sécurité
- [x] Fiches synthèse denses sur trois colonnes, modifiables par une demande, « Réviser en une phrase »
- [x] Plusieurs comptes, invitations, droits ; assistant d'installation ; stockage kDrive, VM ou les deux
- [x] Public ou privé par élément, page Partagés
- [x] Toute création de contenu par Opus 5.5, garde-fou hors sujet
- [x] Archivage proposé des révisions, flashcards et campagnes délaissées
- [x] Actualités : flux RSS, sujets suivis triés par l'IA, L'essentiel du jour, notification du jour au bon moment
- [x] Navigation simplifiée : Actualités · Cours · Réviser · Agenda · Plus, une seule page Réglages
- [x] Matières : formation par année, référentiel officiel, règles de rangement

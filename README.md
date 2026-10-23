# Claunote

Outil d'études perso : cours et TPs scannés (collège → BTS SIO) stockés sur kDrive,
indexés sur une VM Linux, consultables sur un site interne, avec un agent IA
pour préparer les évaluations (résumés, fiches, quiz) et une intégration Pronote.

Plan complet : https://claude.ai/artifact/TftXx2h7fafSkS1WjNLMKK

## Architecture

```
Papier ──scan 300 dpi──▶ kDrive Etudes/inbox
                              │ API REST kDrive (jeton)
                              ▼
┌──────────────── VM Linux (Docker Compose) ────────────────┐
│  worker (OCR, classement) ──▶ PostgreSQL + pgvector        │
│  agent IA (fiches, résumés) ─▶ Claude Code (forfait Pro)   │
│  site FastAPI + HTMX ◀── Pronote (Pawnote)                 │
└──────────────────────────▲─────────────────────────────────┘
                           │ VPN perso
                          Toi
```

## Phases

- [x] **Phase 0** : VM, accès kDrive par API, Docker Compose
- [x] Phase 1 : pipeline d'ingestion (transcription + classement par Claude Code)
- [x] Phase 2 : site MVP (bibliothèque, recherche, « à vérifier »)
- [x] Phase 3 : agent de révision (fiches, quiz, plans, sources citées)
- [x] Avant Pronote : dossiers par année, dépôt depuis le site, confirmation des doutes, nouveau look
- [x] Phase 4 : Pronote (agenda des évaluations, devoirs, notes, préparation en 1 clic)
- [x] Documents des profs Pronote (Contenu et ressources, pièces jointes du Travail à faire) → bibliothèque
- [x] Phase 5 : flashcards (révision espacée), export Anki, appli installable (PWA)
- [x] Connexion au site (identifiant + mot de passe, changeable sur le site)
- [x] Organiser : reclassement, actions de masse par date d'ajout, rangement IA, espace « Hors cours »
- [x] Ergonomie mobile : accueil « Aujourd'hui », onglets en bas, photo depuis le téléphone
- [x] Appli iPhone (HTTPS local, style Liquid Glass) et notifications push (voir infra/SETUP.md §13)

## Installation et utilisation

Voir [infra/SETUP.md](infra/SETUP.md).

## Tests

```bash
cd app && pip install -r requirements.txt pytest && pytest
```

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
- [ ] Phase 3 : agent de révision
- [ ] Phase 4 : Pronote
- [ ] Phase 5 : flashcards, Anki, PWA

## Installation et utilisation

Voir [infra/SETUP.md](infra/SETUP.md).

## Tests

```bash
cd app && pip install -r requirements.txt pytest && pytest
```

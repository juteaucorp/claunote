# Schémas

[← Retour au README](../README.md) · Installation : [installation.md](installation.md)

## Sommaire

- [Vue d'ensemble](#vue-densemble)
- [Le parcours d'un document](#le-parcours-dun-document)
- [Actualités : du flux RSS à la notification](#actualités--du-flux-rss-à-la-notification)
- [Qui fait le travail : local ou Claude](#qui-fait-le-travail--local-ou-claude)
- [Installation à la main](#installation-à-la-main)
- [Ouvrir le site : VPN ou Internet](#ouvrir-le-site--vpn-ou-internet)
- [Plusieurs comptes](#plusieurs-comptes)
- [Public ou privé](#public-ou-privé)
- [Mise à jour depuis le site](#mise-à-jour-depuis-le-site)

## Vue d'ensemble

```mermaid
flowchart LR
    T["📱 Téléphone / PC<br/>(VPN ou Cloudflare Tunnel)"] -->|HTTPS| S
    subgraph VM["VM Linux · Docker Compose"]
        S["Site<br/>FastAPI + HTMX"] --- DB[("PostgreSQL + pgvector<br/>passages, recherche par le sens")]
        W["Worker<br/>tri · OCR · analyse · rangement"] --- DB
        W --- P["Pronote<br/>(Pawnote)"]
        S --- P
        W --> O["Ollama + Tesseract<br/>modèle local CPU"]
        S --> O
    end
    W <-->|API REST| K[("kDrive<br/>Etudes/inbox → archives")]
    W -.->|aide, vérification, surplus| C["Claude Code<br/>forfait Claude Pro"]
    S -.->|aide, révisions| C
```

## Le parcours d'un document

```mermaid
flowchart TD
    A[Fichier dans kDrive/Etudes/inbox] --> B{Tri sans IA}
    B -->|code, doublon, système| X[Proposé à la suppression]
    B -->|document| G{Garde-fou<br/>RIB, identité, santé…}
    G -->|personnel| Y[Corbeille ou ton avis, jamais envoyé à l'IA]
    G -->|cours| S{VM saturée ou<br/>file trop longue ?}
    S -->|non| L[Modèle local sur la VM : texte ou OCR Tesseract<br/>+ classement par Ollama]
    S -->|oui, Claude disponible| C[Claude : transcription + classement]
    L -->|manuscrit illisible| C
    L -->|doute, ou 1 contrôle sur 10| V2[Claude vérifie et corrige<br/>texte seul, Haiku]
    L -->|sûr de lui| R
    V2 --> R
    C --> R[Rangé dans kDrive<br/>archives/année/matière/type + .md]
    R --> I[Découpé en passages et indexé<br/>par les mots et par le sens]
    I --> V[Chat, recherche, fiches, quiz,<br/>flashcards, épreuves blanches]
```

## Actualités : du flux RSS à la notification

```mermaid
flowchart TD
    F["Flux RSS que tu ajoutes<br/>+ sujets suivis (option Google Actualités)"] -->|relevé toutes les 30 min| D["Dédoublonnage par lien"]
    D --> H["Claude Haiku<br/>par lots de 40"]
    H -->|indisponible| M["Tri par mots-clés"]
    H --> T["Score, résumé en 1 phrase,<br/>alerte ?, même événement ?, notion de cours"]
    M --> T
    T --> P["« Pour toi »<br/>une carte par information, « Aussi chez … »"]
    T -->|notion trouvée dans tes cours| C["Lien « … · ton cours »"]
    P --> E["L'essentiel du jour<br/>Claude Opus, au plus toutes les 3 h"]
    T -->|alerte rare et grave| A["🔔 Notification immédiate<br/>une seule fois par événement"]
    E --> Q["🔔 Notification du jour<br/>à l'heure où tu lis d'habitude"]
```

- **Alerte** = rare et grave seulement (grosse fuite de données, faille critique exploitée, panne majeure).
- **Heure de la notification du jour** : apprise de tes ouvertures des Actualités sur 28 jours (7 h 30 par défaut),
  ou fixée à la main, ou coupée (*Sources et sujets → Réglages*).
- Rien ne sonne pendant tes heures de silence.
- **À chaque compte son fil** : ses sources, ses sujets, ses articles lus ou favoris, son « Essentiel » et ses
  alertes, sur ses appareils et selon ses réglages de notification. Un compte sans forfait Claude reprend le
  résumé et l'alerte que l'IA a donnés au même article chez l'admin, rangés dans ses sujets par mots-clés
  (pas d'« Essentiel » pour lui : la notification du jour compte ses articles).
- **Fil par défaut** : un compte qui n'a rien changé suit le fil choisi par l'admin (*Sources et sujets → Fil par
  défaut pour tout le monde*, « Mettre mon fil par défaut »), sinon la sélection de départ, alertes urgentes
  activées. Il change une source ou un sujet : son fil devient le sien ; « Revenir au fil par défaut » l'y remet.
- **Fil de l'admin par défaut, en permanence** (activé tant que l'admin ne le coupe pas) : chaque changement de son
  fil devient le fil par défaut. Un compte qui le suit reprend ses articles, son tri par l'IA et son « Essentiel » :
  ni relevé ni IA pour lui ; seules ses alertes (sur ses appareils), ses lectures et ses favoris sont à lui.
- **L'essentiel du jour pour les autres comptes** : avec le droit « L'essentiel du jour » (page Comptes). Même fil
  que l'admin : son point est repris, sans rappeler Claude ; fil différent : Claude Opus le fait sur le forfait de
  l'admin, même sans autonomie.

## Qui fait le travail : local ou Claude

```mermaid
flowchart LR
    subgraph Local["Sur la VM (gratuit, rien ne sort)"]
        L1["Lecture, OCR, classement"]
        L2["Chat « Une question »"]
    end
    subgraph Claude["Forfait Claude Pro"]
        H["Haiku : classement de texte, contrôles,<br/>tri des actualités"]
        S["Sonnet : scans manuscrits, découpage,<br/>« Une question »"]
        O["Opus 5.5 : fiches, quiz, flashcards, plans,<br/>épreuves blanches, référentiels, L'essentiel"]
    end
    L1 -->|doute, 1 contrôle sur 10, VM saturée| H
    L1 -->|manuscrit illisible| S
    L2 -->|doute| S
```

Toute **création de contenu** passe par **Opus 5.5**. S'il est à sa limite, la création attend sa reprise
(`CLAUDE_REPLI_CREATION=1` pour repasser sur Sonnet).

## Installation à la main

```mermaid
flowchart LR
    A["1 · VM + Docker"] --> B["2 · kDrive<br/>dossiers + jeton"]
    B --> C["3 · Claude<br/>claude setup-token"]
    C --> D["4 · .env"]
    D --> E["5 · docker compose up"]
    E --> F["6 · Ouvrir le site<br/>et déposer un scan"]
```

## Ouvrir le site : VPN ou Internet

```
 ┌─ Par ton VPN (par défaut) ─────────────────────────────────────────────┐
 │ Téléphone / PC ──VPN──► http://BIND_ADDR:8000   (ou https://CLAUNOTE_HOTE pour l'iPhone)
 └────────────────────────────────────────────────────────────────────────┘
 ┌─ Par Internet, sans VPN (facultatif) ──────────────────────────────────┐
 │ Appareil approuvé ──► Cloudflare (https://claunote.ton-domaine.fr) ──tunnel sortant──► VM
 │ Aucun port ouvert. Un appareil inconnu ne voit que la page « code d'appareil ».
 └────────────────────────────────────────────────────────────────────────┘
```

## Plusieurs comptes

```
 admin ──Réglages → Comptes → « Inviter »──► jeton (15 min, un seul usage)
                                               └─► /invitation : la personne choisit identifiant + mot de passe
                                                   et accepte les conditions d'utilisation
 chaque compte ──► ses propres cours, fiches, cartes, questions (la base le garantit : row-level security)
 admin ──« Tout voir »──► les données de tous les comptes (bandeau visible, noté dans Sécurité)

 Ranger : chacun range ce qu'il a déposé
   dépôt d'un invité ──► son tri d'import, ses « À vérifier », ses notifications « à ranger »
                     ──► l'IA classe avec SES matières, SA formation, SES règles de rangement
   admin (même en « Tout voir ») ──► sa file et ses pastilles ne comptent que ses dépôts ;
                                      Import, Matières et Déposer restent les siens ;
                                      les dépôts des autres : comptés sur À vérifier, ouverts dans Tous les documents
```

## Public ou privé

```
 chaque cours, matière, fiche, campagne ──► « Qui peut voir ? »
      🔒 Moi seul (par défaut)  │  🌐 Tous les comptes  │  Comme la matière (pour un cours)
 page Partagés ──► ce que les autres comptes ont rendu public, en lecture seule
 seul le propriétaire change ce choix ; « Tout voir » de l'admin n'en dépend pas
```

## Mise à jour depuis le site

```
 site ──demande──► service maj ──► git pull ──► docker compose build ──► up -d
  ▲                    │
  └── état + journal ──┘        (le site ne peut demander que 4 actions fixes, jamais une commande libre)
```

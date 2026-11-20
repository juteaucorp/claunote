# Installer et administrer Claunote

[← Retour au README](../README.md) · Guide pas à pas avec les tests : [infra/SETUP.md](../infra/SETUP.md) · Schémas : [schemas.md](schemas.md)

## Sommaire

- [Ce qu'il te faut](#ce-quil-te-faut)
- [Installation](#installation)
- [Le fichier .env](#le-fichier-env)
- [Ouvrir le site : VPN ou Internet](#ouvrir-le-site--vpn-ou-internet)
- [Plusieurs comptes](#plusieurs-comptes)
- [Mettre à jour](#mettre-à-jour)
- [Réglages avancés](#réglages-avancés)
- [Dépannage rapide](#dépannage-rapide)

## Ce qu'il te faut

| | Obligatoire ? | Pourquoi | Où le trouver |
| --- | --- | --- | --- |
| 🖥️ **Une VM Linux avec Docker** | **oui** | Tout tourne dedans (site, worker, base, modèles locaux) | Debian 12 / Ubuntu 24.04, **4 vCPU · 8 Go RAM · 40 Go SSD** conseillés (2 vCPU · 4 Go avec `OLLAMA_MODELE=qwen2.5:1.5b`). Aucun GPU. |
| 🤖 **Un abonnement Claude Pro (ou Max)** | **oui** | L'IA passe par Claude Code (`claude -p`) avec **le jeton de ton forfait**, pas par une clé API : **aucune facturation à l'usage** | [claude.ai](https://claude.ai), puis `claude setup-token` (voir l'étape 3) |
| ☁️ **Un kDrive Infomaniak** | **oui** | Tes scans y arrivent (`Etudes/inbox`) et y sont rangés (`Etudes/archives/…`) | [Jeton API](https://manager.infomaniak.com/v3/ng/accounts/token/list) avec le scope `drive` |
| 🔒 **Un moyen d'ouvrir le site** | **oui** | Le site n'est **jamais** exposé directement sur Internet | Ton VPN (WireGuard, OpenVPN…), ou Cloudflare Tunnel (voir [plus bas](#ouvrir-le-site--vpn-ou-internet)) |
| 🎓 **Un compte Pronote** | non | Agenda, devoirs, notes, documents des profs | Se branche depuis le site (page *Agenda*), QR code ou identifiants |
| 🌐 **Un domaine sur Cloudflare** | non | Seulement pour ouvrir le site **par Internet, sans VPN** | ~10 €/an, voir la [procédure Cloudflare](cloudflare-tunnel.md) |
| 🔑 **Un jeton GitHub** | si le dépôt est privé | Le bouton *Mise à jour* du site fait un `git pull` | [Jeton fine-grained](https://github.com/settings/personal-access-tokens/new), voir [Mettre à jour](#mettre-à-jour) |

## Installation

**Le plus simple : l'assistant du site.** Remplis seulement `POSTGRES_PASSWORD` et `BIND_ADDR` dans le `.env`,
lance `docker compose up -d --build`, ouvre le site et connecte-toi (`admin` / `claunote`) : un **assistant pas à
pas** s'ouvre (espace disque → ton compte admin → kDrive, avec « Tester » et « Créer les dossiers pour moi » →
jeton Claude, avec « Tester » → Cloudflare → Pronote). Les jetons sont **chiffrés** dans la base avec une clé gardée
à part (volume Docker `secrets`) et **jamais réaffichés**. Tout se modifie ensuite dans *Réglages → Installation* ;
un réglage fait sur le site passe avant celui du `.env`.

**Où sont les fichiers** (au choix dans l'assistant) :

| Choix | Scans et cours rangés | À prévoir |
| --- | --- | --- |
| **kDrive** | sur kDrive (Infomaniak) | un jeton API et deux dossiers (ou « Créer les dossiers pour moi ») |
| **Sur la VM** | dans le volume Docker `fichiers` (`Inbox/`, `Etudes/`) | de la place disque et **ta propre sauvegarde** ; `FICHIERS_HOTE=/chemin` dans `.env` pour un autre disque |
| **Les deux** | sur la VM, **copie de `Etudes/` sur kDrive** toutes les heures | les deux ; la copie ne supprime jamais rien sur kDrive |

Ce choix se fait **à l'installation** : une fois des documents rangés, le site refuse d'en changer (leurs
fichiers deviendraient introuvables). Sur la VM, un document supprimé va dans `.corbeille/`, vidée après 30 jours.

> Sauvegarde le volume `secrets` avec la base : sans la clé, les jetons enregistrés sur le site sont à recoller.

À la main, sans l'assistant :

Les 6 étapes en schéma : [schémas → installation](schemas.md#installation-à-la-main).

**1. La VM** (en tant qu'utilisateur du groupe `docker`, pas en `sudo`) :

```bash
sudo apt install -y docker.io docker-compose-v2 git curl
git clone https://github.com/juteaucorp/claunote && cd claunote
cp .env.example .env
```

**2. kDrive** : crée un dossier `Etudes` et un dossier `inbox` dedans, puis un
[jeton API](https://manager.infomaniak.com/v3/ng/accounts/token/list) avec le scope **`drive`**.
Les identifiants se lisent dans l'adresse de kDrive web :

```
…/drive/123456/files/7890
         └─────┘       └──┘
         KDRIVE_ID      ID du dossier ouvert (Etudes, puis inbox)
```

**3. Claude** : sur la VM (ou n'importe quel PC), une seule fois :

```bash
curl -fsSL https://claude.ai/install.sh | bash   # installe Claude Code
claude setup-token                               # connexion à ton compte claude.ai → affiche un jeton (valable 1 an)
```

**4. Le `.env`** : remplis au minimum les lignes du [tableau ci-dessous](#le-fichier-env).

**5. Lancer** :

```bash
docker compose up -d --build   # site, worker, base, modèles locaux, Pronote, HTTPS, service de mise à jour
curl localhost:8000/health     # {"ok":true,"kdrive":true}
```

Le premier lancement télécharge le modèle local (~2,3 Go) : compte 10 à 20 minutes.

**6. Ouvrir** `http://<IP-VPN-de-la-VM>:8000`, se connecter (`admin` / `claunote` par défaut, **à changer
tout de suite** sur la page *Compte*), puis déposer un premier scan dans *Ajouter* ou dans `kDrive/Etudes/inbox`.

Le guide détaillé, étape par étape, avec les tests et le dépannage : **[infra/SETUP.md](../infra/SETUP.md)**
(Pronote, appli iPhone, notifications, Face ID, modèles locaux…).

## Le fichier .env

Il reste **sur ta VM** (il est ignoré par git) : ne le partage jamais, il contient tes jetons.

| Variable | À remplir ? | Valeur |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | **oui** | Un mot de passe au hasard (`openssl rand -hex 16`) |
| `BIND_ADDR` | **oui** | L'IP de la VM **sur ton VPN** : le site n'écoute que là. `127.0.0.1` si tu passes uniquement par Cloudflare Tunnel |
| `KDRIVE_TOKEN` | **oui** (ou assistant du site) | Le jeton API Infomaniak (scope `drive`) |
| `KDRIVE_ID`, `KDRIVE_ETUDES_ID`, `KDRIVE_INBOX_ID` | **oui** (ou assistant du site) | Les nombres lus dans l'adresse de kDrive web (étape 2) |
| `CLAUDE_CODE_OAUTH_TOKEN` | **oui** (ou assistant du site) | Le jeton de `claude setup-token`. Une `ANTHROPIC_API_KEY` serait **ignorée** |
| `CLAUNOTE_UTILISATEUR`, `CLAUNOTE_MOT_DE_PASSE` | conseillé | Ton identifiant et ton mot de passe (changeable ensuite sur le site) |
| `GITHUB_TOKEN` | si dépôt privé | Jeton fine-grained en **lecture seule**, pour le bouton *Mise à jour* (voir [plus bas](#mettre-à-jour)) |
| `CLAUNOTE_HOTE`, `CLAUNOTE_PUSH_CONTACT` | pour l'iPhone | Adresse VPN de la VM et ton e-mail (`mailto:…`) : appli iPhone en HTTPS et notifications |
| `COMPOSE_PROFILES=tunnel`, `CLOUDFLARE_TUNNEL_TOKEN` | pour Internet | Active Cloudflare Tunnel ([procédure](cloudflare-tunnel.md)) |

Pronote ne se règle pas dans le `.env` : la connexion se fait sur la page *Agenda* du site, et **ton mot de
passe Pronote n'est jamais stocké** (seul un jeton de reconnexion l'est).

## Ouvrir le site : VPN ou Internet

Schéma des deux accès : [schémas → VPN ou Internet](schemas.md#ouvrir-le-site--vpn-ou-internet).

- **VPN** : mets l'IP de la VM sur le VPN dans `BIND_ADDR`, c'est tout. Claunote ne fournit pas de VPN :
  utilise le tien.
- **Cloudflare Tunnel** : il faut un domaine géré par Cloudflare (compte gratuit). La procédure complète
  (tunnel, route `HTTP web:8000`, règle « France seulement », approbation des appareils, dépannage) est ici :
  **[docs/cloudflare-tunnel.md](cloudflare-tunnel.md)**.

## Plusieurs comptes

Schéma des invitations et des droits : [schémas → plusieurs comptes](schemas.md#plusieurs-comptes).

- **Droits par fonctionnalité** : Bibliothèque, Déposer, Organiser, Réviser, Agenda Pronote, Actualités, et
  **« Forfait Claude de l'admin »**. Sans ce dernier, le compte ne consomme jamais ton abonnement : ses scans sont
  lus par les modèles locaux, et les fiches ou quiz (qui demandent Claude) lui sont refusés.
- L'admin désactive, déconnecte, limite dans le temps ou supprime un compte (avec ses données sur le site ; ses
  fichiers restent dans kDrive, sous `Etudes/comptes/<identifiant>/`). Un mot de passe oublié se refait avec un
  lien de 15 min : l'admin ne connaît jamais le mot de passe.
- Pages publiques **Conditions d'utilisation** (`/conditions`) et **Tes données** (`/tes-donnees`, RGPD) :
  acceptées à la création du compte et redemandées quand elles changent. Elles ne sont pas un conseil juridique.
- Installation d'avant les comptes : rien à faire. Ton identifiant et ton mot de passe deviennent le compte admin,
  tes données restent les tiennes, et tu n'es pas déconnecté.

## Mettre à jour

**Depuis le site** : *Plus → Réglages → Mise à jour → Tout mettre à jour*. Un badge prévient quand une
nouvelle version est sur GitHub (vérifié toutes les 6 h). Le service `maj` fait le `git pull`, reconstruit
**seulement ce qui a changé** et redémarre les services ; si la construction échoue, l'ancienne version continue
de tourner.

Schéma du service de mise à jour : [schémas → mise à jour](schemas.md#mise-à-jour-depuis-le-site).

**À la main**, depuis le dossier de Claunote (sans `sudo`) :

```bash
git pull
docker compose stop ollama lecture                      # libère ~4 Go le temps de construire
for s in web worker pronote lecture; do docker compose build $s || break; done
docker compose up -d
```

Une image à la fois, comme le fait le service `maj` : `docker compose up -d --build` construit **toutes les
images en même temps**, à côté d'Ollama ; PyTorch + apt + modèle chargé peuvent saturer 8 Go et faire échouer
une construction.

**Dépôt privé → `GITHUB_TOKEN`** : sans lui, la mise à jour affiche « GitHub refuse l'accès ».

1. [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
2. **Resource owner** : le propriétaire du dépôt (ici l'organisation **`juteaucorp`**, pas ton compte perso :
   un ancien jeton créé pour un autre propriétaire ne marche plus).
3. **Repository access** : *Only select repositories* → `claunote`.
4. **Permissions** : *Contents* → **Read-only** (rien d'autre).
5. Dans `.env` : `GITHUB_TOKEN=github_pat_…`. Il est relu à chaque action, pas besoin de redémarrer.

Si l'organisation demande une approbation des jetons, un propriétaire de l'organisation doit l'accepter
(*Settings → Personal access tokens → Pending requests*).

Tu as fait un fork ? Le service `maj` suit simplement le dépôt `origin` de ton clone.

## Réglages avancés

Tout le reste a une valeur par défaut qui marche. Chaque ligne est expliquée dans [.env.example](../.env.example),
et les réglages du quotidien se font aussi sur le site (*Activité*, *Réglages*).

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `KDRIVE_TOKEN`, `KDRIVE_ID`, `KDRIVE_ETUDES_ID`, `KDRIVE_INBOX_ID` | Accès à kDrive | à remplir |
| `CLAUDE_CODE_OAUTH_TOKEN` | Jeton du forfait Claude Pro (`claude setup-token`) | à remplir |
| `CLAUDE_MODELES`, `CLAUDE_MODELE_MAX` | Un modèle par tâche (Haiku, Sonnet, Opus), plafond | voir la page *État* |
| `CLAUDE_ID_OPUS` | Identifiant exact d'Opus, utilisé pour toute création de contenu | `claude-opus-5-5` |
| `CLAUDE_REPLI_CREATION` | `1` : si Opus est en limite, fiches/quiz faits par Sonnet au lieu d'attendre | `0` |
| `CLAUDE_PARALLELE` | Appels à Claude en même temps | `3` |
| `ANALYSE_LOCALE` | Qui fait le travail : `local` (local d'abord), `auto` (Claude d'abord), `toujours`, `jamais` (aussi sur *Activité*) | `local` |
| `OLLAMA_MODELE`, `OLLAMA_EMBED`, `LOCAL_PARALLELE` | Modèle local, modèle de recherche par le sens, analyses locales en même temps | `qwen2.5:3b`, `nomic-embed-text`, `1` |
| `LOCAL_CHARGE_MAX`, `LOCAL_MEMOIRE_MIN_MO`, `LOCAL_ATTENTE_MAX_MIN` | Seuils de saturation : au-delà, le surplus part chez Claude | `0.85`, `600`, `30` |
| `LOCAL_CONTROLE` | Part des analyses locales sûres que Claude contrôle quand même | `0.1` |
| `PRONOTE_INTERVALLE_H`, `PRONOTE_PREPARATION_AUTO` | Synchro Pronote, préparation automatique des contrôles | `3`, `1` |
| `CLAUNOTE_UTILISATEUR`, `CLAUNOTE_MOT_DE_PASSE` | Connexion au site (mot de passe changeable sur le site) | `admin` / `claunote` |
| `DECOUPAGE_PAGES` | Au-delà, un PDF est d'abord découpé en documents | `40` |
| `CPU_MOYENNE_MAX`, `CPU_FENETRE_MIN`, `OLLAMA_THREADS` | Budget du processeur sur la durée (pics permis, pas de VM à fond en permanence), cœurs d'Ollama | `0.6`, `15`, tous sauf un |
| `OLLAMA_GARDER`, `LECTURE_GARDER_MIN` | Mémoire : modèles déchargés après ce temps sans usage (et tout de suite si la mémoire de la VM est basse) | `5m`, `10` |
| `LECTURE_THREADS` | Cœurs du service lecture (Docling, pix2tex, FlashRank) par calcul | `2` |
| `FSRS_RETENTION` | Part des flashcards qu'on veut encore savoir le jour où elles reviennent (plus haut = plus de révisions) | `0.9` |

## Dépannage rapide

| Symptôme | À vérifier |
| --- | --- |
| `/health` répond `"kdrive":false` | `KDRIVE_TOKEN` (scope `drive`) et `KDRIVE_ID` ; test `curl` dans [SETUP.md §2](../infra/SETUP.md#2-kdrive-via-lapi-rest-infomaniak) |
| Rien ne s'analyse | Page *État du service* : jeton Claude expiré ou limite du forfait atteinte (l'IA reprend seule à l'heure annoncée) |
| Le site ne s'ouvre pas | `BIND_ADDR` = bonne IP VPN ? `docker compose ps` puis `docker compose logs -f web` |
| « GitHub refuse l'accès » dans *Mise à jour* | `GITHUB_TOKEN` absent, expiré, ou créé pour le mauvais propriétaire |
| Mot de passe du site oublié | `docker compose exec web python -m claunote.auth reinitialiser` puis `docker compose restart web` |
| Une construction échoue (`docker compose build`) | `docker compose build web 2>&1 \| tail -40`, `df -h /` (disque plein ?), `free -h` ; construire une image à la fois (voir *Mettre à jour*) ; *Réglages → Mise à jour → Libérer de la place* |
| Erreur Cloudflare 1033 / 502 | Voir le dépannage de [docs/cloudflare-tunnel.md](cloudflare-tunnel.md) |

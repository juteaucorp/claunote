# Phase 0 : installation sur la VM

Terminé quand : un PDF déposé dans `kDrive/Etudes/inbox` depuis ton téléphone
apparaît sur `http://<vm>:8000/inbox` (via ton VPN).

## 1. VM

### Ressources recommandées

| Ressource | Minimum (MVP) | Recommandé | Pourquoi |
| --- | --- | --- | --- |
| vCPU | 2 | **4** | l'OCR de l'imprimé (OCRmyPDF) tourne sur le CPU : plus de cœurs = gros lots traités plus vite |
| RAM | 4 Go | **8 Go** | Postgres + site + worker ~2 Go ; 8 Go si on fait les embeddings en local (phase 3) |
| Disque VM | 20 Go | **40 Go SSD** | OS + images Docker ~8 Go, base < 2 Go, cache des scans en cours ~10 Go |
| GPU | aucun | aucun | l'IA (transcription, fiches) passe par Claude Code avec ton forfait Pro |
| Réseau | accès Internet | — | vers kDrive et Claude ; aucun port entrant |

Les scans restent sur kDrive : la VM n'en garde qu'une **copie temporaire** le temps de les traiter.

Seul cas où il faudrait beaucoup plus : faire tourner un modèle IA **en local** au lieu de Claude Code
(16 à 32 Go de RAM + GPU 8 Go de VRAM). Déconseillé : plus lent, moins bon sur le manuscrit.

Les 8 Go de RAM recommandés servent aussi au modèle d'embeddings local (recherche par sens),
puisque ton forfait Pro ne fournit pas d'API d'embeddings.

### Installation

Debian 12 ou Ubuntu 24.04.

```bash
sudo apt install -y docker.io docker-compose-v2 git curl
sudo useradd -m -G docker claunote
```

## 2. kDrive via l'API REST Infomaniak

Pas de montage : le site et le worker parlent directement à l'API kDrive avec un **jeton**.

1. Crée le dossier `Etudes` dans kDrive, et `inbox` dedans.
2. Crée un **jeton API** sur [manager.infomaniak.com → Jetons API](https://manager.infomaniak.com/v3/ng/accounts/token/list)
   avec le scope **`drive`**. Copie-le tout de suite, il ne s'affiche qu'une fois.
3. Récupère les IDs dans les URL de l'appli web kDrive :
   - dans l'URL de kDrive web, le nombre juste après `/drive/` → `KDRIVE_ID` (ex. `.../drive/123456/files/...` → `123456`)
   - ouvre `Etudes`, puis `inbox` : le nombre après `/files/` donne `KDRIVE_ETUDES_ID` et `KDRIVE_INBOX_ID`.
4. Remplis ces 4 valeurs dans `.env`.

Test rapide depuis la VM :

```bash
source .env
curl -s -H "Authorization: Bearer $KDRIVE_TOKEN" \
  "https://api.infomaniak.com/2/drive/$KDRIVE_ID/files/$KDRIVE_INBOX_ID/files" | head -c 300
```

Tu dois voir `"result":"success"` et la liste de ton inbox.

Le jeton donne accès à tout ton kDrive : **ne le commite jamais** (`.env` est ignoré par git).

## 3. Application

```bash
cp .env.example .env   # remplir le mot de passe Postgres
docker compose up -d --build
curl localhost:8000/health   # {"ok":true,"kdrive":true}
curl localhost:8000/inbox    # liste les fichiers de ton inbox kDrive
```

## 4. Accès distant

Via ton VPN existant. Dans `.env`, mets `BIND_ADDR` = l'adresse IP de la VM sur le VPN :
le site n'écoutera que sur cette interface, jamais sur Internet.

```bash
docker compose up -d   # après avoir changé BIND_ADDR
```

Puis `http://<ip-vpn-de-la-vm>:8000` depuis ton PC ou téléphone connecté au VPN.

## 5. Claude Code (l'agent IA, avec ton forfait Pro)

L'agent utilise **Claude Code en mode non interactif** (`claude -p`), connecté à **ton forfait
Claude Pro**. Pas de clé API, pas de facturation en plus : ça compte dans les limites
d'usage de ton forfait, comme tes conversations sur claude.ai.

```bash
curl -fsSL https://claude.ai/install.sh | bash   # installe Claude Code
claude setup-token                               # ouvre la connexion claude.ai, affiche un jeton
```

Copie le jeton affiché dans `.env` (`CLAUDE_CODE_OAUTH_TOKEN=...`). Il est valable **1 an**.
Ne le partage pas et ne le commite jamais (`.env` est ignoré par git).

Test :

```bash
export $(grep CLAUDE_CODE_OAUTH_TOKEN .env) && claude -p "Réponds juste : ok"
```

**Limites du forfait Pro** : le worker traite les scans **par petits lots** et se met en pause
quand la limite d'usage est atteinte, puis reprend tout seul. La numérisation de tout ton
stock prendra donc plusieurs jours en tâche de fond, sans rien te coûter de plus.

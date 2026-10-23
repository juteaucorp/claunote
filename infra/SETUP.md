# Phase 0 : installation sur la VM

Terminé quand : un PDF déposé dans `kDrive/Etudes/inbox` depuis ton téléphone
apparaît sur `http://<vm>:8000/inbox` (via ton VPN).

## 1. VM

### Ressources recommandées

| Ressource | Minimum (MVP) | Recommandé | Pourquoi |
| --- | --- | --- | --- |
| vCPU | 2 | **4** | l'OCR de l'imprimé (OCRmyPDF) tourne sur le CPU : plus de cœurs = gros lots traités plus vite |
| RAM | 4 Go | **8 Go** | Postgres + site + worker ~2 Go ; 8 Go si on fait les embeddings en local (phase 3) |
| Disque VM | 20 Go | **40 Go SSD** | OS + images Docker ~8 Go, base < 2 Go, cache rclone 10 Go |
| GPU | aucun | aucun | l'IA (transcription, fiches) passe par Claude Code avec ton forfait Pro |
| Réseau | accès Internet | — | vers kDrive et Claude ; aucun port entrant |

Les scans eux-mêmes ne prennent **pas de place sur la VM** : ils restent sur kDrive.

Seul cas où il faudrait beaucoup plus : faire tourner un modèle IA **en local** au lieu de Claude Code
(16 à 32 Go de RAM + GPU 8 Go de VRAM). Déconseillé : plus lent, moins bon sur le manuscrit.

Les 8 Go de RAM recommandés servent aussi au modèle d'embeddings local (recherche par sens),
puisque ton forfait Pro ne fournit pas d'API d'embeddings.

### Installation

Debian 12 ou Ubuntu 24.04.

```bash
sudo apt install -y docker.io docker-compose-v2 rclone fuse3 git
sudo useradd -m -G docker claunote
echo user_allow_other | sudo tee -a /etc/fuse.conf
```

## 2. kDrive en WebDAV

1. Infomaniak Manager → créer un **mot de passe d'application** (pas ton mot de passe principal).
2. Repérer l'ID de ton kDrive (dans l'URL du kDrive web).
3. Créer le dossier `Etudes/inbox` dans kDrive.
4. Configurer rclone (en tant que `claunote`) :

```bash
rclone config create kdrive webdav \
  url https://<ID_KDRIVE>.connect.kdrive.infomaniak.com \
  vendor other user <ton-email-infomaniak> \
  pass "$(rclone obscure '<mot-de-passe-application>')"
rclone lsd kdrive:            # doit lister tes dossiers
```

5. Service systemd :

```bash
sudo cp infra/rclone-kdrive.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rclone-kdrive
ls /mnt/kdrive/Etudes/inbox
```

## 3. Application

```bash
cp .env.example .env   # remplir le mot de passe Postgres
docker compose up -d --build
curl localhost:8000/health   # {"ok":true,"kdrive_monte":true}
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

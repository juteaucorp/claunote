# Phase 0 : installation sur la VM

Terminé quand : un PDF déposé dans `kDrive/Etudes/inbox` depuis ton téléphone
apparaît sur `http://<vm>:8000/inbox` (via Tailscale).

## 1. VM

Debian 12 ou Ubuntu 24.04, 2 vCPU, 4 Go RAM, 20 Go disque suffisent pour démarrer
(l'OCR lourd passe par l'API, pas en local).

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

```bash
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
```

Puis `http://<nom-vm-tailscale>:8000` depuis ton PC ou téléphone (Tailscale installé).
Pour l'instant le port n'écoute que sur 127.0.0.1 : utiliser `tailscale serve 8000`
pour l'exposer sur le réseau Tailscale uniquement.

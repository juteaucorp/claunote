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

Claude Code est **installé automatiquement dans l'image Docker** du worker. Sur la VM, tu n'en as
besoin qu'une fois, pour générer le jeton :

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

## 6. Utilisation (scan et classement)

1. Dépose un ou plusieurs PDF dans `Etudes/inbox` sur kDrive (**un document = un PDF** : un cours,
   un TP, un contrôle).
2. Clique sur **« Scanner maintenant »** sur le site (ou attends 1 minute).
3. Suis l'analyse : `docker compose logs -f worker`.
4. Le scan est déplacé dans `Etudes/archives/<niveau>/<matière>/<type>/`, renommé
   (`2025-03-12_les-vlan.pdf`), avec sa transcription `.md` à côté. S'il n'est pas sûr du
   classement, il part dans `Etudes/a-verifier/` et apparaît sur la page **À vérifier**.

### Déposer un dossier complet

Tu peux déposer **un dossier entier** dans `Etudes/inbox` (ou via la page **Déposer** du site), y
compris avec des sous-dossiers. **Nomme-le avec ton année d'étude** : `Terminale`, `3e`,
`BTS SIO 1`, `1ère année BTS`, `Seconde 2021`...

- Le nom du dossier **impose le niveau** (le plus profond qui en contient un l'emporte).
- Si le contenu ne colle pas avec le dossier (ex. dossier `BTS1` mais cours de Terminale), le
  fichier part dans **À vérifier** avec l'explication ; tu confirmes en un clic.
- Idem quand l'agent n'est pas sûr (confiance < 70 %) : le badge orange **À vérifier** du menu
  compte les documents en attente.
- Les sous-dossiers vides de l'inbox sont envoyés à la corbeille kDrive après 10 minutes.

### Déposer depuis le site

Page **Déposer** : glisse des fichiers ou un dossier complet (ou boutons « Choisir des fichiers »
/ « Choisir un dossier »). Formats acceptés : PDF, JPG, PNG, HEIC, WEBP. Le menu « Année d'étude »
est facultatif : il range le dépôt dans un dossier à ce nom. Les fichiers partent dans
`Etudes/inbox` et un scan est lancé tout de suite.

Après une mise à jour du code : `git pull && docker compose up -d --build`.

## 7. Réviser (phase 3)

Page **Réviser** du site : choisis une matière, un sujet (facultatif) et le type de support.

| Support | Ce que tu obtiens |
| --- | --- |
| **Fiche de révision** | l'essentiel, définitions, formules/commandes, méthodes, pièges, 3 exercices corrigés |
| **Quiz** | 10 QCM, réponse et explication cachées sous chaque question |
| **Plan de révision** | un programme jour par jour jusqu'à la date de l'évaluation |

- L'agent n'utilise **que tes cours** : chaque point renvoie au document source (`[D1]`, cliquable).
- Le sujet sert de recherche dans la matière ; s'il ne trouve rien, toute la matière est utilisée.
- Depuis la page d'un document : boutons **Fiche** et **Quiz** sur ce seul document.
- Chaque support est aussi copié dans `Etudes/fiches/` sur kDrive (en Markdown).
- Les révisions passent **avant** les scans dans la file du worker.

## 8. Pronote (phase 4)

Page **Agenda** du site. Un petit service `pronote` (Node + [Pawnote-LTS](https://github.com/BlocksHub/Pawnote-LTS), fork maintenu de Pawnote compatible Pronote 2026)
tourne à côté du site, **sans port ouvert** : seuls le site et le worker lui parlent.

```
Pronote ──(Pawnote, jeton)──► service pronote ──► worker (toutes les 3 h) ──► PostgreSQL ──► page Agenda
                                                                              └─► « Préparer » ─► fiche / plan
```

**Connexion (une seule fois)**

1. Sur Pronote (ordinateur), clique sur l'icône **QR code** (« connexion à l'application mobile »), choisis un
   code à 4 chiffres : le QR code s'affiche.
2. Sur Claunote, page **Agenda** : colle une capture d'écran du QR code (Ctrl+V) ou choisis l'image,
   puis entre le code à 4 chiffres. Le QR code est lu **dans ton navigateur**.
3. Sans ENT, tu peux aussi utiliser l'adresse Pronote + identifiant + mot de passe.

**Le mot de passe n'est jamais stocké** : seul le jeton de reconnexion de Pronote est gardé dans le
volume Docker `pronote` (fichier lisible par le seul service), et il change à chaque synchro. Si Pronote
le refuse un jour (changement de mot de passe, fin d'année), la page Agenda te demande de te reconnecter.

**Ce qui est récupéré** (4 semaines à venir) :

| Pronote | Sur Claunote |
| --- | --- |
| Cours marqués « devoir surveillé / évaluation » dans l'emploi du temps | **Évaluations à venir** |
| Travail à faire qui annonce un DS, contrôle, interro, examen, partiel, CCF… | **Évaluations à venir** |
| Autre travail à faire (2 semaines) | **Devoirs** |
| Notes de la période en cours | **Dernières notes** |

**Préparer une évaluation** : bouton **Préparer**. La matière Pronote est reliée à tes cours classés
(« CULTURE ECONOMIQUE, JURIDIQUE ET MANAGERIALE » → CEJM, « SISR - RESEAUX » → Réseaux…), au niveau le
plus récent. À 3 jours ou plus : un **plan de révision** ; plus près : une **fiche**. Si aucun cours ne
correspond, le formulaire Réviser s'ouvre prérempli.

**Documents des profs** : à chaque synchro, Claunote récupère les fichiers déposés dans
**Contenu et ressources** (depuis la rentrée) et les **pièces jointes du Travail à faire**.

```
Pronote ──► service pronote ──► kDrive : Inbox/<classe>/Pronote/<matière>/ ──► analyse + classement ──► Bibliothèque
```

- Chaque fichier n'est téléchargé **qu'une fois** (5 lots de 40 Mo max par synchro, 25 Mo max par fichier ;
  un échec est retenté 3 fois).
- PDF et images sont lus comme tes scans ; **Word, PowerPoint, Excel, LibreOffice** : le texte est extrait
  (un `.txt` est posé à côté).
- Les liens web déposés par les profs sont seulement listés.
- Suivi sur la page Agenda, tableau **Documents des profs** (classé / à vérifier / en analyse…).
- ⚠️ Le premier import peut contenir beaucoup de fichiers : l'analyse consomme ton **forfait Claude Pro**
  (le worker les traite petit à petit).

Réglages dans `.env` :

- `PRONOTE_INTERVALLE_H=3` : fréquence de la synchro.
- `PRONOTE_PREPARATION_AUTO=1` : prépare **tout seul** chaque évaluation à moins de 10 jours (consomme
  ton forfait Claude Pro). `0` par défaut : seulement quand tu cliques.

Logs : `docker compose logs -f pronote worker`.


## 9. Connexion au site

Le site demande un identifiant et un mot de passe. **Par défaut : `admin` / `claunote`** — à changer dans
le `.env` :

```
CLAUNOTE_UTILISATEUR=admin
CLAUNOTE_MOT_DE_PASSE=mon-nouveau-mot-de-passe
```

puis `docker compose up -d`. Pour ne pas l'écrire en clair, génère un hash et colle-le à la place :

```bash
docker compose run --rm web python -m claunote.auth   # affiche scrypt:…:…
```

- Mot de passe haché en **scrypt**, cookie de session **signé** (HMAC), `HttpOnly`, `SameSite=Lax`,
  `Secure` dès que le site passe en HTTPS. Une connexion dure **30 jours**.
- Changer le mot de passe **déconnecte tous les appareils**.
- 5 essais ratés depuis une même adresse → blocage **5 minutes**.
- Bouton **⏻** en haut à droite pour se déconnecter.

## 10. Flashcards, Anki et appli mobile (phase 5)

```
Réviser ─► « Flashcards » ─► Claude tire ~20 cartes de tes cours ─► page Cartes
                                                                   ├─► révision espacée sur le site
                                                                   └─► export .apkg ─► Anki / AnkiDroid
```

- **Créer** : page **Réviser**, choisis la matière (et un sujet), coche **Flashcards**.
- **Réviser** : page **Cartes** (le badge donne le nombre de cartes du jour). Tu retournes la carte
  (espace), puis tu notes **À revoir / Difficile / Bien / Facile** (touches 1 à 4). Algorithme SM-2,
  comme Anki : une carte sue revient de plus en plus tard, une carte ratée revient tout de suite.
- **Anki** : bouton **Anki** sur un paquet, ou **Tout exporter**. Le fichier `.apkg` s'ouvre dans Anki
  (ordinateur) ou AnkiDroid. Réimporter **met à jour** les cartes sans doublons. Les paquets sont rangés
  sous `Claunote::<sujet>`. La progression Anki et celle du site sont séparées.

**Appli sur le téléphone (PWA)** : Claunote peut s'installer comme une appli (icône papillon, plein
écran, pages déjà ouvertes lisibles hors ligne).

- ⚠️ Les navigateurs n'autorisent l'installation qu'en **HTTPS**. En `http://IP-VPN:8000`, tu peux
  seulement « Ajouter à l'écran d'accueil » (raccourci, sans hors ligne).
- Pour la vraie appli : mets un reverse proxy HTTPS devant le site (Caddy, Nginx Proxy Manager,
  Traefik…) avec un certificat (par ex. Let's Encrypt en DNS-01 sur un sous-domaine qui pointe vers
  l'IP VPN). Le proxy doit envoyer `X-Forwarded-Proto: https` pour que le cookie soit `Secure`.
- Puis sur Android (Chrome) : menu **⋮ → Installer l'application** ; sur iPhone (Safari) :
  **Partager → Sur l'écran d'accueil**.

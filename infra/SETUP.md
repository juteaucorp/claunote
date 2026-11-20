# Guide d'installation détaillé

Résumé et prérequis : [docs/installation.md](../docs/installation.md#ce-quil-te-faut). Ce guide reprend chaque étape en détail.

Premier objectif atteint quand : un PDF déposé dans `kDrive/Etudes/inbox` depuis ton téléphone
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

Sans VPN, depuis tes appareils seulement : Cloudflare Tunnel, voir la section 17.

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

**Un modèle par tâche** (page **État du service** du site) :

| Tâche | Modèle | Si l'agent doute |
|---|---|---|
| Noms de dossiers d'un import, reclassement, Word/PowerPoint, PDF numérique | Haiku (rapide) | Sonnet, puis Opus |
| Scan manuscrit, fiche, quiz, flashcards, plan, programme | Sonnet | Opus |
| Épreuve blanche (sujet + corrigé) | Opus | – |

Un classement peu sûr (confiance < 70 %) ou une réponse inutilisable est **refait par le modèle
au-dessus** avant de finir dans « À vérifier » : moins de tri à faire à la main. Si ton forfait
refuse Opus, ou si sa limite propre est atteinte, Sonnet le remplace tout seul.
Réglages : `CLAUDE_MODELES=examen=sonnet` (une tâche), `CLAUDE_MODELE_MAX=sonnet` (jamais Opus).
L'ancienne ligne `CLAUDE_MODEL` ne sert plus.

**Vitesse d'analyse** :
- **3 documents analysés en même temps** (`CLAUDE_PARALLELE=3`, de 1 à 6) ; un 4e se télécharge ou se
  range dans kDrive pendant ce temps. Le forfait compte les jetons, pas les appels : même quota, plus vite.
- **Word, PowerPoint, PDF numérique** (export Word, supports des profs) : le texte est extrait
  directement et devient la transcription, exacte ; Haiku ne fait que classer. Un PDF scanné ou
  photographié (même avec une couche OCR) ou plein d'images reste lu par Claude.
- **Scan de plus de 12 pages** : découpé en morceaux de 8 pages lus en parallèle, puis classé sur
  le texte. Chaque morceau fini est gardé : après une limite ou un redémarrage, seuls les morceaux
  manquants sont refaits.
- Le journal du worker donne pour chaque document le temps de téléchargement, d'IA et de rangement.

**Limites du forfait Pro** : quand la limite d'usage est atteinte, le worker lit **l'heure de
reprise** annoncée par Claude Code, met **seulement l'IA** en pause jusque-là (Pronote, tri,
rangement et notifications continuent) et repart tout seul. Un bandeau sur le site affiche
« l'IA reprend vers 19 h 00 ». La numérisation de tout ton stock prendra plusieurs jours en tâche
de fond, sans rien te coûter de plus.

**Pannes et redémarrages** : rien n'est perdu.

- Claude ou kDrive ne répond pas : nouvel essai automatique (1, 2, 4… min), le document reste en
  file et **n'est pas mis en erreur**. Un fichier qui fait planter Claude 3 fois de suite (alors que
  le reste passe) part dans « À vérifier » avec l'explication.
- L'analyse est **enregistrée avant** le rangement dans kDrive : si le worker s'arrête au milieu,
  il finit le rangement au démarrage suivant, sans rappeler Claude.
- `docker compose up -d --build` : le worker **termine le document en cours** (2 min au plus)
  avant de s'arrêter, et reprend la file au démarrage, même après plusieurs jours éteint.
- Détails et derniers appels à Claude : page **État du service** (menu Plus, ou Compte).

## 6. Utilisation (scan et classement)

1. Dépose un ou plusieurs PDF dans `Etudes/inbox` sur kDrive (**un document = un PDF** : un cours,
   un TP, un contrôle).
2. Clique sur **« Scanner maintenant »** sur le site (ou attends : 1 minute, jusqu'à 5 si rien n'est
   arrivé dans l'inbox depuis une demi-heure).
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

Mise à jour du code : *Plus → Réglages → Mise à jour* sur le site (section 15), ou à la main
`git pull && docker compose up -d --build`.

### Documents personnels oubliés (RIB, carte d'identité…)

Avant toute analyse, chaque fichier est vérifié **sur la VM, sans IA** : nom, dossier, texte du PDF
ou OCR local (Tesseract). Ce qui est repéré **n'est jamais envoyé à Claude** :

- **sûr** (IBAN valide, bande MRZ d'une pièce d'identité, n° de sécu avec sa clé, carte bancaire,
  fichier nommé `RIB`, `passeport`, `avis d'imposition`, `fiche de paie`, `mots de passe`…) :
  **corbeille kDrive tout de suite** (récupérable 30 jours), notification, liste sur la page **Import** ;
- **doute** (une expression comme « avis d'imposition » en en-tête, un dossier `Banque`…) : ni
  analysé ni supprimé, tu choisis sur **Import** : *Supprimer* ou *Ce sont des cours : analyser*.

Les documents des profs (Pronote) ne passent pas par ce contrôle.

### Très gros scan (une année entière, toutes matières)

Un PDF de plus de `DECOUPAGE_PAGES` pages (40 par défaut) est **découpé automatiquement** :
lecture locale de chaque page (pages blanches, en-têtes « Chapitre », « TP n° »…, pages
personnelles retirées), puis Claude repère les débuts de documents sur de petites images, par
paquets de 24 pages. Chaque document repart dans l'inbox et est analysé normalement. L'original
va dans `Etudes/import/scans-decoupes/` (ou à la corbeille s'il contenait des pages personnelles).
Compter ~1 s par page pour la lecture locale. Pour un fichier de plus de 1 Go, copie-le plutôt
directement dans l'inbox avec l'appli kDrive.

## 7. Réviser (phase 3)

Page **Réviser** du site : trois boutons en haut, **Une révision**, **Une question** et **Une campagne**, puis ce qui
est au programme et l'historique **Déjà fait** (filtres par type, matière, recherche).

| Support | Ce que tu obtiens |
| --- | --- |
| **Fiche de révision** | l'essentiel, définitions, formules/commandes, méthodes, pièges, 3 exercices corrigés |
| **Quiz** | 10 QCM à faire sur le site : bouton **Corriger**, score gardé dans l'historique |
| **Flashcards** | 20 cartes en répétition espacée (onglet Cartes), export Anki |
| **Épreuve blanche** | un sujet type examen avec barème, et son corrigé à part |
| **Plan de révision** | un programme jour par jour jusqu'à la date de l'évaluation |

- **Tes cours d'abord** : chaque point renvoie au document source (`[D1]`, cliquable).
- **Web automatique** : l'agent cherche sur le web seulement s'il le juge utile (notion incomplète,
  point à confirmer, erreur probable, format officiel d'une épreuve). Chaque apport est marqué `[W1]`
  avec le lien de la page, et la page affiche « 🌐 Complété avec le web ». Un support qui cherche sur
  le web prend un peu plus de temps et de forfait.
- Tu peux **cocher les cours** à utiliser, et donner un **contexte** (consignes du prof, points faibles…).
  Sans cours cochés, le sujet sert de recherche dans la matière ; s'il ne trouve rien, toute la matière est utilisée.
- **Campagne** : plusieurs matières × plusieurs supports d’un coup (sans limite de nombre, générés l’un après l’autre) + un **programme
  jour par jour** sur la période choisie, qui tient compte de ton contexte et des évaluations Pronote.
  Les tâches se cochent sur la page de la campagne ; celles du jour apparaissent sur Réviser et
  dans le rappel quotidien. Une seule notification quand tout est prêt.
- **PDF** : chaque support (et le livret complet d'une campagne) s'ouvre en PDF, avec ou sans corrigé.
  **Partager** ouvre la feuille de partage de l'iPhone (AirDrop, Messages, Fichiers…).
- Chaque support est aussi déposé en PDF dans `Etudes/fiches/<matière ou campagne>/` sur kDrive.
- Les révisions passent **avant** les scans dans la file du worker ; avec le forfait Pro elles sont
  générées l'une après l'autre (2 à 3 min chacune), et reprennent seules après une limite d'usage.

### Une question (chat)

**Réviser → Une question** (ou **Poser une question** sur la page d'un cours) : un chat pour une info
rapide, une notion à réexpliquer, une commande oubliée. Tu choisis dans quels cours chercher (tous, ou une
matière d'une année) et qui répond :

| Mode | Comment | Quand |
| --- | --- | --- |
| **IA (Claude)** | les cours qui contiennent les mots de ta question (6 au plus) + la discussion, réponse courte qui cite `[D1]`, le web si utile `[W1]` | explication, reformulation, question de suivi |
| **Rapide, sans IA** | recherche plein texte dans PostgreSQL : les passages de tes cours où apparaissent tes mots, surlignés | instantané, gratuit, marche même pendant une pause de Claude |

- La réponse IA est faite par le site lui-même (pas par le worker, qui peut être occupé par un gros scan) :
  elle arrive en 10 à 30 s et s'affiche toute seule. Modèle : Sonnet (tâche `question`, voir `CLAUDE_MODELES`).
- Si Claude est en pause (limite du forfait) ou ne répond pas, la réponse vient automatiquement des cours, sans IA.
- Sous une réponse sans IA, **Demander à l'IA** repose la même question à Claude.
- Les conversations sont gardées (liste **Questions précédentes**), supprimables une par une.

### Matières en double

L'IA de classement donne parfois deux noms à la même matière la même année (« Maths » et « Mathématiques »,
« Réseau » et « Réseaux », « SLAM » et « Développement »). Page **Plus → Ranger → Matières** (un rappel
s'affiche aussi sur **Cours** quand il y en a) :

- **Sans IA** : noms rapprochés par accents/pluriels/ponctuation, abréviations connues (Maths, HG, LV1, CEJM…),
  sigles, fautes de frappe, nom contenu dans l'autre. « Évident » ou « À confirmer ». Nom proposé : le plus utilisé.
- **Demander à l'IA d'en chercher d'autres** : un appel court (Haiku) qui voit les noms et quelques titres de cours.
- **Fusionner** : tu choisis le nom gardé ; documents, révisions et conversations sont renommés, les fichiers
  déplacés dans `archives/<année>/<matière>/` par le worker. **Fusionner à la main** : n'importe quelles matières d'une année.
- **Laisser tel quel** : ce sont vraiment deux matières ; la proposition ne revient plus, et les prochains
  scans gardent les deux noms. *Les reproposer* (en bas de la liste) annule ces choix.
- **Plus de nouveaux doublons** : l'agent reçoit la liste des matières existantes, et un nom évidemment
  identique à une matière existante (« Maths » quand « Mathématiques » existe) est remplacé avant rangement.
- Les anciens dossiers kDrive vidés par une fusion restent (vides) : supprime-les à la main si tu veux.

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
- `PRONOTE_PREPARATION_AUTO=1` (conseillé) : prépare **tout seul** chaque évaluation à moins de 10 jours
  (consomme ton forfait Claude Pro, mais ta fiche est prête avant que tu y penses). `0` : seulement quand
  tu cliques.

Logs : `docker compose logs -f pronote worker`.


## 9. Connexion au site

Le site demande un identifiant et un mot de passe. **Par défaut : `admin` / `claunote`** — à changer sur la
page **Compte** (⚙), ou dans le `.env` :

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
- **Changer le mot de passe depuis le site** : bouton **⚙** (page Compte). Il est gardé haché en base et
  passe avant celui du `.env`. Mot de passe oublié :
  `docker compose exec web python -m claunote.auth reinitialiser` puis `docker compose restart web`.
- Changer le mot de passe **déconnecte les autres appareils**.
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
  (espace), puis tu notes **À revoir / Difficile / Bien / Facile** (touches 1 à 4). Algorithme **FSRS**
  (celui d'Anki depuis 2023) : une carte sue revient de plus en plus tard, une carte ratée revient dans
  quelques minutes. Sous chaque bouton : quand la carte reviendra. Les cartes suivies avec l'ancien SM-2
  gardent leur rythme. `FSRS_RETENTION` (défaut `0.9`) : part des cartes qu'on veut encore savoir au
  moment où elles reviennent.
- **Progression** (*Réviser → Ta progression*) : série de jours de travail, flashcards par jour, mémoire
  estimée, cartes sues par matière, réussite aux cartes et aux quiz, moyennes et tendance Pronote.
- **Carte mentale** : bouton sur une fiche, une épreuve ou un plan, lien « carte mentale » sur un cours.
  Les titres et les listes deviennent des branches ; touche une branche pour la plier.
- **Anki** : bouton **Anki** sur un paquet, ou **Tout exporter**. Le fichier `.apkg` s'ouvre dans Anki
  (ordinateur) ou AnkiDroid. Réimporter **met à jour** les cartes sans doublons. Les paquets sont rangés
  sous `Claunote::<sujet>`. La progression Anki et celle du site sont séparées.

**Appli sur le téléphone (PWA)** : Claunote peut s'installer comme une appli (icône papillon, plein
écran, pages déjà ouvertes lisibles hors ligne).

- ⚠️ Les navigateurs n'autorisent l'installation qu'en **HTTPS**. En `http://IP-VPN:8000`, tu peux
  seulement « Ajouter à l'écran d'accueil » (raccourci, sans hors ligne).
- Le plus simple : le service `caddy` fourni (section 13). Sinon, mets ton propre reverse proxy HTTPS devant le site (Caddy, Nginx Proxy Manager,
  Traefik…) avec un certificat (par ex. Let's Encrypt en DNS-01 sur un sous-domaine qui pointe vers
  l'IP VPN). Le proxy doit envoyer `X-Forwarded-Proto: https` pour que le cookie soit `Secure`.
- Puis sur Android (Chrome) : menu **⋮ → Installer l'application** ; sur iPhone (Safari) :
  **Partager → Sur l'écran d'accueil**.

## 11. Organiser : reclasser, actions de masse, hors cours

```
Organiser ─► filtres (ajouté du … au …, année, matière, type, état, dossier)
          ─► coche ─► action : année / matière / type / Ranger par l'IA / hors cours / remettre en cours
                              └─► le worker déplace les fichiers dans kDrive (quelques secondes)
```

- **Un document** : sur sa page, **Modifier le classement** (année, matière, type) ou **Mettre hors cours**.
- **En masse** (page **Organiser**) : par exemple « tout ce que j'ai ajouté aujourd'hui entre 14 h et 16 h » →
  tout cocher → **Changer l'année : BTS2** → puis **Ranger par l'IA** : Claude relit la **transcription**
  (pas le scan, donc rapide et peu coûteux), **garde l'année choisie** et trouve matière + type en
  réutilisant tes matières existantes. S'il hésite, le document part dans **À vérifier**.
- **Hors cours** : bulletins, convocations, attestations, emplois du temps, pages vides… L'IA les repère
  dès l'analyse des nouveaux scans. Ils vont dans `Etudes/hors-cours/` sur kDrive, sont listés sur la page
  **Hors cours** (lien depuis la Bibliothèque) et ne servent jamais aux fiches, quiz ni flashcards.
  Bouton **Remettre dans les cours** si l'IA s'est trompée.
- Les documents en erreur ou encore en analyse ne sont pas modifiés par les actions de masse.

## 12. Sur le téléphone

```
┌───────────────────────────┐
│ Claunote          🔍  📷  │  recherche · photo d'un cours
│                           │
│  Actualités               │  page d'arrivée : veille et sujets suivis
│  …                        │
├───────────────────────────┤
│ Actualités Cours·248 Réviser Agenda Plus │  onglets en bas
└───────────────────────────┘
```

- On arrive sur **Actualités**. L'onglet **Cours** (la Bibliothèque) affiche le nombre total de cours ;
  flashcards du jour et campagne sont dans **Réviser**, évaluations et devoirs dans **Agenda**.
- **Plus** ne garde que trois entrées : **Ajouter**, **Ranger**, **Réglages**. La page **Réglages** mène à tout,
  en deux groupes : **Moi** (compte, notifications, données, appli iPhone) et, pour l'admin, **Administration**
  (comptes, sécurité, activité, état et disque, mise à jour, installation, « Tout voir »), avec l'état de chaque page.
- **Déposer** → **Prendre en photo** : chaque photo devient un document (une page = une photo, ou
  un PDF pour un cours de plusieurs pages).
- Flashcards : toucher la carte pour la retourner, les boutons de note restent sous le pouce.
- Le mode sombre suit celui du téléphone. Installation en appli : voir la section 10 (HTTPS requis).
- Notifications push et appli iPhone : voir la section 13.

## 13. Appli iPhone et notifications (gratuit, sans App Store)

```
                 ┌──────────── VPN ou même réseau ────────────┐
 iPhone ─ Safari ─┤ https://CLAUNOTE_HOTE ─► caddy ─► web:8000  │   ouvrir l'appli
 (appli web)      └────────────────────────────────────────────┘
      ▲
      │ notification chiffrée (Apple ne peut pas la lire)
      │
 web.push.apple.com ◄── worker (VM, sortie Internet) : rappel du jour, Pronote, révision prête
```

**À retenir** : il faut le VPN pour **ouvrir** Claunote, mais **les notifications arrivent partout** (4G
comprise) : c'est la VM qui les envoie au service push d'Apple, gratuit, sans compte développeur.

### Mise en place (une fois)

1. Dans `.env` : `CLAUNOTE_HOTE=<IP de la VM sur ton VPN>` (ou un nom si ton VPN a un DNS),
   `BIND_ADDR` = cette même IP, et ton e-mail dans `CLAUNOTE_PUSH_CONTACT=mailto:…`.
   Le port 443 est déjà pris sur la VM ? Mets `HTTPS_PORT=8443`.
2. `git pull && docker compose up -d --build` : le service **caddy** crée l'autorité de certification
   « Claunote » (volume `caddy`, à ne pas supprimer : sinon il faut réinstaller le certificat).
3. Sur l'iPhone, VPN allumé, ouvre **`http://<BIND_ADDR>:8000/installer`** dans Safari et suis les 5 étapes :
   certificat → confiance → `https://…` → **Partager → Sur l'écran d'accueil** → **Plus → Réglages → Notifications → Activer**.

### Face ID / Touch ID

*Plus → Réglages → Compte → Ajouter cet appareil*, puis le bouton **Se connecter avec Face ID** sur la page
de connexion. Le secret reste dans l'iPhone (clé d'accès) ; le site ne garde qu'une clé publique.

Face ID demande un **nom**, pas une adresse IP. Sans DNS à configurer, utilise **sslip.io**, qui renvoie
l'IP écrite dans le nom : `CLAUNOTE_HOTE=claunote.10.8.0.5.sslip.io` (avec l'IP VPN de ta VM), puis
`docker compose up -d` et ouvre `https://claunote.10.8.0.5.sslip.io`. La page Compte te donne le nom exact.
Changer de nom = nouvelle appli pour l'iPhone : réinstalle-la depuis la nouvelle adresse et rajoute l'appareil.

> ⚠️ Utilise **toujours la même adresse** (celle de `CLAUNOTE_HOTE`) : l'iPhone considère une autre IP
> comme une autre appli, avec ses propres réglages de notifications.

### Ce que tu reçois (page **Plus → Réglages → Notifications**)

| Notification | Quand | En la touchant |
|---|---|---|
| **Rappel du jour** (le plus utile) : contrôle sans fiche → flashcards à revoir → fiche à relire → nouveaux cours sans révision | à l'heure choisie (18 h par défaut), une fois par jour, rien s'il n'y a rien | la page qui va avec |
| Matin d'un contrôle | fin des heures de silence | ta fiche |
| Nouveau contrôle annoncé / documents des profs | après la synchro Pronote | Agenda |
| Fiche, quiz, flashcards prêts | dès que le worker a fini | la révision |

- **Heures de silence** (22 h → 7 h 30 par défaut) : rien ne sonne, la file attend le matin.
- La **pastille** sur l'icône = flashcards du jour + évaluations de la semaine.
- **Envoyer un test** vérifie toute la chaîne. Échec ? `docker compose logs -f worker | grep notif`
  (la VM doit pouvoir joindre `web.push.apple.com`).
- Désactiver : bouton **Désactiver ici**, ou Réglages iPhone → Notifications → Claunote.

## 14. Local d'abord, pause et arrêt (Plus → Activité)

Le travail interne est fait **sur la VM**, avec le processeur seul (pas de carte graphique) ; Claude n'intervient
que pour aider le modèle local ou le vérifier :

| Étape | Sur la VM (outils libres) | Quand Claude intervient |
| --- | --- | --- |
| Texte | Word, PowerPoint, PDF numérique, scripts et notebooks : texte extrait tel quel ; scans et photos : **OCR Tesseract** | OCR pauvre (écriture manuscrite, scan flou) : Claude lit le fichier lui-même |
| Classement | **Ollama** (`qwen2.5:3b`), sinon vote des cours déjà classés | confiance sous 70 % : Claude (Haiku, texte seul) vérifie et corrige ; 1 analyse sûre sur 10 : contrôle |
| Reclassement, noms de dossiers, matières en double | Ollama | réponse inutilisable ou doute |
| Chat (*Une question*) | Ollama, sur les passages de tous les documents et l'agenda | doute, ou le modèle local demande de l'aide : Claude vérifie et complète son brouillon |
| Recherche | passages par les mots (PostgreSQL) et par le sens (pgvector + `nomic-embed-text`) | jamais |

**La VM ne sature pas** : si la mémoire manque, si le processeur est occupé par autre chose, ou si la file
d'analyse ferait attendre un document plus de 30 min, le surplus part chez Claude (s'il est disponible).
La page *Activité* montre la charge, la durée moyenne d'une analyse locale et le **taux d'accord avec Claude**
mesuré par les contrôles : c'est ce qui te dit si tu peux faire confiance au local.

### Processeur : des pics, pas une VM à fond en permanence

```
occupation ▲   pic toléré (une analyse, une question)     budget dépassé : le fond fait des pauses
     100 % │  ███                                         ▁▁█▁▁█▁▁█▁▁
moyenne 60 ┼ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   ← CPU_MOYENNE_MAX
           └──────────────────────────────────────────────────────────────► temps
```

- **Budget sur la durée** : le worker mesure l'occupation moyenne du processeur sur 15 min
  (`CPU_FENETRE_MIN`). Un pic à 100 % est normal (environ 14 min tolérées). Au-delà de 60 % en moyenne
  (`CPU_MOYENNE_MAX`), le travail de fond (analyses locales, calcul du sens des passages) **fait des
  pauses** entre deux documents, ou part chez Claude s'il est là, jusqu'à redescendre sous 50 %. Rien
  n'est perdu, tout est fait un peu plus tard. Les questions du chat et les recherches ne sont jamais freinées.
- **Un cœur reste au site** : Ollama prend tous les cœurs sauf un (`OLLAMA_THREADS` pour changer),
  Tesseract un cœur par page, le service lecture `LECTURE_THREADS` (2).
- **Priorités** : quand le processeur est plein, le site et la base passent avant le worker, Ollama et
  le service lecture (`cpu_shares` dans `docker-compose.yml`) ; l'OCR tourne en priorité basse.
- **Rien n'est calculé deux fois** : un document rangé ou reclassé sans changer de texte garde le sens de
  ses passages ; une question déjà posée ne repasse pas par le modèle.
- **Moins de réveils** : une page laissée ouverte se rafraîchit toutes les 20 s quand tu t'en sers,
  puis toutes les minutes, puis toutes les 5 min (12 fois par heure au lieu de 180) ; elle se remet à
  jour dès que tu y reviens. L'inbox kDrive est relue toutes les 5 min quand rien n'y bouge depuis
  30 min (le site, lui, réveille le worker tout de suite).

La page *Activité* affiche la moyenne sur la durée et si le travail de fond est en train de ralentir.

### Mémoire : rendue quand elle ne sert pas

- **Ollama** garde son modèle (~2,3 Go) **5 min** après sa dernière réponse, puis le décharge
  (`OLLAMA_GARDER`) ; il se recharge en quelques secondes à la demande suivante. Il prépare 2 réponses en
  parallèle au plus (le fond + une question du chat) au lieu de 4 : ~600 Mo de contexte en moins.
- **Service lecture** : un modèle inutilisé depuis 10 min est déchargé et sa mémoire rendue au système
  (`LECTURE_GARDER_MIN`).
- **Mémoire basse** (moins de 2 × `LOCAL_MEMOIRE_MIN_MO` libres) : le worker fait décharger tout de suite
  les modèles au repos, sans attendre leur délai ; rien n'est perdu, ils se rechargent au prochain usage.
- **Service Pronote** : tas JavaScript plafonné à 256 Mo (Node garderait sinon jusqu'à 2 Go).

### Mise en place

Ollama démarre avec le reste (`docker compose up -d`) et télécharge ses deux modèles au premier lancement
(~2,3 Go). Pour une petite VM (4 Go) : `OLLAMA_MODELE=qwen2.5:1.5b`. Avec 16 Go : `qwen2.5:7b`, plus fin mais plus lent.
Pour t'en passer : **Analyses → « Éteindre les modèles locaux »** sur le site. Ollama et le service
`lecture` sont arrêtés (par le service `maj`), plus jamais appelés ni reconstruits, et tout passe par Claude.
Le bouton « Rallumer » les relance (reconstruit `lecture` si le code a changé). Même page : **qui consomme le
processeur**, conteneur par conteneur (relevé à chaque « Vérifier » de la mise à jour et toutes les 6 h).
Sans le service `maj` : `docker compose stop ollama lecture` à la main.

### Réglages (Activité → Qui fait le travail)

- **Local d'abord** (défaut) : comme ci-dessus.
- **Claude d'abord** : Claude fait tout, le local seulement pendant une limite ou une panne.
- **Local uniquement** : jamais Claude pour les analyses (ton forfait reste aux révisions).
- **Claude uniquement** : attendre Claude.
- **Faire relire par Claude** les documents douteux lus pendant une absence de Claude, à son retour.
  Tu peux annuler ces relectures, ou en demander toi-même : *Ranger → Tous les documents → Relire avec Claude*.

### Pause, arrêt, file d'analyse

- **Mettre en pause** : les analyses en cours se terminent, aucune autre ne démarre. Révisions, rangements et
  Pronote continuent.
- **Arrêter maintenant** : coupe tout de suite les analyses en cours. Les appels `claude -p` sont arrêtés et
  l'OCR s'arrête entre deux pages. Rien n'est perdu ni mis en erreur : les fichiers restent dans la file.
- **File d'analyse** : coche des fichiers puis choisis une action de masse :
  - « Ne pas analyser pour l'instant » / « Remettre dans la file » ;
  - « Modèle local » / « Claude directement » / « Selon le réglage » ;
  - « Supprimer » (corbeille kDrive).

### Menus

Le menu **Plus** a quatre entrées, chacune avec ses onglets :
- **Ajouter** : Déposer, Import de dossiers ;
- **Ranger** : À vérifier, Tous les documents, Matières, Hors cours ;
- **Activité** : Analyses, État du service ;
- **Réglages** : Compte, Notifications, Mise à jour.

La barre de recherche de l'accueil sert aussi à **poser une question à l'IA** (bouton *Demander*). Sur ordinateur,
la touche `/` place le curseur dans la recherche.

## 15. Mise à jour depuis le site

*Plus → Réglages → Mise à jour* : version installée, nouveautés disponibles sur GitHub (vérifiées toutes
les 6 h, badge sur le menu Réglages) et trois actions **réservées à l'admin** (le compte connecté) :

- **Tout mettre à jour** : `git pull`, construction des nouvelles images (le site reste en ligne pendant ce
  temps), puis redémarrage des services modifiés. **Seul ce qui a changé est reconstruit** (un changement du
  site ne reconstruit pas le service lecture et son PyTorch ; un changement de documentation, rien du tout),
  et les téléchargements pip restent en cache d'une fois sur l'autre. Le journal donne la durée de chaque étape.
  Le service `maj` retient **la version réellement en service** de chaque service : après un `git pull` fait à
  la main ou une mise à jour interrompue, la page affiche « Pas encore installé » et *Tout mettre à jour*
  reconstruit ce qui est en retard (avant, l'ancien code pouvait continuer de tourner). Une configuration
  Caddy modifiée redémarre Caddy.
- ⚠️ **Pendant une mise à jour, ne lance aucune commande sur la VM.** Deux constructions en même temps (celle
  du site et une à la main), à côté de tous les services, peuvent saturer la mémoire et figer la machine. La page
  affiche « Mise à jour en cours » tant qu'elle tourne. Pour limiter la mémoire, les services sont construits un
  par un, et Ollama et le service lecture sont mis en pause pendant la construction si la VM a moins de 3 Go
  libres (relancés à la fin, même en cas d'échec). Le site se coupe environ une minute : la page se reconnecte
  toute seule. Si la construction échoue, l'ancienne version continue de tourner.
- **Redémarrer** : site et worker, sans changer de version.
- **Libérer de la place** : supprime les anciennes images Docker et le cache de construction de plus de
  30 jours (le cache récent est gardé : sans lui, la mise à jour suivante retéléchargerait tout).

```
site ──demande──▶ volume « maj » ──▶ service maj : git pull, docker compose build, up -d
site ◀── état + journal ─────────────────┘
```

Le service `maj` pilote Docker (socket monté) : il n'exécute **que ces actions**, jamais une commande venue
du site. git tourne en root puis rend `.git` et les fichiers du dépôt au propriétaire du dossier Claunote
(ça répare aussi les fichiers laissés à root par un ancien `sudo git pull`). Si un fichier a été
modifié à la main sur la VM, la mise à jour est refusée sans rien toucher (`git status` pour voir lequel).

**Dépôt GitHub privé** : le service `maj` n'a pas tes identifiants GitHub (« could not read Username »).
Crée un [jeton fine-grained](https://github.com/settings/personal-access-tokens/new) limité au dépôt
`claunote`, permission **Contents → Read-only**, et mets-le dans `.env` : `GITHUB_TOKEN=github_pat_…`.
⚠️ **Resource owner = le propriétaire du dépôt** : l'organisation `juteaucorp` depuis que le dépôt y a été
déplacé (un jeton créé pour un compte perso ne voit pas le dépôt). Si l'organisation exige une approbation,
un propriétaire l'accepte dans *Settings → Personal access tokens → Pending requests*.
Une VM clonée depuis l'ancienne adresse (`…/ctrljuteau/claunote`) est repointée toute seule vers
`juteaucorp/claunote` à la prochaine vérification.
Il est relu à chaque action : pas de redémarrage. Il n'est jamais écrit ailleurs ni affiché dans le journal.

**Première fois** : le service n'existe pas encore, il faut une dernière mise à jour à la main, depuis le
dossier de Claunote (pas avec `sudo`, qui peut perdre le dossier courant) :

```bash
git pull && docker compose up -d --build
```

Le service `maj` lui-même n'est jamais reconstruit par le site (il s'arrêterait en plein travail) ; son
script est relu dans le dépôt à chaque action, donc il suit les mises à jour. Si `infra/maj/Dockerfile`
change un jour : `docker compose up -d --build maj`.

## 16. Lecture des documents, formules, tri des passages

```
Word, PowerPoint, Excel ─► Docling (site et worker) ─► titres, listes, tableaux, équations Word en LaTeX
PDF numérique ───────────► service lecture : Docling + modèles de mise en page ─► tableaux, colonnes
photo d'une formule ─────► service lecture : pix2tex ─► LaTeX dans ta question (Demander → Formule)
question du chat ────────► service lecture : FlashRank ─► les passages les plus utiles d'abord
```

- **Docling** lit les fichiers bureautiques sans modèle d'IA (rapide). Les PDF numériques passent par le
  service `lecture`, qui garde les tableaux et l'ordre des colonnes ; s'il perd du texte, l'extraction
  simple est gardée. Les scans, eux, passent toujours par l'OCR ou par Claude.
- **Formules** : les cours, fiches, cartes et réponses écrivent leurs formules en LaTeX (`$…$`), dessinées
  par KaTeX sur le site (chimie comprise : `\ce{H2O}`) et converties en texte lisible dans les PDF.
- **Photo d'une formule** : *Demander → Formule*, encadre la formule seule, elle est recopiée en LaTeX.
- **Service `lecture`** : facultatif. Sans lui (`docker compose stop lecture`), tout marche comme avant.
  Ses modèles se téléchargent au premier usage (HuggingFace, GitHub) dans le volume `lecture`, et prennent
  environ 1,5 Go de mémoire une fois tous chargés. `LECTURE_THREADS` (défaut 2) : cœurs par calcul.

## 17. Accès par Internet (Cloudflare Tunnel), seulement sur tes appareils

> 📘 **Guide complet** (fonctionnement, procédure détaillée, dépannage) : [docs/cloudflare-tunnel.md](../docs/cloudflare-tunnel.md).

```
 Appareil approuvé ── 4G, wifi, n'importe où ──► Cloudflare  https://claunote.ton-domaine.fr
 (iPhone, PC)                                     │  règle pare-feu : France seulement
                                                  ▼
                              tunnel SORTANT (aucun port ouvert sur ta box ni sur la VM)
                                                  ▼
                                VM : cloudflared ─► web:8000
                                                  │
                         ① appareil approuvé ?  non ─► page « Code d'appareil » (rien d'autre visible)
                         ② connecté ?           non ─► mot de passe ou Face ID, comme avant

 Par le VPN : rien ne change (pas de code d'appareil, mêmes adresses qu'avant).
 Notifications : elles ne passent pas par le tunnel (VM ─► Apple), elles marchent comme avant.
```

**À retenir** : un appareil inconnu ne voit **que la page du code**, même avec le bon mot de passe.
Chaque appareil a son propre accès, que tu **retires en un clic** (*Réglages → Compte → Appareils par Internet*).

### Pourquoi un code d'appareil plutôt qu'une option Cloudflare

| Méthode | Appli iPhone (écran d'accueil) + notifications | Verdict |
|---|---|---|
| **Code d'appareil dans Claunote** | **oui** | **choisi** : gratuit, un accès par appareil, retirable |
| Certificat client (mTLS) Cloudflare | peu fiable : l'appli d'écran d'accueil ne présente pas toujours le certificat | possible en plus pour un PC, pas pour l'iPhone |
| WARP + posture d'appareil | l'iPhone n'accepte **qu'un VPN à la fois** : WARP couperait ton VPN | non |
| Cloudflare Access (code par e-mail) | connexion qui sort de l'appli, pas lié à un appareil | facultatif, en plus |

### Ce qu'il te faut

- Un compte Cloudflare (gratuit) et **un nom de domaine géré par Cloudflare** (un domaine que tu as déjà,
  ou ~10 €/an chez Cloudflare Registrar). Le tunnel a besoin d'un nom à toi : les adresses `trycloudflare.com`
  sont aléatoires et ne se restreignent pas.

### Étapes côté Cloudflare (à faire toi-même, une fois)

Les noms de menus bougent parfois ; le principe reste le même.

1. **Domaine** : [dash.cloudflare.com](https://dash.cloudflare.com) → *Ajouter un domaine* → plan **Free** →
   remplace les serveurs de noms chez ton registraire par ceux que Cloudflare affiche → attends l'état **Actif**.
   (Domaine acheté chez Cloudflare : rien à faire.)
2. **Tunnel** : [one.dash.cloudflare.com](https://one.dash.cloudflare.com) (Zero Trust, plan gratuit) →
   *Réseaux → Tunnels* → **Créer un tunnel** → type **Cloudflared** → nom `claunote`.
   Cloudflare affiche une commande d'installation : **ne l'exécute pas**, copie seulement le jeton
   (la longue suite qui commence par `eyJ…`, après `--token`).
3. **Sur la VM**, dans `.env` :
   ```
   COMPOSE_PROFILES=tunnel
   CLOUDFLARE_TUNNEL_TOKEN=eyJ…
   ```
   puis `git pull && docker compose up -d --build`. Dans le tableau de bord, le tunnel passe à **Sain (Healthy)**.
4. **Adresse publique** (dans le tunnel → *Nom d'hôte public* / *Published application routes*) → *Ajouter* :
   sous-domaine `claunote`, ton domaine, **Type `HTTP`**, **URL `web:8000`**.
   ⚠️ Bien `HTTP` et `web:8000` (pas `https`, pas `caddy`) : Cloudflare fait déjà le HTTPS.
5. **Pare-feu (recommandé)** : ton domaine → *Sécurité → Règles WAF → Règles personnalisées* → *Créer une règle* :
   nom `Claunote France seulement`, *Modifier l'expression* :
   ```
   (http.host eq "claunote.ton-domaine.fr" and ip.src.country ne "FR")
   ```
   Action **Bloquer**. En voyage à l'étranger : désactive la règle le temps du séjour.
   Dans *SSL/TLS → Certificats Edge*, active **Toujours utiliser HTTPS**.

### Approuver tes appareils

1. **Premier code**, au choix :
   - par le VPN : `http://<BIND_ADDR>:8000/compte` → **Ajouter un appareil** ;
   - sur la VM : `docker compose exec web python -m claunote.appareils`.

   Le code (ex. `K7QM-3XPA`) vaut **15 minutes, une seule fois**.
2. **iPhone** : Safari → `https://claunote.ton-domaine.fr` → tape le code → connexion →
   **Partager → Sur l'écran d'accueil**. Ouvre l'appli : si elle redemande un code (l'iPhone sépare l'appli
   de Safari), crée un nouveau code et tape-le **dans l'appli**. Puis *Plus → Réglages → Notifications →
   Activer* et *Compte → Ajouter cet appareil* pour Face ID.
3. **Les appareils suivants** : *Réglages → Compte → Ajouter un appareil* depuis un appareil déjà approuvé
   (même par Internet), puis le code sur le nouvel appareil.

> **Une seule appli suffit** : celle installée depuis l'adresse Cloudflare marche aussi à la maison
> (elle passe par Internet). Celle du VPN reste possible, mais c'est une **autre appli** pour l'iPhone
> (notifications et Face ID à activer dans chacune).

### Appareil perdu ou volé

*Réglages → Compte → Appareils par Internet* → **Retirer** : coupé tout de suite. Change aussi ton mot de passe
(page Compte), ce qui déconnecte toutes les sessions.

### Couper l'accès par Internet

Mets `#COMPOSE_PROFILES=tunnel` en commentaire puis `docker compose stop cloudflared`, ou supprime le tunnel
dans Cloudflare. `CLAUNOTE_TUNNEL_APPAREILS=0` retire le contrôle des appareils : à n'utiliser **que** si
Cloudflare Access fait déjà le tri.

### Dépannage

| Symptôme | Cause probable |
|---|---|
| Erreur Cloudflare **1033** | cloudflared arrêté ou jeton faux : `docker compose logs -f cloudflared` |
| Erreur **502** | adresse du service fausse dans le tunnel : doit être `HTTP` + `web:8000` |
| Le code est redemandé à chaque ouverture | cookies bloqués (navigation privée), ou appareil retiré |
| « Trop d'essais » | 5 codes faux en 5 min depuis la même adresse : attends 5 min |
| Erreur **1020** (accès refusé) | la règle « France seulement » : désactive-la en voyage |

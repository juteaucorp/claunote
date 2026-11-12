# Ouvrir Claunote depuis Internet avec Cloudflare Tunnel

Ce guide sert à **ouvrir ton Claunote hors de chez toi** (4G, wifi d'ailleurs) **sur tes appareils seulement**,
sans ouvrir de port sur ta box et sans VPN sur le téléphone. C'est **facultatif** : sans tunnel, Claunote marche
comme avant sur ton réseau local ou ton VPN.

Durée : **20 à 30 minutes** la première fois. Coût : **gratuit** (plan Free de Cloudflare), plus un nom de domaine
si tu n'en as pas (environ 10 € par an).

---

## 1. Comment ça marche

```
   Ton iPhone / ton PC, n'importe où
                │  https://claunote.ton-domaine.fr
                ▼
   ┌──────────────────────── Cloudflare ────────────────────────┐
   │  certificat HTTPS, règle pare-feu « France seulement »     │
   └────────────────────────────┬───────────────────────────────┘
                                │  le tunnel : une connexion SORTANTE
                                │  ouverte par ta machine vers Cloudflare
   ┌────────────── ta machine (docker compose) ─────────────────┐
   │  cloudflared ──► web:8000 (le site Claunote)               │
   │                     │                                      │
   │        ① appareil approuvé ?  non ─► page « Code d'appareil »│
   │        ② connecté ?           non ─► mot de passe / Face ID │
   │                     ▼                                      │
   │                  Claunote                                  │
   └────────────────────────────────────────────────────────────┘

   Réseau local / VPN : http://<ip>:8000 ou https://<CLAUNOTE_HOTE>
   ─► rien ne change, pas de code d'appareil.
```

**Les trois idées à retenir :**

1. **Aucun port ouvert.** C'est le conteneur `cloudflared` qui appelle Cloudflare, jamais l'inverse.
   Ta box et ta machine restent fermées à Internet.
2. **Deux verrous l'un après l'autre.** Un appareil inconnu ne voit **que la page du code**, même s'il connaît
   le mot de passe. Un appareil approuvé doit **encore se connecter** (mot de passe ou Face ID).
3. **Un accès par appareil.** Chaque appareil reçoit son propre accès, que tu **retires en un clic** s'il est
   perdu ou volé.

### Ce qui se passe à la première visite d'un nouvel appareil

```
 Nouvel appareil            Claunote                     Appareil déjà approuvé
       │  ouvre le site        │                        (ou réseau local / VPN)
       │──────────────────────►│                                 │
       │ ◄── « Code d'appareil »│                                 │
       │                       │ ◄── Réglages → Compte →          │
       │                       │     « Ajouter un appareil »      │
       │                       │──── code K7QM-3XPA (15 min) ────►│
       │── tape K7QM-3XPA ────►│                                 │
       │ ◄── cookie d'appareil (400 jours), puis page de connexion│
```

- Le code fait **8 caractères**, vaut **15 minutes** et ne sert **qu'une fois**. Après 20 essais faux, il est
  annulé. Après 5 essais faux depuis la même adresse, cette adresse attend 5 minutes.
- Le cookie d'appareil contient un secret aléatoire. **Le serveur n'en garde que l'empreinte** : même quelqu'un
  qui lirait la base ne pourrait pas refaire le cookie.
- **Les notifications ne passent pas par le tunnel.** C'est le serveur qui les envoie directement à Apple ou
  Google. Elles marchent avec ou sans tunnel.

### Pourquoi un code d'appareil, et pas une option de Cloudflare

| Méthode | Appli iPhone (écran d'accueil) + notifications | Verdict |
|---|---|---|
| **Code d'appareil dans Claunote** | **oui** | **choisi** : gratuit, un accès par appareil, retirable |
| Certificat client (mTLS) de Cloudflare | peu fiable : l'appli d'écran d'accueil ne présente pas toujours le certificat | possible en plus pour un PC |
| WARP + posture d'appareil | l'iPhone n'accepte **qu'un VPN à la fois** : WARP couperait ton VPN | non |
| Cloudflare Access (code par e-mail) | la connexion sort de l'appli, et elle n'est pas liée à un appareil | facultatif, en plus |

---

## 2. Ce qu'il te faut

- [ ] Claunote installé et qui marche en local (voir [infra/SETUP.md](../infra/SETUP.md)).
- [ ] Un **compte Cloudflare** (gratuit) : [dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up).
- [ ] Un **nom de domaine géré par Cloudflare**. Soit un domaine que tu as déjà, soit un domaine acheté chez
      Cloudflare Registrar. Les adresses gratuites `trycloudflare.com` ne conviennent pas : elles changent à
      chaque démarrage et ne se restreignent pas.

Dans la suite, remplace **`ton-domaine.fr`** par ton domaine et **`claunote`** par le sous-domaine de ton choix.

---

## 3. Procédure pas à pas

Les noms de menus de Cloudflare changent parfois ; le principe reste le même.

### Étape 1. Mettre ton domaine sur Cloudflare *(dashboard Cloudflare)*

1. [dash.cloudflare.com](https://dash.cloudflare.com) → **Ajouter un domaine** → tape ton domaine → plan **Free**.
2. Cloudflare affiche **deux serveurs de noms**. Chez ton registraire (OVH, Gandi, Infomaniak…), remplace les
   serveurs de noms du domaine par ces deux-là.
3. Attends que le domaine passe à l'état **Actif** (de quelques minutes à quelques heures).

> Domaine acheté chez Cloudflare : cette étape est déjà faite.

### Étape 2. Créer le tunnel *(dashboard Zero Trust)*

1. [one.dash.cloudflare.com](https://one.dash.cloudflare.com) (Zero Trust). À la première visite, choisis un nom
   d'équipe et le plan **Free** (une carte bancaire peut être demandée, rien n'est facturé).
2. **Réseaux → Tunnels** → **Créer un tunnel** → type **Cloudflared** → nom `claunote` → **Enregistrer**.
3. Cloudflare affiche une commande d'installation. **Ne l'exécute pas** : Docker s'en charge.
   **Copie seulement le jeton**, la longue suite qui commence par `eyJ…` après `--token`.

### Étape 3. Démarrer le tunnel *(sur ta machine)*

Dans le fichier `.env` de Claunote, ajoute :

```
COMPOSE_PROFILES=tunnel
CLOUDFLARE_TUNNEL_TOKEN=eyJ…ton-jeton…
```

Puis :

```bash
git pull && docker compose up -d --build
docker compose logs cloudflared | tail   # doit afficher « Registered tunnel connection »
```

Dans le dashboard, le tunnel passe à **Sain (Healthy)**.

> 🔒 Le jeton donne le contrôle du tunnel : **ne le publie jamais** (il reste dans `.env`, qui n'est pas versionné).

### Étape 4. Donner une adresse au site *(dashboard Zero Trust)*

Dans le tunnel → onglet **Nom d'hôte public** (ou *Published application routes*) → **Ajouter** :

| Champ | Valeur |
|---|---|
| Sous-domaine | `claunote` |
| Domaine | `ton-domaine.fr` |
| Type | **`HTTP`** |
| URL | **`web:8000`** |

⚠️ **Bien `HTTP` et `web:8000`**, pas `https` ni `caddy` : Cloudflare fait déjà le HTTPS, et `web` est le nom du
service du site dans `docker-compose.yml`.

Vérifie : `https://claunote.ton-domaine.fr` doit afficher la page **« Code d'appareil »**. Si tu vois directement
la page de connexion ou le site, arrête-toi et relis l'étape 4.

### Étape 5. Pare-feu « France seulement » *(recommandé, dashboard Cloudflare)*

1. Ton domaine → **Sécurité → Règles WAF → Règles personnalisées** → **Créer une règle**.
2. Nom : `Claunote France seulement`. **Modifier l'expression** :
   ```
   (http.host eq "claunote.ton-domaine.fr" and ip.src.country ne "FR")
   ```
3. Action : **Bloquer** → **Déployer**.
4. Dans **SSL/TLS → Certificats Edge**, active **Toujours utiliser HTTPS**.

> Tu pars à l'étranger ? Désactive la règle le temps du séjour (ou remplace `"FR"` par ton pays).

### Étape 6. Approuver ton premier appareil

1. **Crée un code**, au choix :
   - depuis le réseau local ou ton VPN : `http://<ip-de-la-machine>:8000/compte` → **Ajouter un appareil** ;
   - ou sur la machine :
     ```bash
     docker compose exec web python -m claunote.appareils
     ```
2. Sur l'appareil, ouvre `https://claunote.ton-domaine.fr`, tape le code et le nom de l'appareil.
3. Connecte-toi avec ton identifiant et ton mot de passe Claunote.

### Étape 7. Installer l'appli sur l'iPhone

1. Dans Safari, une fois connecté : **Partager → Sur l'écran d'accueil**.
2. Ouvre l'appli. **Si elle redemande un code**, c'est normal : l'iPhone sépare l'appli de Safari. Crée un nouveau
   code et tape-le **dans l'appli**.
3. Dans l'appli : **Plus → Réglages → Notifications → Activer**, puis **Compte → Ajouter cet appareil** pour Face ID.

> **Une seule appli suffit** : celle installée depuis l'adresse Cloudflare marche aussi à la maison. Une appli
> installée depuis l'adresse du réseau local ou du VPN est une **autre appli** pour l'iPhone, avec ses propres
> notifications et son propre Face ID.

### Étape 8. Les appareils suivants

Depuis n'importe quel appareil déjà approuvé (même par Internet) : **Réglages → Compte → Appareils par Internet →
Ajouter un appareil**, puis tape le code sur le nouvel appareil.

### Étape 9. Durcir Cloudflare *(dashboard Cloudflare, 10 minutes)*

Le site se défend déjà seul (appareil approuvé, blocages, pays, alertes : voir **Réglages → Sécurité**).
Ces réglages arrêtent les attaques **avant** qu'elles n'arrivent chez toi :

| # | Où | Réglage |
|---|---|---|
| 1 | **Sécurité → Bots** | **Bot Fight Mode : activé** (robots connus bloqués ; si l'appli iPhone se met à afficher des erreurs, désactive-le) |
| 2 | **Sécurité → Règles WAF → Règles de limitation de débit** | `(http.host eq "claunote.ton-domaine.fr" and http.request.method eq "POST" and http.request.uri.path in {"/connexion" "/appareil" "/cles/connexion"})` → **5 requêtes / 10 s par IP** → **Bloquer** (offre gratuite : 1 règle, période et durée de 10 s ; le site bloque ensuite plus longtemps lui-même) |
| 3 | **Sécurité → Règles WAF → Règles gérées** | **Cloudflare Free Managed Ruleset : activé** |
| 4 | **Sécurité → Paramètres** | **Niveau de sécurité : Moyen** (ou Élevé), **Browser Integrity Check : activé** |
| 5 | **SSL/TLS → Certificats Edge** | **Toujours utiliser HTTPS**, **TLS minimum 1.2**, **TLS 1.3 activé** |
| 6 | **SSL/TLS → Certificats Edge** | **HSTS : activé** (max-age 6 mois, sans « preload ») |
| 7 | **Notifications** (compte Cloudflare) | crée une alerte **« Security Events Alert »** (e-mail quand le pare-feu bloque en masse ; offres payantes seulement, sinon les alertes du site suffisent) |
| 8 | **Mon profil → Authentification** | **2FA activée sur ton compte Cloudflare** : c'est la clé de tout le reste |
| 9 | **Zero Trust → Réseaux → Tunnels** | ne partage jamais le jeton du tunnel ; s'il fuit : **Actualiser le jeton**, puis mets le nouveau dans `.env` |

> ❗ **Pas de « Cloudflare Access » ni de WARP** devant le site : l'appli iPhone ne s'en sort pas (voir plus haut).

---

## 4. Au quotidien

| Je veux… | Je fais… |
|---|---|
| Voir mes appareils | **Réglages → Compte → Appareils par Internet** (nom, date d'ajout, dernière visite) |
| Couper un appareil perdu ou volé | **Retirer** sur cette page (effet immédiat), puis change le mot de passe sur la page Compte |
| Voir qui a essayé d'entrer | **Réglages → Sécurité** : journal, alertes, adresses bannies |
| Tout couper d'un coup (téléphone volé…) | **Réglages → Sécurité → Déconnecter partout** (+ case « retirer les appareils ») |
| Couper tout l'accès par Internet | mets `#COMPOSE_PROFILES=tunnel` en commentaire dans `.env`, puis `docker compose stop cloudflared` |
| Le supprimer pour de bon | supprime aussi le tunnel dans le dashboard Zero Trust |

`CLAUNOTE_TUNNEL_APPAREILS=0` dans `.env` retire le contrôle des appareils. **Ne le fais que** si Cloudflare Access
protège déjà l'adresse.

---

## 5. Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| Erreur Cloudflare **1033** | `cloudflared` arrêté ou jeton faux | `docker compose logs -f cloudflared` ; recopie le jeton |
| Erreur **502** | mauvaise adresse de service | dans le tunnel : `HTTP` + `web:8000` |
| Erreur **1020** (accès refusé) | règle « France seulement » | désactive-la à l'étranger |
| Le code est redemandé à chaque ouverture | cookies bloqués (navigation privée) ou appareil retiré | navigation normale ; nouveau code |
| « Trop d'essais » | 5 codes faux en 5 min depuis la même adresse | attends 5 minutes |
| « accès refusé (adresse bloquée) » | 20 échecs en 24 h ou 100 requêtes en 10 min sans appareil approuvé | par le VPN : **Réglages → Sécurité → Lever les blocages** |
| « accès refusé (pays non autorisé) » | pays hors de `CLAUNOTE_PAYS` | à l'étranger : `CLAUNOTE_PAYS=FR,XX` dans `.env`, puis `docker compose up -d web` |
| Alerte « faux en-têtes Cloudflare » | cloudflared lancé hors de ce docker-compose | `CLAUNOTE_CLOUDFLARED=<son adresse>` dans `.env` ; sinon, une machine du réseau triche |
| `cloudflared` ne démarre pas | `COMPOSE_PROFILES=tunnel` absent du `.env` | ajoute la ligne, puis `docker compose up -d` |

---

## 6. Questions fréquentes

**Est-ce que Cloudflare voit mes cours ?** Oui, techniquement : Cloudflare déchiffre le HTTPS pour l'acheminer,
comme tout service de ce type. Si tu ne le veux pas, reste sur ton réseau local ou ton VPN.

**Et si quelqu'un trouve l'adresse ?** Il ne voit que la page du code. Sans code valable (15 minutes, une seule
fois), il ne peut rien faire, et le mot de passe reste demandé derrière.

**Ça marche sans nom de domaine ?** Non, pas pour un accès durable : le tunnel nommé a besoin d'un domaine géré
par Cloudflare.

**Ça change quelque chose au réseau local ou au VPN ?** Non. Le contrôle des appareils ne s'applique qu'aux visites
qui passent par Cloudflare.

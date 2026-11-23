# Page de maintenance

Quand Claunote ne répond pas (mise à jour, redémarrage, VM éteinte, tunnel coupé), tu vois **la page
« Claunote revient dans un instant »** aux couleurs de Claunote (clair ou sombre, avec le papillon) au lieu
d'une erreur technique comme **« Error 1033 — Cloudflare Tunnel error »**. La page **se recharge toute seule**
dès que Claunote répond de nouveau.

## Qui affiche la page, selon la panne

```
                         ┌──────────────── Cloudflare ────────────────┐
  Par Internet ─────────►│ Worker « claunote-maintenance »            │──► tunnel ──► cloudflared ──► web:8000
  (claunote.domaine.fr)  │  erreur 1033/530, 52x, 502-504 ?           │
                         │  ──► page de maintenance (Worker)  ①       │
                         └────────────────────────────────────────────┘

  Par le VPN ───────────► Caddy (https://CLAUNOTE_HOTE) ──► web:8000
                            site arrêté ? ──► page de maintenance (Caddy)  ②
```

| Panne | Par Internet | Par le VPN |
|---|---|---|
| Le site redémarre (mise à jour, `docker compose restart web`) | ① Worker : « Claunote redémarre » | ② Caddy |
| `cloudflared` arrêté, tunnel coupé (erreur **1033**) | ① Worker : « le serveur ne répond pas » | — (le VPN ne passe pas par le tunnel) |
| VM éteinte ou sans réseau | ① Worker | navigateur : pas de connexion (page « Hors ligne » de l'appli) |

- **② Caddy** marche tout seul, rien à faire : il est configuré dans `infra/caddy/Caddyfile` et lit la page dans
  `infra/maintenance/maintenance.html`.
- **① Le Worker** doit être installé **une fois** dans Cloudflare (10 minutes, gratuit) : voir plus bas.
  Seul le réseau Cloudflare peut répondre quand ta machine est injoignable, et les pages d'erreur
  personnalisées de Cloudflare ne couvrent pas l'erreur 1033 du tunnel : un Worker, si.

**Un vrai message d'erreur de Claunote n'est jamais caché** : les réponses du site portent l'en-tête
`X-Claunote-App`, et le Worker ne remplace que les erreurs venues de Cloudflare ou du tunnel. Un « kDrive ne
répond pas » (503 de Claunote) reste affiché tel quel.

## Pendant une mise à jour (Réglages → Mise à jour)

Rien à activer : la construction se fait **site allumé**, et le site n'est coupé que pendant le redémarrage
(quelques secondes à une minute). Pendant ce temps, la page de maintenance s'affiche toute seule puis la
page d'origine revient. Caddy attend aussi jusqu'à 5 s que le site revienne avant d'afficher la page : un
redémarrage éclair passe sans qu'on la voie.

## Appli iPhone et notifications

- **L'appli de l'écran d'accueil** affiche la page de maintenance comme n'importe quelle page, puis se recharge
  seule. La page n'est jamais gardée en cache (ni par l'appli, ni par Cloudflare) : pas de page de maintenance
  « coincée » une fois le site revenu.
- **Les notifications** ne passent pas par Cloudflare (le serveur les envoie à Apple directement) : le Worker n'y
  change rien.
- **Le thème** : la page reprend celui choisi dans Réglages → Apparence sur cet appareil (sinon celui du
  téléphone).

## Installer le Worker Cloudflare (une fois, 10 minutes)

Il faut le tunnel déjà en place ([cloudflare-tunnel.md](cloudflare-tunnel.md)). Le plan **Free** suffit
(100 000 requêtes par jour, très au-dessus d'un usage normal de Claunote).

Le tableau de bord de Cloudflare est en anglais par défaut ; le nom français est entre parenthèses quand la
langue est réglée sur Français (icône de profil → **My Profile** → **Preferences** → **Language**).
Cloudflare renomme parfois ses menus : si un nom a changé, cherche le mot en gras le plus proche.

### Étape 1. Créer le Worker

1. Ouvre **https://dash.cloudflare.com** et connecte-toi.
2. Menu de gauche : **Compute (Workers)** → **Workers & Pages**.
3. Bouton bleu **Create** (Créer) → onglet **Workers** → **Start with Hello World!** → **Get started**.
4. **Worker name** : tape `claunote-maintenance` → bouton **Deploy** (Déployer).
5. Sur la page « Success », bouton **Edit code** (Modifier le code).

### Étape 2. Coller le code

1. Dans le dépôt Claunote, ouvre **`infra/maintenance/worker.js`** (sur GitHub : le fichier → bouton
   **Copy raw file**, l'icône « copier » en haut à droite du fichier).
2. Dans l'éditeur Cloudflare, à gauche, clique sur **worker.js**, sélectionne tout (Ctrl+A / Cmd+A), **supprime**,
   puis **colle** le contenu copié.
3. Bouton **Deploy** (Déployer) en haut à droite → confirme **Save and deploy**.

### Étape 3. Brancher le Worker sur l'adresse de Claunote

1. Reviens à la page du Worker (flèche **←** en haut à gauche de l'éditeur, ou **Workers & Pages** →
   **claunote-maintenance**).
2. Onglet **Settings** (Paramètres) → section **Domains & Routes** (Domaines et itinéraires) → **+ Add**
   (Ajouter) → **Route**.
3. **Zone** : choisis ton domaine (ex. `ton-domaine.fr`).
4. **Route** : tape l'adresse de Claunote suivie de `/*`, par exemple **`claunote.ton-domaine.fr/*`**.
5. **Failure mode** (Mode d'échec) : choisis **Fail open (proceed)** (Ouverture en cas d'échec). Si un jour le
   quota gratuit est dépassé, le site marche quand même, simplement sans la page de maintenance.
6. Bouton **Add route** (Ajouter un itinéraire).
7. Toujours dans **Domains & Routes** : si une ligne **workers.dev** ou **Preview URLs** est **activée**,
   désactive-la (icône **⋯** ou interrupteur → **Disable**). Le Worker n'a pas besoin d'une adresse à lui.

### Étape 4. Vérifier

Sur la VM, dans le dossier de Claunote :

```bash
docker compose stop cloudflared      # simule le tunnel coupé (erreur 1033)
```

Ouvre `https://claunote.ton-domaine.fr` en 4G : **la page « Claunote revient dans un instant »** s'affiche.

```bash
docker compose start cloudflared     # la page se recharge seule et Claunote revient (≈ 10-30 s)
docker compose stop web              # simule une mise à jour : « Claunote redémarre »
docker compose start web
```

Si tu vois encore « Error 1033 » : la route de l'étape 3 ne correspond pas exactement à l'adresse (vérifie le
`/*` à la fin et le sous-domaine), ou le nom DNS n'est pas « proxifié » (nuage orange dans **DNS** →
**Records**, c'est le cas par défaut pour un tunnel).

## Mettre à jour le Worker

Le Worker ne se met **pas** à jour avec Réglages → Mise à jour (il vit chez Cloudflare). C'est rarement utile : la
page ne change presque jamais. Si `infra/maintenance/worker.js` a changé, refais simplement l'**étape 2**.

### Variante en ligne de commande (facultatif)

Pour déployer sans copier-coller, avec un jeton Cloudflare **Edit Cloudflare Workers** (icône de profil →
**My Profile** → **API Tokens** → **Create Token** → modèle **Edit Cloudflare Workers**), depuis le dossier de
Claunote sur la VM :

```bash
docker run --rm -it -v "$PWD/infra/maintenance:/w" -w /w -e CLOUDFLARE_API_TOKEN=<le-jeton> \
  node:24-alpine npx --yes wrangler@4 deploy --route "claunote.ton-domaine.fr/*"
```

Puis vérifie une fois le **Failure mode** de la route dans le tableau de bord (étape 3, point 5).

## Pour les développeurs

- `infra/maintenance/maintenance.html` : **la** page (autonome : styles, papillon et script inclus, rien à
  charger). Servie par Caddy telle quelle.
- `infra/maintenance/worker.src.js` : le code du Worker. `worker.js` est **fabriqué** à partir des deux par
  `python3 infra/maintenance/assembler.py` (à relancer après toute modification ; un test vérifie qu'il est à jour).
- Tests : `node --test` dans `infra/maintenance` (Worker), `app/tests/test_maintenance.py` (page, Caddy, en-tête).

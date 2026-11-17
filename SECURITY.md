# Sécurité

Tu as trouvé une faille dans Claunote ? **N'ouvre pas d'issue publique.**
Signale-la en privé par l'onglet **Security → Report a vulnerability** de ce dépôt
(GitHub « Private vulnerability reporting »). Merci !

Rappels pour ceux qui l'installent :

- change le mot de passe par défaut (`admin` / `claunote`) dès la première connexion (Réglages → Compte) ;
- n'expose jamais le port 8000 directement sur Internet : VPN, ou Cloudflare Tunnel avec appareils approuvés
  (voir `docs/cloudflare-tunnel.md`) ;
- ne commite jamais ton `.env` (il est dans `.gitignore`).

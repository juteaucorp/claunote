# Service « maj » : met à jour Claunote quand on le demande depuis le site (Réglages → Mise à jour).
# Docker + Compose pour reconstruire, git pour récupérer le code, jq pour l'état lu par le site.
FROM docker:27.3-cli
RUN apk add --no-cache git jq su-exec
COPY boucle.sh /boucle.sh
CMD ["sh", "/boucle.sh"]

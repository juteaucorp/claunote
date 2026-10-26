#!/bin/sh
# Une action demandée depuis le site. Écrit son avancement dans /maj/etat.json (lu par le site)
# et son détail sur la sortie (/maj/journal.log).
action="$1"
ETAT=/maj/etat.json
PROPRIO=$(stat -c %u:%g "$DEPOT")   # propriétaire du dossier Claunote sur la VM
# git tourne en root : il passe même si des fichiers de .git appartiennent à root (un ancien « sudo git pull »)
# ou à un autre utilisateur. Ensuite, rendre() redonne tout au propriétaire du dossier.
# safe.directory : sans ça, git refuse un dépôt qui n'appartient pas à root.
printf '[safe]\n\tdirectory = *\n' > /tmp/.gitconfig
git_() { HOME=/tmp git "$@"; }
rendre() {  # .git et les fichiers suivis par git (jamais .env) reviennent au propriétaire du dossier
  chown -R "$PROPRIO" "$DEPOT/.git"
  git_ ls-files -z | xargs -0 -r chown -h "$PROPRIO" 2>/dev/null
  return 0
}
maintenant() { date -Iseconds; }
ecrire() {  # ecrire <filtre jq> [args jq...] : modifie l'état sans perdre le reste
  f="$1"; shift
  jq "$@" "$f" "$ETAT" > "$ETAT.tmp" 2>/dev/null || jq -n "$@" "{} | $f" > "$ETAT.tmp"
  mv "$ETAT.tmp" "$ETAT"
}
fin() {  # fin <ok|erreur> <message>
  ecrire '.statut=$s | .message=$m | .fin=$t' --arg s "$1" --arg m "$2" --arg t "$(maintenant)"
  echo "== $2"
  [ "$1" = ok ] || exit 1
}
services() { docker compose config --services | grep -vx maj; }   # tout sauf ce service-ci

decrire() {  # version installée et nouveautés disponibles sur GitHub
  if ! erreur=$(git_ fetch --quiet origin 2>&1); then
    echo "$erreur"; rendre
    raison=$(echo "$erreur" | tail -1); return 1
  fi
  amont=$(git_ rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo origin/main)
  nouveautes=$(git_ log --format='%h%x09%cI%x09%s' "HEAD..$amont" | head -50 \
    | jq -R 'split("\t") | {id: .[0], le: .[1], titre: .[2]}' | jq -s .)
  ecrire '.version={id: $id, le: $le, titre: $titre, branche: $b} | .nouveautes=$n | .retard=($n|length)
          | .modifs_locales=$ml | .verifie=$t' \
    --arg id "$(git_ rev-parse --short HEAD)" --arg le "$(git_ log -1 --format=%cI)" \
    --arg titre "$(git_ log -1 --format=%s)" --arg b "$(git_ rev-parse --abbrev-ref HEAD)" \
    --argjson n "$nouveautes" --argjson ml "$(git_ status --porcelain --untracked-files=no | wc -l | tr -d ' ')" \
    --arg t "$(maintenant)"
  rendre   # fetch et status ont pu écrire dans .git en root
}

ecrire '.action=$a | .statut="en_cours" | .debut=$t | .fin=null | .message=null' \
  --arg a "$action" --arg t "$(maintenant)"
echo "== $action ($(maintenant))"
case "$action" in
  verifier)
    decrire || fin erreur "Vérification impossible : $raison"
    fin ok "Vérifié." ;;
  mettre_a_jour)
    decrire || fin erreur "Récupération impossible, rien n'a été changé : $raison"
    avant=$(git_ rev-parse --short HEAD)
    git_ pull --ff-only; ok=$?; rendre
    [ $ok = 0 ] || fin erreur "git pull refusé, rien n'a été changé (fichier modifié à la main sur la VM ? voir le journal)."
    # Construire d'abord : le site reste en ligne pendant la construction, coupé seulement au redémarrage
    docker compose build $(services) || fin erreur "Échec de la construction : l'ancienne version tourne toujours."
    ecrire '.message="Redémarrage des services…"'
    docker compose up -d --remove-orphans $(services) || fin erreur "Échec du redémarrage (voir le journal)."
    docker image prune -f > /dev/null
    decrire
    fin ok "Mis à jour : $avant → $(git_ rev-parse --short HEAD)." ;;
  redemarrer)
    docker compose restart web worker || fin erreur "Échec du redémarrage (voir le journal)."
    fin ok "Site et worker redémarrés." ;;
  nettoyer)
    avant=$(df -h / | awk 'NR==2 {print $4}')
    docker image prune -f && docker builder prune -f
    fin ok "Nettoyé : espace libre $avant → $(df -h / | awk 'NR==2 {print $4}')." ;;
esac

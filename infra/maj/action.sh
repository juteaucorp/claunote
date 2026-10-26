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
# Dépôt privé : jeton GitHub lu dans le .env de la VM (GITHUB_TOKEN), donné à git par un assistant
# d'identification ; il n'est jamais écrit dans un fichier ni dans le journal.
GITHUB_TOKEN=$(sed -n 's/^GITHUB_TOKEN=//p' "$DEPOT/.env" 2>/dev/null | tail -1 | tr -d "\"' \r")
export GITHUB_TOKEN
if [ -n "$GITHUB_TOKEN" ]; then cat >> /tmp/.gitconfig <<'FIN'
[credential "https://github.com"]
	helper = "!f() { echo username=x-access-token; echo \"password=$GITHUB_TOKEN\"; }; f"
FIN
fi
git_() { HOME=/tmp GIT_TERMINAL_PROMPT=0 git "$@"; }
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
DEBUT=$(date +%s)
duree() { s=$(( $(date +%s) - ${1:-$DEBUT} )); [ $s -ge 60 ] && echo "$((s / 60)) min $((s % 60)) s" || echo "$s s"; }
etape() { echo "== $1 ($(duree))"; ecrire '.message=$m' --arg m "$1"; }

# Ce qui tourne vraiment : pour chaque service, la version du code avec laquelle il a été construit et
# (re)démarré la dernière fois, notée dans le volume maj. On compare le code à CETTE version, pas à celle
# d'avant le git pull : un « git pull » fait à la main ou une mise à jour qui avait échoué ne laissent
# plus l'ancien code tourner sous un « Mis à jour ».
MARQUES=/maj/construit
mkdir -p "$MARQUES"
CONSTRUITS="web worker lecture pronote"   # services construits depuis le dépôt
dossier_de() {
  case "$1" in web|worker) echo "app/" ;; lecture) echo "lecture/" ;; pronote) echo "pronote/" ;;
               caddy) echo "infra/caddy/" ;; esac
}
a_jour() {  # a_jour <service> : vrai si ce qui tourne correspond au code actuel du dépôt
  base=$(cat "$MARQUES/$1" 2>/dev/null) || return 1        # jamais noté : on ne sait pas, donc non
  git_ cat-file -e "$base^{commit}" 2>/dev/null || return 1
  changes=$(git_ diff --name-only "$base" HEAD)
  echo "$changes" | grep -qx "docker-compose.yml" && return 1
  ! echo "$changes" | grep -q "^$(dossier_de "$1")"
}
a_installer() {  # services dont la version en service est en retard sur le code de la VM
  for svc in $CONSTRUITS caddy; do a_jour "$svc" || printf '%s ' "$svc"; done
}
noter_installe() { for svc in $CONSTRUITS caddy; do git_ rev-parse HEAD > "$MARQUES/$svc"; done; }

decrire() {  # version installée et nouveautés disponibles sur GitHub
  if ! erreur=$(git_ fetch --quiet origin 2>&1); then
    echo "$erreur"; rendre
    raison=$(echo "$erreur" | tail -1)
    case "$erreur" in *Username*|*Authentication*|*"403"*|*"not found"*)
      raison="GitHub refuse l'accès (dépôt privé) : ajoute GITHUB_TOKEN dans le .env de la VM." ;; esac
    return 1
  fi
  amont=$(git_ rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo origin/main)
  nouveautes=$(git_ log --format='%h%x09%cI%x09%s' "HEAD..$amont" | head -50 \
    | jq -R 'split("\t") | {id: .[0], le: .[1], titre: .[2]}' | jq -s .)
  ecrire '.version={id: $id, le: $le, titre: $titre, branche: $b} | .nouveautes=$n | .retard=($n|length)
          | .modifs_locales=$ml | .verifie=$t | .a_installer=($ai | split(" ") | map(select(. != "")))' \
    --arg ai "$(a_installer)" \
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
    avant=$(git_ rev-parse HEAD)
    git_ pull --ff-only; ok=$?; rendre
    [ $ok = 0 ] || fin erreur "git pull refusé, rien n'a été changé (fichier modifié à la main sur la VM ? voir le journal)."
    apres=$(git_ rev-parse HEAD)
    construire="" ; relancer=""
    for svc in $CONSTRUITS; do a_jour "$svc" || construire="$construire $svc"; done
    a_jour caddy || relancer="caddy"   # Caddyfile changé : « up -d » ne le voit pas, il faut redémarrer
    if [ -n "$construire" ]; then
      # Construire d'abord : le site reste en ligne pendant la construction, coupé seulement au redémarrage
      etape "Construction :$construire"
      t=$(date +%s)
      docker compose build $construire || fin erreur "Échec de la construction : l'ancienne version tourne toujours."
      echo "== construit en $(duree $t)"
    else
      echo "== rien à reconstruire : ce qui tourne correspond déjà au code"
    fi
    etape "Redémarrage des services modifiés…"
    t=$(date +%s)
    # up -d ne recrée que les conteneurs dont l'image ou la configuration a changé
    docker compose up -d --remove-orphans $(services) || fin erreur "Échec du redémarrage (voir le journal)."
    if [ -n "$relancer" ]; then docker compose restart $relancer || fin erreur "Échec du redémarrage de $relancer."; fi
    echo "== redémarré en $(duree $t)"
    noter_installe   # seulement ici : une étape ratée plus haut sera refaite à la prochaine mise à jour
    docker image prune -f > /dev/null
    decrire
    installes=$(echo $construire $relancer)
    if [ "$avant" = "$apres" ]; then
      quoi="rien de neuf sur GitHub, version $(echo "$apres" | cut -c1-7) installée"
    else
      quoi="$(echo "$avant" | cut -c1-7) → $(echo "$apres" | cut -c1-7)"
    fi
    fin ok "Mis à jour en $(duree) : $quoi$([ -n "$installes" ] && echo " (reconstruits : $installes)")." ;;
  redemarrer)
    docker compose restart web worker || fin erreur "Échec du redémarrage (voir le journal)."
    fin ok "Site et worker redémarrés." ;;
  nettoyer)
    avant=$(df -h / | awk 'NR==2 {print $4}')
    # Le cache de construction récent est gardé : sans lui, la mise à jour suivante retélécharge tout
    docker image prune -f && docker builder prune -f --filter until=720h
    fin ok "Nettoyé : espace libre $avant → $(df -h / | awk 'NR==2 {print $4}')." ;;
esac

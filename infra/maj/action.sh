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
# Le dépôt a déménagé de ctrljuteau/claunote vers l'organisation juteaucorp : une VM clonée depuis
# l'ancienne adresse est repointée (même forme https ou ssh). Les forks gardent leur propre origin.
ancien=$(git_ -C "$DEPOT" remote get-url origin 2>/dev/null)
case "$ancien" in *github.com[:/]ctrljuteau/claunote*)
  git_ -C "$DEPOT" remote set-url origin "$(echo "$ancien" | sed 's#ctrljuteau/claunote#juteaucorp/claunote#')"
  chown "$PROPRIO" "$DEPOT/.git/config"
  echo "== dépôt GitHub repointé : $(git_ -C "$DEPOT" remote get-url origin)" ;;
esac
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
# Tunnel Cloudflare allumé depuis le site (Réglages → Configuration du site) : le profil « tunnel » est actif pour
# toutes les commandes docker compose (comme COMPOSE_PROFILES=tunnel dans .env), donc aussi aux mises à jour.
if [ -f /maj/tunnel_actif ]; then
  profils=$(sed -n 's/^COMPOSE_PROFILES=//p' "$DEPOT/.env" 2>/dev/null | tail -1 | tr -d "\"' \r")
  case ",$profils," in *,tunnel,*) ;; *) profils="${profils:+$profils,}tunnel" ;; esac
  export COMPOSE_PROFILES="$profils"
fi
appliquer_tunnel() {  # demande ponctuelle du site : (re)démarre cloudflared avec le nouveau jeton, ou l'arrête
  [ -f /maj/tunnel_relancer ] || return 0
  rm -f /maj/tunnel_relancer
  if [ -f /maj/tunnel_actif ]; then
    echo "== tunnel Cloudflare allumé depuis le site : (re)démarrage de cloudflared"
    docker compose up -d --force-recreate cloudflared
  else
    echo "== tunnel Cloudflare éteint depuis le site : arrêt de cloudflared"
    docker compose --profile tunnel stop cloudflared
  fi
  return 0
}
services() {   # tout sauf ce service-ci (et sauf les modèles locaux s'ils sont éteints)
  if [ -f /maj/locaux_eteints ]; then docker compose config --services | grep -vx -e maj -e ollama -e lecture
  else docker compose config --services | grep -vx maj; fi
}
DEBUT=$(date +%s)
duree() { s=$(( $(date +%s) - ${1:-$DEBUT} )); [ $s -ge 60 ] && echo "$((s / 60)) min $((s % 60)) s" || echo "$s s"; }
etape() { echo "== $1 ($(duree))"; ecrire '.message=$m' --arg m "$1"; }

# Ce qui tourne vraiment : pour chaque service, la version du code avec laquelle il a été construit et
# (re)démarré la dernière fois, notée dans le volume maj. On compare le code à CETTE version, pas à celle
# d'avant le git pull : un « git pull » fait à la main ou une mise à jour qui avait échoué ne laissent
# plus l'ancien code tourner sous un « Mis à jour ».
MARQUES=/maj/construit
mkdir -p "$MARQUES"
# Modèles locaux éteints depuis le site (page Analyses) : Ollama et lecture restent arrêtés, jamais reconstruits
LOCAUX="ollama lecture"
locaux_eteints() { [ -f /maj/locaux_eteints ]; }
if locaux_eteints; then CONSTRUITS="web worker pronote"; else CONSTRUITS="web worker lecture pronote"; fi
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

appliquer_locaux() {  # l'interrupteur du site : arrête les modèles locaux, ou les relance sur demande
  if locaux_eteints; then
    if [ -n "$(docker compose ps -q --status running $LOCAUX 2>/dev/null)" ]; then
      echo "== modèles locaux éteints depuis le site : arrêt de $LOCAUX"
      docker compose stop $LOCAUX
    fi
  elif [ -f /maj/locaux_allumer ]; then
    rm -f /maj/locaux_allumer
    echo "== modèles locaux rallumés depuis le site"
    if ! a_jour lecture; then   # le code a pu changer pendant qu'il était éteint
      docker compose build lecture && git_ rev-parse HEAD > "$MARQUES/lecture"
    fi
    docker compose up -d $LOCAUX
  fi
}

releve() {  # processeur et mémoire de chaque conteneur, pour la page Analyses
  conso=$(docker stats --no-stream --format '{{json .}}' 2>/dev/null \
    | jq -c '{nom: (.Name | sub("^[^-]+-"; "") | sub("-[0-9]+$"; "")), cpu: .CPUPerc, mem: (.MemUsage | split(" / ")[0])}' \
    | jq -s . 2>/dev/null) || return 0
  [ -n "$conso" ] && ecrire '.conso=$c | .conso_le=$t' --argjson c "$conso" --arg t "$(maintenant)"
  return 0
}

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

# Pendant l'action, la boucle du service ne signe plus sa présence (/maj/vu) : on la signe ici, dans un
# fichier à part, toutes les 10 s. Sans ça, le site croyait le service arrêté au bout d'une minute et
# proposait une commande à lancer à la main… en pleine mise à jour.
( while :; do date -Iseconds > /maj/action-vu; sleep 10; done ) &
BATTEMENT=$!
EN_PAUSE=""   # services mis en pause pour la construction : toujours relancés à la sortie, même après un échec
trap 'kill $BATTEMENT 2>/dev/null; rm -f /maj/action-vu; [ -n "$EN_PAUSE" ] && docker compose up -d $EN_PAUSE >/dev/null 2>&1' EXIT

ecrire '.action=$a | .statut="en_cours" | .debut=$t | .fin=null | .message=null' \
  --arg a "$action" --arg t "$(maintenant)"
echo "== $action ($(maintenant))"
appliquer_locaux
appliquer_tunnel
case "$action" in
  verifier)
    releve
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
      # Mémoire basse : Ollama (~2,3 Go) et le service lecture (~1,5 Go) en pause le temps de construire ;
      # « up -d » plus bas les relance. Une construction (pip, PyTorch) à côté de tout le reste peut sinon
      # saturer la VM.
      libre=$(awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo)
      if [ "${libre:-0}" -lt 3000 ] && ! locaux_eteints; then
        echo "== mémoire libre : $libre Mo → Ollama et le service lecture en pause pendant la construction"
        docker compose stop ollama lecture >/dev/null 2>&1 && EN_PAUSE="ollama lecture"
      fi
      # Construire d'abord : le site reste en ligne pendant la construction, coupé seulement au redémarrage.
      # Un service à la fois : plusieurs constructions en parallèle multiplient la mémoire utilisée.
      t=$(date +%s)
      for svc in $construire; do
        etape "Construction de $svc…"
        docker compose build "$svc" || fin erreur "Échec de la construction de $svc : l'ancienne version tourne toujours."
      done
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
    # Anciennes images et cache de construction de plus de 30 jours : rendus à chaque mise à jour, sans attendre
    # « Libérer de la place » (le cache récent est gardé : la mise à jour suivante ne retélécharge rien)
    docker image prune -f > /dev/null
    docker builder prune -f --filter until=720h > /dev/null 2>&1
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

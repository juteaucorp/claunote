#!/bin/sh
# Attend les demandes du site, déposées dans le volume partagé /maj (un fichier « demande »).
# Seules les actions de la liste sont exécutées : le site ne peut pas lancer d'autre commande.
# Le script des actions est relu dans le dépôt à chaque fois : il suit les mises à jour du code.
cd "$DEPOT" || { echo "dépôt introuvable : $DEPOT"; exit 1; }
derniere=0
lancer() {
  cp "$DEPOT/infra/maj/action.sh" /tmp/action.sh  # copie : git pull peut réécrire le fichier pendant l'action
  sh /tmp/action.sh "$1" > /maj/journal.log 2>&1
}
while true; do
  date -Iseconds > /maj/vu
  if [ -f /maj/demande ]; then
    action=$(head -c 40 /maj/demande | tr -cd 'a-z_')
    rm -f /maj/demande
    case "$action" in
      verifier|mettre_a_jour|redemarrer|nettoyer) lancer "$action"; derniere=$(date +%s) ;;
      *) echo "action refusée : $action" ;;
    esac
  elif [ $(( $(date +%s) - derniere )) -ge 21600 ]; then
    lancer verifier; derniere=$(date +%s)   # toutes les 6 h : y a-t-il du nouveau sur GitHub ?
  fi
  sleep 3
done

// Un Pronote par compte Claunote : chaque compte a son fichier de jeton (compte.json pour l'admin d'origine,
// compte-<id>.json pour les autres, à côté) et sa propre file d'opérations.
import { basename, dirname, extname, join } from "node:path";
import { creerCompte, ErreurPronote } from "./pronote.mjs";

/** Fichier du jeton d'un compte. Compte 1 (ou absent) : le fichier d'origine, inchangé. */
export function fichierDuCompte(base, id) {
  if (id === undefined || id === null || id === "" || String(id) === "1") return base;
  if (!/^[1-9]\d{0,8}$/u.test(String(id))) throw new ErreurPronote("Compte invalide.", 400);
  const ext = extname(base);
  return join(dirname(base), `${basename(base, ext)}-${id}${ext}`);
}

/** Les comptes Pronote, créés à la demande. */
export function creerComptes({ pawnote, fichier, ...options }) {
  const comptes = new Map();
  return {
    pour(id) {
      const f = fichierDuCompte(fichier, id);
      if (!comptes.has(f)) comptes.set(f, creerCompte({ pawnote, fichier: f, ...options }));
      return comptes.get(f);
    },
  };
}

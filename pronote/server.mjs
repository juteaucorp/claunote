// Service HTTP interne (réseau Docker uniquement) entre Claunote et Pronote.
import { createServer } from "node:http";
// Fork maintenu de Pawnote (archivé) : gère le nouveau défi de connexion de Pronote 2026
import * as pawnote from "@blockshub/pawnote-lts";
import { creerComptes } from "./comptes.mjs";
import { ErreurPronote, traduire } from "./pronote.mjs";

// Un Pronote par compte Claunote (?compte=<id> ; absent : l'admin d'origine)
const comptes = creerComptes({ pawnote, fichier: process.env.ETAT_FICHIER ?? "./compte.json" });

const ROUTES = {
  "GET /etat": (compte) => compte.etat(),
  "POST /connexion/qr": (compte, corps) => compte.connexionQr(corps),
  "POST /connexion/identifiants": (compte, corps) => compte.connexionIdentifiants(corps),
  "POST /deconnexion": (compte) => compte.deconnexion().then(() => ({ ok: true })),
  "POST /synchro": (compte, corps) => compte.synchro({ jours: Math.min(Number(corps.jours) || 28, 120) }),
  "POST /verification": (compte, corps) => compte.verification({ jours: Math.min(Number(corps.jours) || 8, 14) }),
  "POST /documents": (compte, corps) => compte.documents({ deja: Array.isArray(corps.deja) ? corps.deja : [] }),
};

async function lireCorps(req) {
  let texte = "";
  for await (const morceau of req) texte += morceau;
  try {
    return texte ? JSON.parse(texte) : {};
  } catch {
    throw new ErreurPronote("Corps JSON invalide.");
  }
}

export const serveur = createServer(async (req, res) => {
  const adresse = new URL(req.url, "http://pronote");
  const route = ROUTES[`${req.method} ${adresse.pathname}`];
  const repondre = (statut, donnees) => {
    res.writeHead(statut, { "content-type": "application/json; charset=utf-8" });
    res.end(JSON.stringify(donnees));
  };
  if (!route) return repondre(404, { erreur: "route inconnue" });
  try {
    const compte = comptes.pour(adresse.searchParams.get("compte"));
    repondre(200, (await route(compte, await lireCorps(req))) ?? {});
  } catch (e) {
    const err = traduire(e);
    const origine = err.cause ?? (e instanceof ErreurPronote ? null : e);
    console.error(`${req.method} ${req.url} : ${err.message}${origine ? `\n${origine.stack ?? origine}` : ""}`);
    repondre(err.statut, { erreur: err.message });
  }
});

const port = Number(process.env.PORT ?? 3000);
serveur.listen(port, () => console.log(`pronote : à l'écoute sur le port ${port}`));

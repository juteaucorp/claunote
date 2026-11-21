// Service HTTP interne (réseau Docker uniquement) entre Claunote et Pronote.
import { createServer } from "node:http";
// Fork maintenu de Pawnote (archivé) : gère le nouveau défi de connexion de Pronote 2026
import * as pawnote from "@blockshub/pawnote-lts";
import { creerCompte, ErreurPronote, traduire } from "./pronote.mjs";

const compte = creerCompte({ pawnote, fichier: process.env.ETAT_FICHIER ?? "./compte.json" });

const ROUTES = {
  "GET /etat": () => compte.etat(),
  "POST /connexion/qr": (corps) => compte.connexionQr(corps),
  "POST /connexion/identifiants": (corps) => compte.connexionIdentifiants(corps),
  "POST /deconnexion": () => compte.deconnexion().then(() => ({ ok: true })),
  "POST /synchro": (corps) => compte.synchro({ jours: Math.min(Number(corps.jours) || 28, 120) }),
  "POST /verification": (corps) => compte.verification({ jours: Math.min(Number(corps.jours) || 8, 14) }),
  "POST /documents": (corps) => compte.documents({ deja: Array.isArray(corps.deja) ? corps.deja : [] }),
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
  const route = ROUTES[`${req.method} ${req.url}`];
  const repondre = (statut, donnees) => {
    res.writeHead(statut, { "content-type": "application/json; charset=utf-8" });
    res.end(JSON.stringify(donnees));
  };
  if (!route) return repondre(404, { erreur: "route inconnue" });
  try {
    repondre(200, (await route(await lireCorps(req))) ?? {});
  } catch (e) {
    const err = traduire(e);
    const origine = err.cause ?? (e instanceof ErreurPronote ? null : e);
    console.error(`${req.method} ${req.url} : ${err.message}${origine ? `\n${origine.stack ?? origine}` : ""}`);
    repondre(err.statut, { erreur: err.message });
  }
});

const port = Number(process.env.PORT ?? 3000);
serveur.listen(port, () => console.log(`pronote : à l'écoute sur le port ${port}`));

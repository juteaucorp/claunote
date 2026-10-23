// Connexion à Pronote (via Pawnote) et extraction de ce dont Claunote a besoin :
// évaluations à venir, devoirs, notes. Le mot de passe n'est jamais gardé :
// seul le jeton de reconnexion de Pronote est stocké, et il change à chaque connexion.
import { randomUUID } from "node:crypto";
import { readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

const JOUR = 24 * 60 * 60 * 1000;
const MAX_REDIRECTIONS = 5;

const UA_BUREAU = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0";
const MARQUE_VERSION = />PRONOTE \d+\.\d+\.\d+/u; // ce que Pawnote cherche dans la page
const AUTRES_VERSIONS = [
  /PRONOTE(?:\s|&nbsp;|\u00A0)*(?:-|–)?(?:\s|&nbsp;|\u00A0)*(\d{4}\.\d+\.\d+)/u,
  /["']?version["']?\s*[:=]\s*["']?(\d{4}\.\d+\.\d+)/iu,
  /\b(20\d{2}\.\d{1,2}\.\d{1,3})\b/u,
];

const titre = (html) =>
  (html.match(/<title[^>]*>([^<]*)<\/title>/iu)?.[1] ?? "").replace(/\s+/gu, " ").trim();

const apercu = (html) =>
  html.replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/giu, " ").replace(/<[^>]+>/gu, " ")
    .replace(/\s+/gu, " ").trim().slice(0, 300);

/**
 * Page d'accueil Pronote telle que Pawnote sait la lire, ou null.
 * Pawnote attend « >PRONOTE x.y.z » et un appel « Start(…) » ; certaines versions de Pronote
 * affichent la version autrement : on ajoute alors la marque attendue en fin de page.
 */
export function pageLisible(html) {
  if (!html.includes("Start")) return null;
  // Pawnote lit ce qu'il y a entre « Start( » et « )}catch » (espaces retirés). Depuis Pronote
  // 2026 l'appel finit par « Start ({…});} catch » : on remet la forme attendue.
  html = html.replace(/Start\s*\((\s*\{[\s\S]*?\}\s*)\)\s*;\s*\}\s*catch/u, "Start($1)} catch");
  if (MARQUE_VERSION.test(html)) return html;
  for (const motif of AUTRES_VERSIONS) {
    const version = html.match(motif)?.[1];
    if (version) return `${html}\n<!-- claunote --><span>>PRONOTE ${version}</span>`;
  }
  return null;
}

/**
 * Fetcher passé à Pawnote, qui corrige ce qui la fait échouer sur certains serveurs :
 * - Pawnote ne suit pas les redirections : on suit celles des GET (cookies compris), comme
 *   pronotepy / Home Assistant ;
 * - la page d'accueil n'est pas reconnue (version absente, page « mets à jour l'application »
 *   servie au user-agent de l'appli mobile) : on retente avec un navigateur de bureau, comme
 *   pronotepy, et si rien ne marche on garde la page dans `diagnostic` pour comprendre.
 */
export function creerFetcher({ fetch: f = globalThis.fetch, journal = console.log, diagnostic = null } = {}) {
  let uaBureau = false;

  async function echange(url, methode, entetes, corps) {
    for (let saut = 0; ; saut++) {
      const r = await f(url.href, { redirect: "manual", headers: entetes, method: methode, body: corps });
      const lieu = r.headers.get("location");
      // Les cookies posés pendant une redirection suivent (session de l'établissement)
      const poses = r.headers.getSetCookie?.() ?? [];
      if (poses.length) {
        const cle = Object.keys(entetes).find((k) => k.toLowerCase() === "cookie") ?? "Cookie";
        entetes[cle] = [entetes[cle], ...poses.map((c) => c.split(";")[0])].filter(Boolean).join("; ");
      }
      fetcher.dernier = { methode, url: `${url.origin}${url.pathname}`, statut: r.status, lieu };
      if (r.status >= 300 && r.status < 400 && lieu && methode === "GET" && saut < MAX_REDIRECTIONS) {
        journal(`pronote : ${url.origin}${url.pathname} → ${r.status} vers ${lieu}`);
        url = new URL(lieu, url);
        continue;
      }
      if (r.status >= 400) journal(`pronote : ${methode} ${url.origin}${url.pathname} → ${r.status}`);
      return { status: r.status, content: await r.text(), headers: r.headers };
    }
  }

  const avecUa = (entetes) => {
    if (!uaBureau) return entetes;
    const cle = Object.keys(entetes).find((k) => k.toLowerCase() === "user-agent") ?? "User-Agent";
    return { ...entetes, [cle]: UA_BUREAU };
  };

  async function fetcher(req) {
    const url = new URL(req.url.href ?? req.url);
    const methode = req.method ?? "GET";
    const entetes = { ...(req.headers instanceof Headers ? Object.fromEntries(req.headers) : req.headers) };
    let r = await echange(url, methode, avecUa(entetes), req.content);

    const accueil = methode === "GET" && /\/mobile\.[a-z]+\.html$/u.test(url.pathname);
    if (!accueil || r.status !== 200) return r;
    let lisible = pageLisible(r.content);
    if (!lisible && !uaBureau) {
      journal(`pronote : page d'accueil non reconnue (« ${titre(r.content)} »), nouvel essai en navigateur de bureau`);
      uaBureau = true;
      const r2 = await echange(url, methode, avecUa(entetes), req.content);
      if (r2.status === 200 && pageLisible(r2.content)) r = r2;
      else uaBureau = false;
      lisible = pageLisible(r.content);
    }
    if (lisible) return { ...r, content: lisible };

    fetcher.dernier = { ...fetcher.dernier, titre: titre(r.content) || apercu(r.content).slice(0, 80) };
    journal(`pronote : page d'accueil toujours non reconnue. Titre : « ${titre(r.content)} ». Début : ${apercu(r.content)}`);
    if (diagnostic) {
      try {
        await writeFile(diagnostic, r.content);
        journal(`pronote : page complète gardée dans ${diagnostic}`);
      } catch { /* diagnostic facultatif */ }
    }
    return r;
  }
  fetcher.dernier = null;
  return fetcher;
}

const MESSAGES = {
  BadCredentialsError: "Pronote a refusé la connexion : identifiants, code PIN ou QR code incorrects (ou expirés).",
  SecurityError: "Pronote demande une double authentification : connecte-toi plutôt avec le QR code.",
  AccountDisabledError: "Ce compte Pronote est désactivé.",
  SuspendedIPError: "Pronote a bloqué l'adresse IP du serveur pour un moment (trop d'essais).",
  RateLimitedError: "Pronote limite les requêtes : réessaie dans quelques minutes.",
  BusyPageError: "Pronote est indisponible pour le moment (maintenance ?).",
  PageUnavailableError: "Pronote n'a pas renvoyé sa page (adresse incorrecte, ou redirection vers l'ENT).",
  UnreachableError: "Impossible de joindre le serveur Pronote.",
};

export class ErreurPronote extends Error {
  constructor(message, statut = 400) {
    super(message);
    this.statut = statut;
  }
}

export const traduire = (e, fetcher = null) => {
  if (e instanceof ErreurPronote) return e;
  let message = MESSAGES[e?.name] ?? `Erreur Pronote : ${e?.message ?? e}`;
  const d = fetcher?.dernier;
  if (d && ["PageUnavailableError", "BusyPageError"].includes(e?.name)) {
    message += ` (dernière réponse : ${d.methode} ${d.url} → HTTP ${d.statut}${d.lieu ? `, redirige vers ${d.lieu}` : ""}`
      + `${d.titre ? `, page « ${d.titre} »` : ""})`;
  }
  const err = new ErreurPronote(message, 502);
  err.cause = e; // gardé pour les logs du service
  return err;
};

/**
 * Contenu du QR code Pronote → { avecPageConnexion, jeton, login, url }.
 * Tolère ce qu'ajoute un copier-coller : guillemets typographiques, espaces, texte autour.
 */
export function lireQr(qr) {
  if (qr && typeof qr === "object") return verifierQr(qr);
  let texte = String(qr ?? "")
    .replace(/[\u200B-\u200D\uFEFF]/gu, "")
    .replace(/[\u201C\u201D\u201E\u00AB\u00BB]/gu, '"')
    .trim();
  if (!texte.startsWith("{") && /%7B/iu.test(texte)) {
    try {
      texte = decodeURIComponent(texte);
    } catch { /* on garde tel quel */ }
  }
  const debut = texte.indexOf("{"), fin = texte.lastIndexOf("}");
  try {
    return verifierQr(JSON.parse(texte.slice(debut, fin + 1)));
  } catch (e) {
    if (e instanceof ErreurPronote) throw e;
    throw new ErreurPronote("Ce QR code n'est pas un QR code de connexion Pronote (contenu illisible).");
  }
}

function verifierQr(donnees) {
  const manquants = ["jeton", "login", "url"].filter((cle) => typeof donnees[cle] !== "string" || !donnees[cle].trim());
  if (manquants.length) {
    throw new ErreurPronote(`Ce QR code n'est pas un QR code de connexion Pronote (il manque : ${manquants.join(", ")}).`);
  }
  if (!/\/mobile\.(eleve|parent|professeur)\.html/u.test(donnees.url)) {
    throw new ErreurPronote("Ce QR code ne pointe pas vers l'espace élève mobile de Pronote.");
  }
  return { ...donnees, jeton: donnees.jeton.trim(), login: donnees.login.trim(), url: donnees.url.trim() };
}

const iso = (d) => (d instanceof Date ? d.toISOString() : null);
const note = (v) => (v ? { genre: v.kind, points: v.points } : null);

/** Ce que la VM garde de la session : de quoi se reconnecter sans mot de passe. */
const memoriser = (refresh, deviceUUID, session) => ({
  deviceUUID,
  refresh: {
    url: refresh.url, token: refresh.token, username: refresh.username,
    kind: refresh.kind, navigatorIdentifier: refresh.navigatorIdentifier,
  },
  profil: {
    nom: session.userResource?.name ?? refresh.username,
    classe: session.userResource?.className ?? null,
    etablissement: session.userResource?.establishmentName ?? null,
  },
  expire: false,
  maj_le: new Date().toISOString(),
});

export function extraireDevoirs(devoirs) {
  return devoirs.map((d) => ({
    id: d.id,
    matiere: d.subject?.name ?? "",
    description: d.description ?? "",
    pour_le: iso(d.deadline),
    fait: !!d.done,
    themes: (d.themes ?? []).map((t) => t.name),
  }));
}

export function extraireControles(edt) {
  return edt.classes
    .filter((c) => c.is === "lesson" && c.test && !c.canceled)
    .map((c) => ({
      id: c.id,
      matiere: c.subject?.name ?? "",
      debut: iso(c.startDate),
      fin: iso(c.endDate),
      profs: c.teacherNames ?? [],
      salles: c.classrooms ?? [],
      notes: c.notes ?? "",
    }));
}

export function extraireNotes(apercu, periode) {
  return {
    periode: periode?.name ?? null,
    moyenne: note(apercu.overallAverage),
    notes: apercu.grades.map((g) => ({
      id: g.id,
      matiere: g.subject?.name ?? "",
      date: iso(g.date),
      valeur: note(g.value),
      sur: note(g.outOf),
      moyenne_classe: note(g.average),
      coefficient: g.coefficient,
      commentaire: g.comment ?? "",
    })),
  };
}

/**
 * Compte Pronote unique de Claunote, persisté dans `fichier`.
 * `pawnote` est injecté pour pouvoir tester sans serveur Pronote.
 */
export function creerCompte({ pawnote, fichier, fetch = globalThis.fetch, journal = console.log }) {
  let file = Promise.resolve(); // une seule opération à la fois : le jeton change à chaque connexion
  const enFile = (f) => (file = file.then(f, f));
  const nouvelleSession = () => {
    const fetcher = creerFetcher({ fetch, journal, diagnostic: join(dirname(fichier), "page-pronote.html") });
    return { session: pawnote.createSessionHandle(fetcher), fetcher };
  };

  const lire = async () => {
    try {
      return JSON.parse(await readFile(fichier, "utf8"));
    } catch {
      return null;
    }
  };
  const ecrire = (etat) => writeFile(fichier, JSON.stringify(etat, null, 2), { mode: 0o600 });

  async function ouvrir() {
    const etat = await lire();
    if (!etat) throw new ErreurPronote("Pronote n'est pas connecté.", 409);
    if (etat.expire) throw new ErreurPronote("La connexion Pronote a expiré : reconnecte-toi.", 409);
    const { session, fetcher } = nouvelleSession();
    try {
      const refresh = await pawnote.loginToken(session, { ...etat.refresh, deviceUUID: etat.deviceUUID });
      await ecrire(memoriser(refresh, etat.deviceUUID, session));
    } catch (e) {
      if (e?.name === "BadCredentialsError") await ecrire({ ...etat, expire: true });
      throw traduire(e, fetcher);
    }
    return session;
  }

  async function section(nom, f, erreurs) {
    try {
      return await f();
    } catch (e) {
      erreurs.push(`${nom} : ${traduire(e).message}`);
      return null;
    }
  }

  return {
    etat: () => enFile(async () => {
      const etat = await lire();
      if (!etat) return { connecte: false };
      return { connecte: !etat.expire, expire: !!etat.expire, url: etat.refresh.url, ...etat.profil, maj_le: etat.maj_le };
    }),

    connexionQr: ({ qr, pin }) => enFile(async () => {
      const donnees = lireQr(qr);
      pin = String(pin ?? "").trim();
      if (!/^\d{4}$/.test(pin)) throw new ErreurPronote("Le code PIN fait 4 chiffres.");
      const deviceUUID = randomUUID();
      const { session, fetcher } = nouvelleSession();
      try {
        const refresh = await pawnote.loginQrCode(session, { deviceUUID, pin, qr: donnees });
        const etat = memoriser(refresh, deviceUUID, session);
        await ecrire(etat);
        return etat.profil;
      } catch (e) {
        throw traduire(e, fetcher);
      }
    }),

    connexionIdentifiants: ({ url, username, password }) => enFile(async () => {
      if (!url || !username || !password) throw new ErreurPronote("Adresse, identifiant et mot de passe sont requis.");
      const deviceUUID = randomUUID();
      const { session, fetcher } = nouvelleSession();
      try {
        const refresh = await pawnote.loginCredentials(session, {
          url, username, password, deviceUUID, kind: pawnote.AccountKind.STUDENT,
        });
        const etat = memoriser(refresh, deviceUUID, session);
        await ecrire(etat);
        return etat.profil;
      } catch (e) {
        throw traduire(e, fetcher);
      }
    }),

    deconnexion: () => enFile(() => rm(fichier, { force: true })),

    /** Évaluations et devoirs des `jours` prochains jours, notes de la période en cours. */
    synchro: ({ jours = 28, maintenant = new Date() } = {}) => enFile(async () => {
      const session = await ouvrir();
      const debut = new Date(maintenant);
      debut.setHours(0, 0, 0, 0);
      const fin = new Date(debut.getTime() + jours * JOUR);
      const erreurs = [];

      const devoirs = await section("devoirs", async () =>
        extraireDevoirs(await pawnote.assignmentsFromIntervals(session, debut, fin)), erreurs);

      const controles = await section("emploi du temps", async () => {
        const edt = await pawnote.timetableFromIntervals(session, debut, fin);
        pawnote.parseTimetable(session, edt, {
          withSuperposedCanceledClasses: false, withCanceledClasses: false, withPlannedClasses: true,
        });
        return extraireControles(edt);
      }, erreurs);

      const notes = await section("notes", async () => {
        const onglet = session.userResource.tabs.get(pawnote.TabLocation.Grades);
        if (!onglet) return null;
        const periodes = onglet.periods ?? [];
        const periode = periodes.find((p) => p.startDate <= maintenant && maintenant <= p.endDate)
          ?? onglet.defaultPeriod ?? periodes.at(-1);
        if (!periode) return null;
        return extraireNotes(await pawnote.gradesOverview(session, periode), periode);
      }, erreurs);

      return { devoirs, controles, notes, erreurs, du: iso(debut), au: iso(fin) };
    }),
  };
}

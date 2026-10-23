// Connexion à Pronote (via Pawnote) et extraction de ce dont Claunote a besoin :
// évaluations à venir, devoirs, notes. Le mot de passe n'est jamais gardé :
// seul le jeton de reconnexion de Pronote est stocké, et il change à chaque connexion.
import { randomUUID } from "node:crypto";
import { readFile, rm, writeFile } from "node:fs/promises";

const JOUR = 24 * 60 * 60 * 1000;

const MESSAGES = {
  BadCredentialsError: "Pronote a refusé la connexion : identifiants, code PIN ou QR code incorrects (ou expirés).",
  SecurityError: "Pronote demande une double authentification : connecte-toi plutôt avec le QR code.",
  AccountDisabledError: "Ce compte Pronote est désactivé.",
  SuspendedIPError: "Pronote a bloqué l'adresse IP du serveur pour un moment (trop d'essais).",
  RateLimitedError: "Pronote limite les requêtes : réessaie dans quelques minutes.",
  BusyPageError: "Pronote est indisponible pour le moment (maintenance ?).",
  PageUnavailableError: "Pronote est indisponible pour le moment.",
  UnreachableError: "Impossible de joindre le serveur Pronote.",
};

export class ErreurPronote extends Error {
  constructor(message, statut = 400) {
    super(message);
    this.statut = statut;
  }
}

export const traduire = (e) => {
  if (e instanceof ErreurPronote) return e;
  const err = new ErreurPronote(MESSAGES[e?.name] ?? `Erreur Pronote : ${e?.message ?? e}`, 502);
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
export function creerCompte({ pawnote, fichier }) {
  let file = Promise.resolve(); // une seule opération à la fois : le jeton change à chaque connexion
  const enFile = (f) => (file = file.then(f, f));

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
    const session = pawnote.createSessionHandle();
    try {
      const refresh = await pawnote.loginToken(session, { ...etat.refresh, deviceUUID: etat.deviceUUID });
      await ecrire(memoriser(refresh, etat.deviceUUID, session));
    } catch (e) {
      if (e?.name === "BadCredentialsError") await ecrire({ ...etat, expire: true });
      throw traduire(e);
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
      const session = pawnote.createSessionHandle();
      try {
        const refresh = await pawnote.loginQrCode(session, { deviceUUID, pin, qr: donnees });
        const etat = memoriser(refresh, deviceUUID, session);
        await ecrire(etat);
        return etat.profil;
      } catch (e) {
        throw traduire(e);
      }
    }),

    connexionIdentifiants: ({ url, username, password }) => enFile(async () => {
      if (!url || !username || !password) throw new ErreurPronote("Adresse, identifiant et mot de passe sont requis.");
      const deviceUUID = randomUUID();
      const session = pawnote.createSessionHandle();
      try {
        const refresh = await pawnote.loginCredentials(session, {
          url, username, password, deviceUUID, kind: pawnote.AccountKind.STUDENT,
        });
        const etat = memoriser(refresh, deviceUUID, session);
        await ecrire(etat);
        return etat.profil;
      } catch (e) {
        throw traduire(e);
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

import assert from "node:assert/strict";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { creerCompte } from "../pronote.mjs";

const maths = { id: "s1", name: "MATHEMATIQUES", inGroups: false };
const reseau = { id: "s2", name: "SISR - RESEAUX", inGroups: false };

class BadCredentialsError extends Error {
  name = "BadCredentialsError";
}

/** Faux Pawnote : même API, données fixes, jetons qui tournent. */
function fauxPawnote({ refuser = false } = {}) {
  let n = 0;
  const appels = [];
  const session = () => ({
    userResource: {
      name: "JUTEAU Anto", className: "BTS SIO 2", establishmentName: "Lycée Test",
      tabs: new Map([[198, {
        periods: [
          { id: "p1", name: "Semestre 1", startDate: new Date("2026-09-01"), endDate: new Date("2027-01-31") },
          { id: "p2", name: "Semestre 2", startDate: new Date("2027-02-01"), endDate: new Date("2027-07-01") },
        ],
      }]]),
    },
  });
  const refresh = (username) => ({
    url: "https://0000000a.index-education.net/pronote", token: `jeton-${++n}`,
    username, kind: 6, navigatorIdentifier: "nav",
  });
  return {
    appels,
    AccountKind: { STUDENT: 6 },
    TabLocation: { Grades: 198 },
    createSessionHandle: session,
    async loginQrCode(s, info) {
      appels.push(["qr", info]);
      if (refuser) throw new BadCredentialsError();
      return refresh(info.qr.login);
    },
    async loginCredentials(s, auth) {
      appels.push(["identifiants", auth]);
      return refresh(auth.username);
    },
    async loginToken(s, auth) {
      appels.push(["jeton", auth]);
      if (refuser || auth.token === "perime") throw new BadCredentialsError();
      return refresh(auth.username);
    },
    async assignmentsFromIntervals(s, debut, fin) {
      appels.push(["devoirs", debut, fin]);
      return [{
        id: "d1", subject: maths, description: "<p>Réviser les <b>suites</b> pour le DS</p>",
        deadline: new Date("2026-10-27T07:00:00Z"), done: false, themes: [{ name: "Suites" }],
      }];
    },
    async timetableFromIntervals() {
      return {
        classes: [
          { is: "lesson", id: "c1", subject: reseau, test: true, canceled: false, startDate: new Date("2026-10-28T08:00:00Z"),
            endDate: new Date("2026-10-28T10:00:00Z"), teacherNames: ["M. Prof"], classrooms: ["B12"] },
          { is: "lesson", id: "c2", subject: maths, test: false, canceled: false, startDate: new Date(), endDate: new Date() },
          { is: "lesson", id: "c3", subject: maths, test: true, canceled: true, startDate: new Date(), endDate: new Date() },
          { is: "activity", id: "a1", title: "Sortie" },
        ],
      };
    },
    parseTimetable() {},
    async gradesOverview(s, periode) {
      appels.push(["notes", periode.id]);
      return {
        overallAverage: { kind: 0, points: 14.2 },
        grades: [{
          id: "g1", subject: maths, date: new Date("2026-10-10T00:00:00Z"), value: { kind: 0, points: 15 },
          outOf: { kind: 0, points: 20 }, average: { kind: 0, points: 11.5 }, coefficient: 2, comment: "DS suites",
        }],
      };
    },
  };
}

const QR = JSON.stringify({ jeton: "abc", login: "def", url: "https://0000000a.index-education.net/pronote/mobile.eleve.html" });

async function nouveau(options) {
  const dossier = await mkdtemp(join(tmpdir(), "pronote-"));
  const pawnote = fauxPawnote(options);
  const fichier = join(dossier, "compte.json");
  return { compte: creerCompte({ pawnote, fichier }), pawnote, fichier };
}

test("pas connecté au départ, synchro refusée", async () => {
  const { compte } = await nouveau();
  assert.deepEqual(await compte.etat(), { connecte: false });
  await assert.rejects(compte.synchro(), /pas connecté/);
});

test("connexion QR : profil gardé, jeton stocké sans mot de passe, fichier privé", async () => {
  const { compte, fichier } = await nouveau();
  const profil = await compte.connexionQr({ qr: QR, pin: "1234" });
  assert.equal(profil.classe, "BTS SIO 2");
  const etat = JSON.parse(await readFile(fichier, "utf8"));
  assert.equal(etat.refresh.token, "jeton-1");
  assert.equal(etat.refresh.password, undefined);
  assert.equal((await stat(fichier)).mode & 0o777, 0o600);
  const e = await compte.etat();
  assert.equal(e.connecte, true);
  assert.equal(e.nom, "JUTEAU Anto");
});

test("QR invalide ou PIN invalide refusés avant d'appeler Pronote", async () => {
  const { compte, pawnote } = await nouveau();
  await assert.rejects(compte.connexionQr({ qr: "bonjour", pin: "1234" }), /QR code/);
  await assert.rejects(compte.connexionQr({ qr: QR, pin: "12" }), /PIN/);
  assert.equal(pawnote.appels.length, 0);
});

test("QR refusé par Pronote : message en français", async () => {
  const { compte } = await nouveau({ refuser: true });
  await assert.rejects(compte.connexionQr({ qr: QR, pin: "1234" }), /refusé la connexion/);
});

test("synchro : jeton renouvelé, contrôles, devoirs et notes de la période en cours", async () => {
  const { compte, pawnote, fichier } = await nouveau();
  await compte.connexionQr({ qr: QR, pin: "1234" });
  const r = await compte.synchro({ jours: 14, maintenant: new Date("2026-10-23T12:00:00") });
  assert.equal(JSON.parse(await readFile(fichier, "utf8")).refresh.token, "jeton-2");
  const jeton = pawnote.appels.find((a) => a[0] === "jeton")[1];
  assert.equal(jeton.token, "jeton-1");
  assert.ok(jeton.deviceUUID);
  assert.deepEqual(r.controles.map((c) => c.id), ["c1"]);
  assert.equal(r.controles[0].matiere, "SISR - RESEAUX");
  assert.equal(r.devoirs[0].pour_le, "2026-10-27T07:00:00.000Z");
  assert.deepEqual(r.devoirs[0].themes, ["Suites"]);
  assert.equal(r.notes.periode, "Semestre 1");
  assert.deepEqual(r.notes.notes[0].valeur, { genre: 0, points: 15 });
  assert.deepEqual(r.erreurs, []);
  const [, debut, fin] = pawnote.appels.find((a) => a[0] === "devoirs");
  assert.equal((fin - debut) / 86400000, 14);
});

test("jeton refusé : la connexion passe en expirée", async () => {
  const { compte, fichier } = await nouveau();
  await compte.connexionQr({ qr: QR, pin: "1234" });
  const etat = JSON.parse(await readFile(fichier, "utf8"));
  etat.refresh.token = "perime";
  await (await import("node:fs/promises")).writeFile(fichier, JSON.stringify(etat));
  await assert.rejects(compte.synchro(), /refusé/);
  assert.deepEqual((await compte.etat()).connecte, false);
  assert.equal((await compte.etat()).expire, true);
  await assert.rejects(compte.synchro(), /expiré/);
});

test("une section en erreur n'empêche pas les autres", async () => {
  const { compte, pawnote } = await nouveau();
  pawnote.gradesOverview = async () => { throw new Error("boum"); };
  await compte.connexionQr({ qr: QR, pin: "1234" });
  const r = await compte.synchro();
  assert.equal(r.notes, null);
  assert.equal(r.devoirs.length, 1);
  assert.match(r.erreurs[0], /^notes : .*boum/);
});

test("déconnexion", async () => {
  const { compte } = await nouveau();
  await compte.connexionIdentifiants({ url: "https://x/pronote", username: "u", password: "p" });
  assert.equal((await compte.etat()).connecte, true);
  await compte.deconnexion();
  assert.deepEqual(await compte.etat(), { connecte: false });
});

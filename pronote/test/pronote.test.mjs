import assert from "node:assert/strict";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { creerCompte, creerFetcher, lireQr, listerPieces, pageLisible, traduire } from "../pronote.mjs";

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
      name: "MARTIN Camille", className: "BTS SIO 2", establishmentName: "Lycée Test",
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
  assert.equal(e.nom, "MARTIN Camille");
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

test("lireQr : format actuel de Pronote et copier-coller abîmé", () => {
  const brut = '{"avecPageConnexion":false,"jeton":"A1B2","login":"C3D4","url":"https://0000000a.index-education.net/pronote/mobile.eleve.html"}';
  const attendu = { avecPageConnexion: false, jeton: "A1B2", login: "C3D4", url: "https://0000000a.index-education.net/pronote/mobile.eleve.html" };
  assert.deepEqual(lireQr(brut), attendu);
  assert.deepEqual(lireQr(`  \uFEFF${brut}\n`), attendu);
  assert.deepEqual(lireQr(brut.replaceAll('"', (m, i) => (i % 2 ? "\u201C" : "\u201D"))), attendu);
  assert.deepEqual(lireQr(`QR : ${brut} fin`), attendu);
  assert.deepEqual(lireQr(encodeURIComponent(brut)), attendu);
  assert.deepEqual(lireQr(JSON.parse(brut)), attendu);
  assert.throws(() => lireQr('{"avecPageConnexion":false,"jeton":"A1B2","url":"https://x/pronote/mobile.eleve.html"}'), /il manque : login/);
  assert.throws(() => lireQr('{"jeton":"a","login":"b","url":"https://x/pronote/"}'), /espace élève mobile/);
  assert.throws(() => lireQr("bonjour"), /illisible/);
  assert.throws(() => lireQr(""), /illisible/);
});

test("connexion QR : PIN avec espaces accepté, QR passé à Pawnote nettoyé", async () => {
  const { compte, pawnote } = await nouveau();
  await compte.connexionQr({ qr: `\u201C${QR}`, pin: " 1234 " });
  const [, info] = pawnote.appels.find((a) => a[0] === "qr");
  assert.equal(info.pin, "1234");
  assert.equal(info.qr.jeton, "abc");
});

function reponse(status, corps = "", entetes = {}) {
  return new Response(status >= 300 && status < 400 ? null : corps, { status, headers: entetes });
}

test("fetcher : suit les redirections des GET avec les cookies, pas celles des POST", async () => {
  const vus = [];
  const journal = [];
  const faux = async (url, opts) => {
    vus.push([opts.method, url, opts.headers.Cookie]);
    if (url.startsWith("http://lycee.fr/pronote/")) {
      return reponse(301, "", { location: "https://lycee.fr/pronote/mobile.eleve.html?fd=1&login=true" });
    }
    if (url === "https://lycee.fr/pronote/mobile.eleve.html?fd=1&login=true") {
      if (!opts.headers.Cookie?.includes("etab=1")) {
        return reponse(302, "", { location: "/pronote/mobile.eleve.html?fd=1&login=true", "set-cookie": "etab=1; Path=/" });
      }
      return reponse(200, "<html>>PRONOTE 2026.0.1<script>try{Start({h:1})}catch(e){}</script></html>");
    }
    return reponse(302, "", { location: "https://ailleurs/" });
  };
  const fetcher = creerFetcher({ fetch: faux, journal: (m) => journal.push(m) });
  const r = await fetcher({ url: new URL("http://lycee.fr/pronote/mobile.eleve.html?fd=1&login=true"), headers: { Cookie: "appliMobile=1" } });
  assert.equal(r.status, 200);
  assert.match(r.content, /PRONOTE 2026/);
  assert.equal(vus.at(-1)[2], "appliMobile=1; etab=1");
  assert.equal(journal.length, 2);
  vus.length = 0;
  const p = await fetcher({ url: new URL("https://lycee.fr/pronote/appelfonction/3/x"), method: "POST", content: "{}" });
  assert.equal(p.status, 302);
  assert.equal(vus.length, 1);
  assert.deepEqual(fetcher.dernier, { methode: "POST", url: "https://lycee.fr/pronote/appelfonction/3/x", statut: 302, lieu: "https://ailleurs/" });
});

test("fetcher : s'arrête après 5 redirections", async () => {
  let n = 0;
  const fetcher = creerFetcher({ fetch: async () => (n++, reponse(302, "", { location: "/boucle" })), journal: () => {} });
  const r = await fetcher({ url: new URL("https://x/pronote/mobile.eleve.html") });
  assert.equal(r.status, 302);
  assert.equal(n, 6);
});

test("page indisponible : le message dit ce que le serveur a répondu", () => {
  const e = new Error("The requested page does not exist");
  e.name = "PageUnavailableError";
  const fetcher = { dernier: { methode: "GET", url: "https://x/pronote/mobile.eleve.html", statut: 302, lieu: "https://ent.fr/cas/login" } };
  assert.match(traduire(e, fetcher).message,
    /renvoyé sa page.*GET https:\/\/x\/pronote\/mobile\.eleve\.html → HTTP 302, redirige vers https:\/\/ent\.fr\/cas\/login/);
  assert.match(traduire(e).message, /^Pronote n'a pas renvoyé sa page/);
});

const START = "<script>try{Start({h:'1',a:6,d:true})}catch(e){}</script>";

test("pageLisible : version affichée autrement, page sans Start", () => {
  assert.equal(pageLisible(`<div>>PRONOTE 2025.2.6</div>${START}`), `<div>>PRONOTE 2025.2.6</div>${START}`);
  assert.match(pageLisible(`<span>PRONOTE&nbsp;2026.1.3</span>${START}`), />PRONOTE 2026\.1\.3/);
  assert.match(pageLisible(`<script>var info={"version":"2026.1.3"}</script>${START}`), />PRONOTE 2026\.1\.3/);
  assert.match(pageLisible(`<footer>Index Éducation 2026.1.3</footer>${START}`), />PRONOTE 2026\.1\.3/);
  assert.equal(pageLisible("<title>Mettez à jour l'application</title>"), null);
  assert.equal(pageLisible(START), null);
});

test("fetcher : page non reconnue avec l'UA de l'appli → nouvel essai en navigateur de bureau, gardé ensuite", async () => {
  const vus = [];
  const faux = async (url, opts) => {
    const ua = opts.headers["User-Agent"];
    vus.push(ua.includes("PRONOTE Mobile") ? "appli" : "bureau");
    if (url.includes("mobile.eleve.html")) {
      return ua.includes("PRONOTE Mobile")
        ? reponse(200, "<title>Application obsolète</title><p>Mettez à jour</p>")
        : reponse(200, `<div>>PRONOTE 2026.1.3</div>${START}`);
    }
    return reponse(200, "{}");
  };
  const journal = [];
  const fetcher = creerFetcher({ fetch: faux, journal: (m) => journal.push(m) });
  const entetes = { "User-Agent": "Mozilla/5.0 PRONOTE Mobile APP Version/2.0.11", Cookie: "appliMobile=1" };
  const r = await fetcher({ url: new URL("https://x/pronote/mobile.eleve.html?fd=1&login=true"), headers: entetes });
  assert.match(r.content, /PRONOTE 2026\.1\.3/);
  await fetcher({ url: new URL("https://x/pronote/appelfonction/3/1"), method: "POST", headers: entetes, content: "{}" });
  assert.deepEqual(vus, ["appli", "bureau", "bureau"]);
  assert.match(journal[0], /Application obsolète/);
});

test("fetcher : page jamais reconnue → gardée pour diagnostic, titre dans l'erreur", async () => {
  const { mkdtemp } = await import("node:fs/promises");
  const dossier = await mkdtemp(join(tmpdir(), "diag-"));
  const diagnostic = join(dossier, "page.html");
  const fetcher = creerFetcher({
    fetch: async () => reponse(200, "<title>Connexion ENT</title><p>Redirection…</p>"),
    journal: () => {}, diagnostic,
  });
  const r = await fetcher({ url: new URL("https://x/pronote/mobile.eleve.html"), headers: { "User-Agent": "PRONOTE Mobile" } });
  assert.match(r.content, /Connexion ENT/);
  assert.match(await readFile(diagnostic, "utf8"), /Redirection/);
  const e = new Error("x");
  e.name = "PageUnavailableError";
  assert.match(traduire(e, fetcher).message, /HTTP 200, page « Connexion ENT »/);
});

test("page réelle Pronote 2026.2.7 : la session démarre", async () => {
  const pawnote = await import("@blockshub/pawnote-lts");
  const page = await readFile(new URL("./pages/pronote-2026.2.7.html", import.meta.url), "utf8");
  const servir = (contenu) => async () => new Response(contenu, { status: 200 });
  const essai = async (fetcher) => {
    const session = pawnote.createSessionHandle(fetcher);
    try {
      await pawnote.loginToken(session, {
        url: "https://0000000a.index-education.net/pronote", kind: 6, username: "u", token: "t", deviceUUID: "d",
      });
    } catch (e) {
      return { session, erreur: e };
    }
    return { session };
  };
  // Page brute (Pawnote-LTS sait la lire) et page passée par le fetcher de Claunote :
  // la session est initialisée dans les deux cas (l'échec suivant vient du faux serveur)
  for (const fetcher of [
    async () => ({ status: 200, content: page, headers: new Headers() }),
    creerFetcher({ fetch: servir(page), journal: () => {} }),
  ]) {
    const { session, erreur } = await essai(fetcher);
    assert.notEqual(erreur?.name, "PageUnavailableError");
    assert.equal(session.information.id, 3504712);
    assert.equal(session.information.accountKind, 6);
    assert.deepEqual(session.instance.version, [2026, 2, 7]);
  }
});

const RESSOURCES = [{
  id: "r1", subject: { name: "SISR - RESEAUX" }, startDate: new Date("2026-10-06T08:00:00Z"),
  contents: [
    { title: "Chapitre 3 : VLAN", categoryText: "Cours", files: [
      { kind: 1, name: "cours-vlan.pdf", url: "https://x/pronote/FichiersExternes/AAA/cours-vlan.pdf?Session=1", id: "n1" },
      { kind: 0, name: "Cisco Packet Tracer", url: "https://www.netacad.com/pt", id: "n2" },
    ] },
    { description: "<p>Corrigé du <b>TP 2</b></p>", categoryText: "Correction", files: [
      { kind: 1, name: "corrige-tp2.docx", url: "https://x/pronote/FichiersExternes/BBB/corrige-tp2.docx?Session=1", id: "n3" },
    ] },
  ],
}];
const DEVOIRS_PJ = [{
  id: "d9", subject: { name: "CEJM" }, deadline: new Date("2026-10-27T07:00:00Z"),
  description: "<p>Lire le dossier</p>", attachments: [
    { kind: 1, name: "dossier4.pdf", url: "https://x/pronote/FichiersExternes/CCC/dossier4.pdf?Session=1", id: "n4" },
  ],
}];

test("listerPieces : ressources et devoirs, clés stables, liens à part", () => {
  const p = listerPieces(RESSOURCES, DEVOIRS_PJ);
  assert.deepEqual(p.map((x) => x.cle), [
    "ressource:SISR - RESEAUX:2026-10-06:cours-vlan.pdf",
    "ressource:SISR - RESEAUX:2026-10-06:Cisco Packet Tracer",
    "ressource:SISR - RESEAUX:2026-10-06:corrige-tp2.docx",
    "devoir:CEJM:2026-10-27:dossier4.pdf",
  ]);
  assert.equal(p[0].titre, "Chapitre 3 : VLAN");
  assert.equal(p[1].lien, "https://www.netacad.com/pt");
  assert.equal(p[2].titre, "Corrigé du TP 2");
  assert.equal(p[3].categorie, "Travail à faire");
});

test("documents : télécharge les nouveaux, saute ceux déjà vus, respecte la limite", async () => {
  const dossier = await mkdtemp(join(tmpdir(), "pronote-"));
  const pawnote = fauxPawnote();
  pawnote.resourcesFromIntervals = async () => RESSOURCES;
  pawnote.assignmentsFromIntervals = async () => DEVOIRS_PJ;
  const telecharges = [];
  const fetch = async (url, opts) => {
    telecharges.push(url);
    assert.match(opts.headers["User-Agent"], /PRONOTE Mobile/);
    if (url.includes("BBB")) return new Response("non", { status: 404 });
    return new Response(Buffer.from(`%PDF ${url}`), { status: 200 });
  };
  const compte = creerCompte({ pawnote, fichier: join(dossier, "c.json"), fetch, journal: () => {} });
  await compte.connexionQr({ qr: QR, pin: "1234" });
  const r = await compte.documents({ deja: ["devoir:CEJM:2026-10-27:dossier4.pdf"] });
  assert.equal(telecharges.length, 2);
  const [vlan, lien, corrige] = r.documents;
  assert.equal(Buffer.from(vlan.contenu, "base64").toString(), "%PDF https://x/pronote/FichiersExternes/AAA/cours-vlan.pdf?Session=1");
  assert.equal(vlan.url, undefined);
  assert.equal(lien.url, "https://www.netacad.com/pt");
  assert.equal(lien.contenu, undefined);
  assert.match(corrige.erreur, /HTTP 404/);
  assert.equal(r.restant, 0);

  const limite = await compte.documents({ maxOctets: 1 });
  assert.equal(limite.documents.filter((d) => d.contenu).length, 1);
  assert.equal(limite.restant, 2);
});

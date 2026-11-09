// Claunote hors ligne : les pages déjà ouvertes (fiches, cours, quiz) restent lisibles sans réseau.
const VERSION = "claunote-v5";
const PAGES = "claunote-pages";
const BASE = ["/hors-ligne", "/static/logo.svg", "/static/icone-192.png", "/static/htmx.min.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(BASE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((cles) => Promise.all(cles.filter((k) => k !== VERSION && k !== PAGES).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Préchargement : la page enverra l'adresse dès que le doigt touche un onglet (~150 ms avant le clic)
const precharges = new Map();
self.addEventListener("message", (e) => {
  const url = e.data && e.data.precharger;
  if (typeof url !== "string" || precharges.has(url) || new URL(url).origin !== location.origin) return;
  precharges.set(url, fetch(url, { credentials: "same-origin" }).catch(() => null));
  setTimeout(() => precharges.delete(url), 8000);  // pas plus vieux que quelques secondes
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method === "POST" && url.pathname === "/deconnexion") {
    e.waitUntil(caches.delete(PAGES));  // rien ne reste lisible après la déconnexion
    return;
  }
  if (req.method !== "GET") return;
  if (url.pathname.startsWith("/static/")) {
    // Fichiers fixes : le cache d'abord
    e.respondWith(caches.match(req).then((r) => r || fetch(req)));
    return;
  }
  if (req.mode === "navigate" && url.origin === location.origin) {
    // Pages : le réseau d'abord (ou la page déjà demandée au toucher du lien), la dernière version vue si le VPN est coupé
    const avance = precharges.get(req.url);
    precharges.delete(req.url);
    const reseau = avance
      ? avance.then((r) => (r && r.ok && !r.redirected ? r : fetch(req)))
      : fetch(req);
    e.respondWith(reseau.then((r) => {
      if (r.ok && !r.redirected && !url.pathname.startsWith("/connexion")) {
        const copie = r.clone();
        caches.open(PAGES).then((c) => c.put(req, copie));
      }
      return r;
    }).catch(() => caches.match(req).then((r) => r || caches.match("/hors-ligne"))));
  }
});

// Notifications push : le serveur envoie {titre, corps, url, tag, badge}
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { corps: e.data && e.data.text() }; }
  const badge = Number.isInteger(d.badge) && self.navigator.setAppBadge
    ? (d.badge ? self.navigator.setAppBadge(d.badge) : self.navigator.clearAppBadge()).catch(() => {})
    : Promise.resolve();
  e.waitUntil(Promise.all([badge, self.registration.showNotification(d.titre || "Claunote", {
    body: d.corps || "", tag: d.tag || undefined, icon: "/static/icone-192.png",
    data: { url: d.url || "/" }, lang: "fr",
  })]));
});

// Toucher la notification ouvre la bonne page (dans l'appli si elle est déjà ouverte)
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data && e.notification.data.url || "/", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((fenetres) => {
    const f = fenetres[0];
    if (!f) return self.clients.openWindow(url);
    return f.navigate(url).then((g) => (g || f).focus()).catch(() => self.clients.openWindow(url));
  }));
});

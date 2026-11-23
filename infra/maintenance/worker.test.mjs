// Tests du Worker de maintenance : node --test infra/maintenance/
import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.js";

const NAVIGATION = { "sec-fetch-mode": "navigate", accept: "text/html,application/xhtml+xml" };

function avecOrigine(reponse) {
  globalThis.fetch = async () => {
    if (reponse instanceof Error) throw reponse;
    return reponse;
  };
}

async function demander(enTetes = NAVIGATION, methode = "GET") {
  return worker.fetch(new Request("https://claunote.exemple.fr/cours", { method: methode, headers: enTetes }));
}

test("tunnel coupé (erreur 1033 = 530) : page de maintenance au lieu de l'erreur Cloudflare", async () => {
  avecOrigine(new Response("Error 1033 Cloudflare Tunnel error", { status: 530 }));
  const r = await demander();
  assert.equal(r.status, 503);
  assert.equal(r.headers.get("x-claunote-maintenance"), "1");
  assert.equal(r.headers.get("cache-control"), "no-store");
  assert.match(r.headers.get("content-type"), /text\/html/);
  const html = await r.text();
  assert.match(html, /Claunote revient dans un instant/);
  assert.match(html, /ne répond pas pour l'instant/);
  assert.match(html, /<svg class="logo"/);
});

test("site qui redémarre (502 de cloudflared) : page « Claunote redémarre »", async () => {
  avecOrigine(new Response("Bad Gateway", { status: 502 }));
  const html = await (await demander()).text();
  assert.match(html, /Claunote redémarre/);
});

test("Cloudflare n'arrive pas à joindre l'origine (exception) : page de maintenance", async () => {
  avecOrigine(new Error("network"));
  assert.equal((await demander()).status, 503);
});

test("un 503 venu de Claunote (kDrive en panne) passe tel quel", async () => {
  avecOrigine(new Response("kDrive ne répond pas", { status: 503, headers: { "x-claunote-app": "1" } }));
  const r = await demander();
  assert.equal(await r.text(), "kDrive ne répond pas");
  assert.equal(r.headers.get("x-claunote-maintenance"), null);
});

test("les réponses normales et les autres erreurs passent telles quelles", async () => {
  for (const status of [200, 303, 404, 500]) {
    const origine = new Response(status === 303 ? null : "x", { status, headers: { "x-claunote-app": "1" } });
    avecOrigine(origine);
    assert.equal(await demander(), origine);
  }
});

test("requête htmx ou fetch d'une page ouverte : réponse courte, pas la page entière", async () => {
  avecOrigine(new Response("", { status: 530 }));
  for (const enTetes of [{ "hx-request": "true", accept: "text/html" }, { accept: "*/*", "sec-fetch-mode": "cors" }]) {
    const r = await demander(enTetes);
    assert.equal(r.status, 503);
    assert.match(r.headers.get("content-type"), /text\/plain/);
    assert.doesNotMatch(await r.text(), /<html/);
  }
  const tete = await demander(NAVIGATION, "HEAD");
  assert.equal(tete.status, 503);
  assert.equal(await tete.text(), "");
});

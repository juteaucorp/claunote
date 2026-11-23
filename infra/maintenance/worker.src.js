// Worker Cloudflare « page de maintenance » de Claunote, placé sur la route claunote.<ton-domaine>/*.
// Il laisse passer chaque requête vers le tunnel ; si le site ne répond pas, il renvoie la page de
// maintenance au lieu de l'erreur Cloudflare (1033 « tunnel error », 530, 502 « Bad gateway »…).
//
//   navigateur ──▶ Worker ──▶ tunnel ──▶ web:8000
//                    │
//                    └── tunnel coupé (1033/530), VM éteinte, site qui redémarre (502-504) : page de maintenance
//
// Les réponses de Claunote portent l'en-tête x-claunote-app : un 503 du site lui-même (kDrive en panne…)
// passe tel quel. Seules les erreurs venues de Cloudflare ou de cloudflared sont remplacées.
//
// NE PAS MODIFIER worker.js à la main : il est fabriqué à partir de ce fichier et de maintenance.html par
//   python3 infra/maintenance/assembler.py
// (un test vérifie qu'il est à jour). Déploiement : docs/page-de-maintenance.md.

const PAGE = "__PAGE__";
const EN_TETES = {
  "cache-control": "no-store",
  "retry-after": "15",
  "x-claunote-maintenance": "1",
  "x-robots-tag": "noindex",
  "x-content-type-options": "nosniff",
  "referrer-policy": "same-origin",
  "content-security-policy":
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; " +
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
};
const CAUSES = {
  // Tunnel arrêté, VM éteinte ou injoignable : rien ne répond derrière Cloudflare
  injoignable: "Le serveur de Claunote ne répond pas pour l'instant (mise à jour, redémarrage ou coupure). Rien n'est perdu.",
  // Le tunnel répond mais le site non : il redémarre (mise à jour en cours)
  redemarre: "Claunote redémarre, sûrement pour une mise à jour. Rien n'est perdu.",
};

function maintenance(request, cause) {
  const enTetes = new Headers(EN_TETES);
  const accepte = request.headers.get("accept") || "";
  const page = request.method === "GET" && !request.headers.get("hx-request") &&
    (request.headers.get("sec-fetch-mode") === "navigate" || accepte.includes("text/html"));
  if (!page) {
    // Requête d'une page déjà ouverte (htmx, fetch, fichier) : réponse courte, la page affichée ne change pas
    enTetes.set("content-type", "text/plain; charset=utf-8");
    return new Response(request.method === "HEAD" ? null : "Claunote redémarre : réessaie dans un instant.",
      { status: 503, headers: enTetes });
  }
  enTetes.set("content-type", "text/html; charset=utf-8");
  const html = PAGE.replace(/(<p class="doux" id="cause">)[^<]*(<\/p>)/, `$1${CAUSES[cause]}$2`);
  return new Response(html, { status: 503, headers: enTetes });
}

export default {
  async fetch(request) {
    let reponse;
    try {
      reponse = await fetch(request);
    } catch (e) {
      return maintenance(request, "injoignable");
    }
    if (reponse.status >= 502 && reponse.status <= 530 && !reponse.headers.has("x-claunote-app")) {
      // 502 : cloudflared tourne mais le site ne répond pas. 530 (erreur 1033), 52x : rien derrière Cloudflare.
      return maintenance(request, reponse.status === 502 ? "redemarre" : "injoignable");
    }
    return reponse;
  },
};

// Face ID / Touch ID (clés d'accès) : le navigateur parle en ArrayBuffer, le serveur en base64url (voir cles.py)
(() => {
  const vers64 = (tampon) => btoa(String.fromCharCode(...new Uint8Array(tampon)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const depuis64 = (texte) => {
    const b = atob(texte.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((texte.length + 3) % 4));
    return Uint8Array.from(b, (c) => c.charCodeAt(0)).buffer;
  };
  const poster = async (url, corps) => {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: corps ? JSON.stringify(corps) : undefined, credentials: "same-origin" });
    const texte = await r.text();
    let donnees = {};
    try { donnees = JSON.parse(texte); } catch (e) { donnees = { erreur: texte }; }
    if (!r.ok) throw new Error(donnees.erreur || donnees.detail || `erreur ${r.status}`);
    return donnees;
  };
  const enJson = (cle) => {
    const r = cle.response, sortie = {
      id: cle.id, rawId: vers64(cle.rawId), type: cle.type,
      clientExtensionResults: cle.getClientExtensionResults ? cle.getClientExtensionResults() : {},
      authenticatorAttachment: cle.authenticatorAttachment || undefined,
      response: { clientDataJSON: vers64(r.clientDataJSON) },
    };
    if (r.attestationObject) {
      sortie.response.attestationObject = vers64(r.attestationObject);
      if (r.getTransports) sortie.response.transports = r.getTransports();
    } else {
      sortie.response.authenticatorData = vers64(r.authenticatorData);
      sortie.response.signature = vers64(r.signature);
      if (r.userHandle) sortie.response.userHandle = vers64(r.userHandle);
    }
    return sortie;
  };
  const message = (e) => e && e.name === "NotAllowedError" ? "Annulé." : (e && e.message) || String(e);

  window.clesPossibles = () => !!(window.PublicKeyCredential && window.isSecureContext);

  window.ajouterCle = async (nom) => {
    const o = await poster("/cles/inscription/options");
    o.challenge = depuis64(o.challenge);
    o.user.id = depuis64(o.user.id);
    (o.excludeCredentials || []).forEach((c) => { c.id = depuis64(c.id); });
    try {
      const cle = await navigator.credentials.create({ publicKey: o });
      return await poster("/cles/inscription", { cle: enJson(cle), nom });
    } catch (e) { throw new Error(message(e)); }
  };

  window.connexionCle = async (suite) => {
    const o = await poster("/cles/connexion/options");
    o.challenge = depuis64(o.challenge);
    (o.allowCredentials || []).forEach((c) => { c.id = depuis64(c.id); });
    let cle;
    try { cle = await navigator.credentials.get({ publicKey: o }); } catch (e) {
      // Annulé, ou aucune clé pour ce site sur cet appareil : le navigateur ne dit pas lequel
      throw new Error(e && e.name === "NotAllowedError"
        ? "Annulé, ou cet appareil n'a pas encore été ajouté : connecte-toi avec le mot de passe, puis Réglages → Compte → Ajouter cet appareil."
        : message(e));
    }
    return await poster("/cles/connexion", { cle: enJson(cle), suite });
  };
})();

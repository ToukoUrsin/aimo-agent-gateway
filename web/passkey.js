// An approver's own device signs the decision (a WebAuthn passkey). Needs lib.js (api, jcs, utf8, b64u, b64d).
// The key is made inside the phone or laptop and never leaves it; the gateway keeps only the public half.
const PK = {
  supported: () => !!(window.PublicKeyCredential && navigator.credentials && window.isSecureContext),
  // True when this device can sign with a fingerprint, face or screen lock of its own.
  async builtIn() {
    try { return PK.supported() && await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable(); } catch (e) { return false; }
  },
  deviceName() {
    const ua = navigator.userAgent;
    const what = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android phone" : /Mac OS X/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows PC" : /Linux/.test(ua) ? "Linux computer" : "Device";
    const how = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "";
    return how ? `${what}, ${how}` : what;
  },
  buf: v => new Uint8Array(v),
  // Make a key in this device for the account. t: an enrolment link's token, or "" on the account's own browser.
  async enrol(t) {
    const o = await api("/api/passkey/enrol/begin", { t: t || "" });
    if (!o.challenge) throw new Error((o.messages || [{}])[0].content || "This device could not be enrolled.");
    const cred = await navigator.credentials.create({ publicKey: {
      challenge: b64d(o.challenge), rp: o.rp, user: { id: b64d(o.user.id), name: o.user.name, displayName: o.user.displayName },
      pubKeyCredParams: o.pubKeyCredParams, excludeCredentials: o.exclude.map(id => ({ type: "public-key", id: b64d(id) })),
      authenticatorSelection: { residentKey: "preferred", userVerification: "preferred" }, attestation: "none", timeout: 120000 } });
    const r = cred.response;
    const done = await api("/api/passkey/enrol/finish", { t: t || "", challenge: o.challenge, id: cred.id, clientDataJSON: b64u(PK.buf(r.clientDataJSON)), authenticatorData: b64u(PK.buf(r.getAuthenticatorData())),
      publicKey: b64u(PK.buf(r.getPublicKey())), alg: r.getPublicKeyAlgorithm(), name: PK.deviceName() });
    if (!done.device) throw new Error((done.messages || [{}])[0].content || "This device could not be enrolled.");
    return done.device;
  },
  // Sign one decision. pre: the answer of POST /api/approve/{id}/device.
  async sign(pre) {
    const a = await navigator.credentials.get({ publicKey: { challenge: b64d(pre.challenge), rpId: pre.rp_id, allowCredentials: pre.allow.map(id => ({ type: "public-key", id: b64d(id) })), userVerification: "preferred", timeout: 120000 } });
    const r = a.response;
    return { id: a.id, authenticatorData: b64u(PK.buf(r.authenticatorData)), clientDataJSON: b64u(PK.buf(r.clientDataJSON)), signature: b64u(PK.buf(r.signature)) };
  },
  // ECDSA signatures come from the device in DER; the browser's verifier wants r||s.
  derToRaw(der) {
    let i = 2 + (der[1] & 0x80 ? der[1] & 0x7f : 0);
    const part = () => { i++; const n = der[i++]; let v = der.subarray(i, i + n); i += n; while (v.length > 32 && v[0] === 0) v = v.subarray(1); const out = new Uint8Array(32); out.set(v, 32 - v.length); return out; };
    const r = part(), s = part(), raw = new Uint8Array(64);
    raw.set(r, 0); raw.set(s, 32);
    return raw;
  },
  // Check, here in the browser, that a device signed exactly this decision. d: approval.device from the signed record.
  async verify(d, approval) {
    try {
      const cdj = b64d(d.client_data_json), ad = b64d(d.authenticator_data), cd = JSON.parse(new TextDecoder().decode(cdj));
      const want = b64u(PK.buf(await crypto.subtle.digest("SHA-256", utf8(jcs(d.signed)))));
      if (cd.type !== "webauthn.get" || cd.challenge !== want) return false;
      if (approval && (d.signed.checkout_digest !== approval.checkout_digest || d.signed.decision !== approval.decision || d.signed.by !== approval.by)) return false;
      const hash = PK.buf(await crypto.subtle.digest("SHA-256", cdj)), data = new Uint8Array(ad.length + 32);
      data.set(ad, 0); data.set(hash, ad.length);
      if (d.alg === "RS256") {
        const key = await crypto.subtle.importKey("spki", b64d(d.public_key), { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
        return await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64d(d.signature), data);
      }
      const key = await crypto.subtle.importKey("spki", b64d(d.public_key), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
      return await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, PK.derToRaw(b64d(d.signature)), data);
    } catch (e) { return false; }
  },
};

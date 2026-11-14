// An approver's own device signs the decision. Needs lib.js (api, jcs, utf8, b64u, b64d, sha256hex).
// Two kinds of key, and every page and record says which one signed:
//   a passkey: made inside the phone or laptop, used after its fingerprint, face or screen lock (WebAuthn);
//   a browser key: where the browser has no such lock, it makes its own ES256 key with WebCrypto, marked
//   non-extractable and kept in this browser's IndexedDB. No fingerprint is involved.
// Either way the gateway keeps only the public half.
const PK = {
  HOW: { passkey: "a passkey", browser_key: "a key held in this browser, no fingerprint" },
  supported: () => !!(window.PublicKeyCredential && navigator.credentials && window.isSecureContext),
  // True when this device can sign with a fingerprint, face or screen lock of its own.
  async builtIn() {
    try { return PK.supported() && await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable(); } catch (e) { return false; }
  },
  // True when this browser can make and keep a signing key of its own.
  canKeep: () => !!(window.indexedDB && window.crypto && crypto.subtle && window.isSecureContext),
  // Which kind this browser would enrol: a passkey where the device has a lock of its own, else its own key, else nothing.
  async kindHere() { return await PK.builtIn() ? "passkey" : PK.canKeep() ? "browser_key" : PK.supported() ? "passkey" : null; },
  deviceName() {
    const ua = navigator.userAgent;
    const what = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android phone" : /Mac OS X/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows PC" : /Linux/.test(ua) ? "Linux computer" : "Device";
    const how = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "";
    return how ? `${what}, ${how}` : what;
  },
  buf: v => new Uint8Array(v),
  err: (o, fallback) => new Error((((o || {}).messages || [{}])[0] || {}).content || fallback),

  // ---- Keys this browser made itself. The private half is a CryptoKey that cannot be exported; only this browser can use it.
  db() {
    return new Promise((res, rej) => {
      const q = indexedDB.open("aimo-approver", 1);
      q.onupgradeneeded = () => q.result.createObjectStore("keys", { keyPath: "id" });
      q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error);
    });
  },
  async tx(mode, fn) {
    const db = await PK.db();
    try { return await new Promise((res, rej) => { const t = db.transaction("keys", mode), q = fn(t.objectStore("keys")); t.oncomplete = () => res(q.result); t.onerror = t.onabort = () => rej(t.error || q.error); }); }
    finally { db.close(); }
  },
  async keys() { try { return (await PK.tx("readonly", s => s.getAll())) || []; } catch (e) { return []; } },
  putKey: row => PK.tx("readwrite", s => s.put(row)),
  dropKey: id => PK.tx("readwrite", s => s.delete(id)).catch(() => {}),

  // Make a key on this device for the account. o: an enrolment link's token (string), or
  // { t, cid, k, kind }: cid and k name the approval link this is done from; kind forces "passkey" or "browser_key".
  async enrol(o) {
    o = typeof o === "string" || !o ? { t: o || "" } : o;
    const who = { t: o.t || "", ...(o.cid ? { cid: o.cid, k: o.k } : {}) };
    const kind = o.kind || await PK.kindHere();
    if (!kind) throw new Error("This browser can neither make a passkey nor keep a signing key. Open the link in another browser.");
    const b = await api("/api/passkey/enrol/begin", who);
    if (!b.challenge) throw PK.err(b, "This device could not be enrolled.");
    let body;
    if (kind === "browser_key") {
      const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
      const spki = PK.buf(await crypto.subtle.exportKey("spki", pair.publicKey));
      const id = "bk-" + (await sha256hex(spki)).slice(0, 32);
      // Proof that this browser holds the private half: it signs this enrolment's challenge.
      const cdj = utf8(JSON.stringify({ type: "aimo.key.create", challenge: b.challenge, origin: location.origin }));
      const sig = PK.buf(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, pair.privateKey, cdj));
      await PK.putKey({ id, key: pair.privateKey, account: b.account, made: new Date().toISOString() });  // kept before the gateway is told, so an account is never tied to a key nobody holds
      body = { kind, id, publicKey: b64u(spki), clientDataJSON: b64u(cdj), signature: b64u(sig) };
    } else {
      const cred = await navigator.credentials.create({ publicKey: {
        challenge: b64d(b.challenge), rp: b.rp, user: { id: b64d(b.user.id), name: b.user.name, displayName: b.user.displayName },
        pubKeyCredParams: b.pubKeyCredParams, excludeCredentials: b.exclude.filter(id => !id.startsWith("bk-")).map(id => ({ type: "public-key", id: b64d(id) })),
        authenticatorSelection: { residentKey: "preferred", userVerification: "preferred" }, attestation: "none", timeout: 120000 } });
      const r = cred.response;
      body = { kind, id: cred.id, clientDataJSON: b64u(PK.buf(r.clientDataJSON)), authenticatorData: b64u(PK.buf(r.getAuthenticatorData())), publicKey: b64u(PK.buf(r.getPublicKey())), alg: r.getPublicKeyAlgorithm() };
    }
    let done;
    try { done = await api("/api/passkey/enrol/finish", { ...who, challenge: b.challenge, name: PK.deviceName(), ...body }); }
    catch (e) { if (kind === "browser_key") PK.dropKey(body.id); throw e; }
    if (!done.device) { if (kind === "browser_key") PK.dropKey(body.id); throw PK.err(done, "This device could not be enrolled."); }
    return done.device;
  },
  // Which of the account's keys this browser can sign with: "browser_key" (one is kept here), "passkey" (the device is asked), or null.
  async holds(pre) {
    const ids = pre.keys || [];
    if (ids.length && (await PK.keys()).some(x => ids.includes(x.id))) return "browser_key";
    return (pre.allow || []).length && PK.supported() ? "passkey" : null;
  },
  // Sign one decision. pre: the answer of POST /api/approve/{id}/device.
  async sign(pre) {
    const mine = (await PK.keys()).find(x => (pre.keys || []).includes(x.id));
    if (mine) {
      // The browser's own key signs a small statement naming this decision's digest and this site.
      const cdj = utf8(JSON.stringify({ type: "aimo.key.get", challenge: pre.challenge, origin: location.origin }));
      const sig = PK.buf(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, mine.key, cdj));
      return { id: mine.id, clientDataJSON: b64u(cdj), signature: b64u(sig) };
    }
    if (!(pre.allow || []).length || !PK.supported()) { const e = new Error("This is not one of the account's approver devices."); e.name = "NotEnrolledError"; throw e; }
    const a = await navigator.credentials.get({ publicKey: { challenge: b64d(pre.challenge), rpId: pre.rp_id, allowCredentials: pre.allow.map(id => ({ type: "public-key", id: b64d(id) })), userVerification: "preferred", timeout: 120000 } });
    const r = a.response;
    return { id: a.id, authenticatorData: b64u(PK.buf(r.authenticatorData)), clientDataJSON: b64u(PK.buf(r.clientDataJSON)), signature: b64u(PK.buf(r.signature)) };
  },
  // ECDSA signatures come from a passkey in DER; the browser's verifier wants r||s.
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
      const cdj = b64d(d.client_data_json), cd = JSON.parse(new TextDecoder().decode(cdj)), own = d.method === "browser_key";
      const want = b64u(PK.buf(await crypto.subtle.digest("SHA-256", utf8(jcs(d.signed)))));
      if (cd.type !== (own ? "aimo.key.get" : "webauthn.get") || cd.challenge !== want) return false;
      if (approval && (d.signed.checkout_digest !== approval.checkout_digest || d.signed.decision !== approval.decision || d.signed.by !== approval.by)) return false;
      // The lines the approval says it clears are the lines the device signed for.
      if (approval && "clears" in d.signed && jcs(d.signed.clears || []) !== jcs(approval.clears || [])) return false;
      if (own) {
        const key = await crypto.subtle.importKey("spki", b64d(d.public_key), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
        return await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, b64d(d.signature), cdj);
      }
      const ad = b64d(d.authenticator_data), hash = PK.buf(await crypto.subtle.digest("SHA-256", cdj)), data = new Uint8Array(ad.length + 32);
      data.set(ad, 0); data.set(hash, ad.length);
      if (d.alg === "RS256") {
        const key = await crypto.subtle.importKey("spki", b64d(d.public_key), { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
        return await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64d(d.signature), data);
      }
      const key = await crypto.subtle.importKey("spki", b64d(d.public_key), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
      return await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, PK.derToRaw(b64d(d.signature)), data);
    } catch (e) { return false; }
  },

  // ---- Words. One place, so the approval page, the order page and the demo say the same thing.
  // The lines an approval cleared, as text: "Rye bread (peanut); Oat drink (peanut, tree nuts)".
  clearsText: list => (list || []).map(u => `${u.title} (${u.what || (u.codes || []).join(", ")})`).join("; "),
  // What the order page says about the approval record.
  approvalText(ap) {
    const n = (ap.clears || []).length;
    return `Approval recorded at ${when(ap.at)}, overriding ${ap.overrides.join(", ")}.` + (ap.asked_by ? ` Asked by ${ap.asked_by}; approved by ${ap.by}.` : "")
      + (n ? ` It clears ${n} ${n === 1 ? "line" : "lines"} with no allergen declaration, ${ap.clears_confirmed || "all at once"}: ${PK.clearsText(ap.clears)}.` : "")
      + (ap.device ? " The gateway signed the record; the approver's own device signed the decision (next check)." : " No approver device was enrolled on this account, so the approval came by link and the gateway's key signed the record.");
  },
  // What the order page says about the device's own signature.
  deviceText(dv) {
    const covers = "this order's digest, the decision, the name" + ("clears" in (dv.signed || {}) ? ", the note and each line it clears" : " and the note");
    if (dv.method === "browser_key") return `A key held in the approver's browser (${dv.device}, key ${dv.thumbprint}) signed ${covers}. No fingerprint was involved: that browser has no fingerprint, face or screen lock to ask, so it made the key itself and keeps it where a page cannot export it. Nobody without that browser can make this signature: not the agent, not Valio Aimo, not this server. What it does not prove is who holds that browser: in this prototype the first device that approves for an account becomes its approver and the name is typed, so a second browser of the asker's own would pass. In production the approver signs in with their own Minun Aimo account and role before a device is enrolled.`;
    return `A passkey on ${dv.device} (key ${dv.thumbprint}) signed ${covers}${dv.user_verified ? ", after the device checked the person by fingerprint, face or screen lock" : ""}. Nobody without that device can make this signature: not the agent, not Valio Aimo, not this server. What it does not prove is who holds that device: in this prototype the first device that approves for an account becomes its approver and the name is typed, so a second browser of the asker's own would pass. In production the approver signs in with their own Minun Aimo account and role before a device is enrolled.`;
  },
};

// The order page's checks are built in lib.js (orderChecks). Their two approval rows get their words from here, so a
// browser key is never called a passkey, and the record's "asked by" and the lines it clears are shown.
if (typeof orderChecks === "function") {
  const base = orderChecks;
  orderChecks = async function (o, ...rest) {
    const out = await base(o, ...rest), ap = (((o || {}).wholesale || {}).evidence || {}).approval;
    if (!ap) return out;
    for (const row of out) {
      if (!row[0]) continue;
      if (row[1] === `${ap.by} approved it`) row[2] = PK.approvalText(ap);
      else if (ap.device && row[1] === "The approval was signed on the approver's own device") {
        row[1] = ap.device.method === "browser_key" ? "The approval was signed in the approver's own browser" : "The approval was signed on the approver's own device";
        row[2] = PK.deviceText(ap.device);
      }
    }
    return out;
  };
}

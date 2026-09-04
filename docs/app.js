// Shared helpers for both pages.

// --- Password gate + encrypted data loading -------------------------------
// The published data is AES-GCM encrypted. We decrypt it in the browser after
// the password is entered, and remember the device for 30 days so the password
// isn't needed every visit. This is a light gate, not bank-grade security.

const TRUST_KEY = "atlas_trust";
const TRUST_MS = 30 * 24 * 3600 * 1000;

function _b64(b64) {
  const bin = atob(b64);
  const a = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  return a;
}

async function _decrypt(enc, pw) {
  const salt = _b64(enc.salt), iv = _b64(enc.iv), ct = _b64(enc.ct);
  const base = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: enc.iter || 200000, hash: "SHA-256" },
    base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ct);
  return JSON.parse(new TextDecoder().decode(pt));
}

function _readTrust() {
  try {
    const t = JSON.parse(localStorage.getItem(TRUST_KEY));
    if (t && t.pw && t.exp > Date.now()) return t;
  } catch (e) { /* ignore */ }
  return null;
}
function _saveTrust(pw) {
  try { localStorage.setItem(TRUST_KEY, JSON.stringify({ pw, exp: Date.now() + TRUST_MS })); }
  catch (e) { /* ignore */ }
}
function _clearTrust() {
  try { localStorage.removeItem(TRUST_KEY); } catch (e) { /* ignore */ }
}

function _addSignOut() {
  if (document.getElementById("atlas-signout")) return;
  const a = document.createElement("a");
  a.id = "atlas-signout";
  a.href = "#";
  a.textContent = "Sign out";
  a.style.cssText = "position:fixed;bottom:12px;left:14px;font-size:12px;color:#6b7280;" +
    "text-decoration:none;background:#fff;border:1px solid #e3e8e2;border-radius:6px;" +
    "padding:4px 9px;z-index:9998;font-family:Lato,sans-serif";
  a.onclick = (e) => { e.preventDefault(); _clearTrust(); location.reload(); };
  document.body.appendChild(a);
}

function _showGate(enc) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;background:#f4f6f3;display:flex;" +
      "align-items:center;justify-content:center;z-index:9999;font-family:Lato,sans-serif";
    ov.innerHTML =
      '<div style="text-align:center;max-width:320px;padding:24px">' +
      '<img src="sch-logo.png" alt="SCH Properties" style="height:120px;width:auto;margin-bottom:18px">' +
      '<div style="font-family:\'Playfair Display\',Georgia,serif;color:#1e4a24;font-size:20px;font-weight:700;margin-bottom:4px">Mobile Home Park Atlas</div>' +
      '<div style="color:#6b7280;font-size:13px;margin-bottom:18px">Enter the password to continue.</div>' +
      '<input id="atlas-pw" type="password" placeholder="Password" autocomplete="current-password" style="width:100%;padding:11px 13px;border:1px solid #d7ddd6;border-radius:9px;font-size:15px;box-sizing:border-box">' +
      '<div id="atlas-err" style="color:#9c2f45;font-size:12.5px;min-height:16px;margin:8px 0 4px"></div>' +
      '<button id="atlas-go" style="width:100%;padding:11px;background:#1e4a24;color:#fff;border:none;border-radius:9px;font-size:15px;font-weight:700;cursor:pointer">Enter</button>' +
      '</div>';
    document.body.appendChild(ov);
    const inp = ov.querySelector("#atlas-pw");
    const err = ov.querySelector("#atlas-err");
    const btn = ov.querySelector("#atlas-go");
    inp.focus();
    async function attempt() {
      btn.disabled = true; err.textContent = "";
      try {
        const fc = await _decrypt(enc, inp.value);
        _saveTrust(inp.value);
        document.body.removeChild(ov);
        _addSignOut();
        resolve(fc);
      } catch (e) {
        btn.disabled = false; err.textContent = "Incorrect password"; inp.select();
      }
    }
    btn.onclick = attempt;
    inp.onkeydown = (e) => { if (e.key === "Enter") attempt(); };
  });
}

async function loadParks() {
  // Production: encrypted file behind the password gate.
  let encResp;
  try { encResp = await fetch("parks.enc.json", { cache: "no-store" }); }
  catch (e) { encResp = { ok: false }; }
  if (encResp.ok) {
    const enc = await encResp.json();
    const t = _readTrust();
    if (t) {
      try { const fc = await _decrypt(enc, t.pw); _addSignOut(); return fc; }
      catch (e) { _clearTrust(); }
    }
    return _showGate(enc);
  }
  // Dev/sample fallback: plain data, no gate.
  const res = await fetch("parks.geojson", { cache: "no-store" });
  if (!res.ok) throw new Error("no data file found");
  return res.json();
}

function money(v) {
  if (v === null || v === undefined || v === "") return "";
  return "$" + Math.round(v).toLocaleString("en-US");
}

function num(v) {
  if (v === null || v === undefined || v === "") return "";
  return Number(v).toLocaleString("en-US");
}

function deed(p) {
  const b = (p.DeedBook || "").trim();
  const pg = (p.DeedPage || "").trim();
  return b || pg ? `${b}/${pg}` : "";
}

// Centroid of the first ring, for map markers.
function centroid(geom) {
  if (!geom || !geom.coordinates) return null;
  const ring = geom.coordinates[0];
  if (!ring || !ring.length) return null;
  let x = 0, y = 0;
  for (const pt of ring) { x += pt[0]; y += pt[1]; }
  return [y / ring.length, x / ring.length]; // [lat, lng] for Leaflet
}

function isSample(fc) {
  return fc && fc.meta && fc.meta.sample === true;
}

// ZIP for a park: prefer the SAMS field, else pull a 5-digit ZIP out of the
// physical address, so every park has a ZIP even if the field is blank.
function zipOf(p) {
  const s = (p.SAMSZip || "").toString().trim();
  if (/^\d{5}/.test(s)) return s.slice(0, 5);
  const m = ((p.FullPhysicalAddress || "") + " " + (p.FullOwnerAddress || "")).match(/\b(\d{5})\b/);
  return m ? m[1] : "";
}

// Full 911 address with the ZIP appended if it isn't already in the string.
function addressWithZip(p) {
  const a = (p.FullPhysicalAddress || "").trim();
  const z = zipOf(p);
  if (!a) return z || "";
  return z && !a.includes(z) ? `${a} ${z}` : a;
}

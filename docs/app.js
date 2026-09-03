// Shared helpers for both pages.

async function loadParks() {
  const res = await fetch("parks.geojson", { cache: "no-store" });
  if (!res.ok) throw new Error("parks.geojson not found");
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

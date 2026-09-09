/**
 * Offline: lonLat↔UTM round-trip + field ring→canvas projection for land 10061.
 * Uses /tmp/s2_10061_pixel.json and /tmp/field_xiaochang1.geojson when present.
 */
import fs from "node:fs";

function utmToLonLat(easting, northing, epsg) {
  const northern = epsg >= 32601 && epsg <= 32660;
  const zone = northern ? epsg - 32600 : epsg - 32700;
  const a = 6378137.0;
  const eccSquared = 0.00669438;
  const e1 = (1 - Math.sqrt(1 - eccSquared)) / (1 + Math.sqrt(1 - eccSquared));
  const k0 = 0.9996;
  const x = easting - 500000.0;
  let y = northing;
  if (!northern) y -= 10000000.0;
  const longOrigin = (zone - 1) * 6 - 180 + 3;
  const m = y / k0;
  const mu =
    m /
    (a *
      (1 -
        eccSquared / 4 -
        (3 * eccSquared * eccSquared) / 64 -
        (5 * eccSquared * eccSquared * eccSquared) / 256));
  const phi1Rad =
    mu +
    ((3 * e1) / 2 - (27 * e1 * e1 * e1) / 32) * Math.sin(2 * mu) +
    ((21 * e1 * e1) / 16 - (55 * e1 * e1 * e1 * e1) / 32) * Math.sin(4 * mu) +
    ((151 * e1 * e1 * e1) / 96) * Math.sin(6 * mu);
  const n1 = a / Math.sqrt(1 - eccSquared * Math.sin(phi1Rad) * Math.sin(phi1Rad));
  const t1 = Math.tan(phi1Rad) * Math.tan(phi1Rad);
  const c1 = (eccSquared * Math.cos(phi1Rad) * Math.cos(phi1Rad)) / (1 - eccSquared);
  const r1 =
    (a * (1 - eccSquared)) /
    Math.pow(1 - eccSquared * Math.sin(phi1Rad) * Math.sin(phi1Rad), 1.5);
  const d = x / (n1 * k0);
  const lat =
    phi1Rad -
    ((n1 * Math.tan(phi1Rad)) / r1) *
      ((d * d) / 2 -
        ((5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * c1) * d * d * d * d) / 24 +
        ((61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * c1 - 3 * c1 * c1) *
          d *
          d *
          d *
          d *
          d *
          d) /
          720);
  const lon =
    (d -
      ((1 + 2 * t1 + c1) * d * d * d) / 6 +
      ((5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * c1 + 24 * t1 * t1) *
        d *
        d *
        d *
        d *
        d) /
        120) /
    Math.cos(phi1Rad);
  return [(lon * 180) / Math.PI + longOrigin, (lat * 180) / Math.PI];
}

function lonLatToUtm(lon, lat, epsg) {
  const northern = epsg >= 32601 && epsg <= 32660;
  const zone = northern ? epsg - 32600 : epsg - 32700;
  const a = 6378137.0;
  const eccSquared = 0.00669438;
  const k0 = 0.9996;
  const longOrigin = (zone - 1) * 6 - 180 + 3;
  const latRad = (lat * Math.PI) / 180;
  const lonRad = (lon * Math.PI) / 180;
  const longOriginRad = (longOrigin * Math.PI) / 180;
  const eccPrimeSquared = eccSquared / (1 - eccSquared);
  const N = a / Math.sqrt(1 - eccSquared * Math.sin(latRad) * Math.sin(latRad));
  const T = Math.tan(latRad) * Math.tan(latRad);
  const C = eccPrimeSquared * Math.cos(latRad) * Math.cos(latRad);
  const A = Math.cos(latRad) * (lonRad - longOriginRad);
  const M =
    a *
    ((1 -
      eccSquared / 4 -
      (3 * eccSquared * eccSquared) / 64 -
      (5 * eccSquared * eccSquared * eccSquared) / 256) *
      latRad -
      ((3 * eccSquared) / 8 +
        (3 * eccSquared * eccSquared) / 32 +
        (45 * eccSquared * eccSquared * eccSquared) / 1024) *
        Math.sin(2 * latRad) +
      ((15 * eccSquared * eccSquared) / 256 +
        (45 * eccSquared * eccSquared * eccSquared) / 1024) *
        Math.sin(4 * latRad) -
      ((35 * eccSquared * eccSquared * eccSquared) / 3072) * Math.sin(6 * latRad));
  let easting =
    k0 *
      N *
      (A +
        ((1 - T + C) * A * A * A) / 6 +
        ((5 - 18 * T + T * T + 72 * C - 58 * eccPrimeSquared) * A * A * A * A * A) /
          120) +
    500000.0;
  let northing =
    k0 *
    (M +
      N *
        Math.tan(latRad) *
        ((A * A) / 2 +
          ((5 - T + 9 * C + 4 * C * C) * A * A * A * A) / 24 +
          ((61 - 58 * T + T * T + 600 * C - 330 * eccPrimeSquared) *
            A *
            A *
            A *
            A *
            A *
            A) /
            720));
  if (!northern) northing += 10000000.0;
  return [easting, northing];
}

const pixelPath = "/tmp/s2_10061_pixel.json";
const fieldPath = "/tmp/field_xiaochang1.geojson";
if (!fs.existsSync(pixelPath)) {
  console.error("missing", pixelPath);
  process.exit(1);
}
const p = JSON.parse(fs.readFileSync(pixelPath, "utf8"));
const g = p.grid;
let maxDe = 0;
let maxDn = 0;
for (const row of p.pixels) {
  const r = row[0];
  const c = row[1];
  const e = g.origin_x + (c + 0.5) * g.resolution;
  const n = g.origin_y - (r + 0.5) * g.resolution;
  const [lon, lat] = utmToLonLat(e, n, g.epsg);
  const [e2, n2] = lonLatToUtm(lon, lat, g.epsg);
  maxDe = Math.max(maxDe, Math.abs(e2 - e));
  maxDn = Math.max(maxDn, Math.abs(n2 - n));
}

let fieldOverlap = null;
if (fs.existsSync(fieldPath)) {
  const geom = JSON.parse(fs.readFileSync(fieldPath, "utf8"));
  const ring =
    geom.type === "Polygon"
      ? geom.coordinates[0]
      : geom.coordinates[0][0];
  const projected = ring.map(([lon, lat]) => {
    const [e, n] = lonLatToUtm(lon, lat, g.epsg);
    return [(e - g.origin_x) / g.resolution, (g.origin_y - n) / g.resolution];
  });
  const cols = projected.map((p) => p[0]);
  const rows = projected.map((p) => p[1]);
  const minCol = Math.min(...cols);
  const maxCol = Math.max(...cols);
  const minRow = Math.min(...rows);
  const maxRow = Math.max(...rows);
  fieldOverlap =
    maxCol >= 0 && minCol <= g.width && maxRow >= 0 && minRow <= g.height;
  console.log(
    JSON.stringify(
      {
        grid: g,
        pixels: p.pixels.length,
        maxDe,
        maxDn,
        fieldCanvasBBox: { minCol, maxCol, minRow, maxRow },
        fieldOverlap,
        roundTripOk: maxDe < 0.01 && maxDn < 0.01,
        ok: maxDe < 0.01 && maxDn < 0.01 && fieldOverlap,
      },
      null,
      2,
    ),
  );
  if (!(maxDe < 0.01 && maxDn < 0.01 && fieldOverlap)) process.exit(1);
} else {
  console.log(JSON.stringify({ grid: g, maxDe, maxDn, roundTripOk: maxDe < 0.01 && maxDn < 0.01 }, null, 2));
  if (!(maxDe < 0.01 && maxDn < 0.01)) process.exit(1);
}

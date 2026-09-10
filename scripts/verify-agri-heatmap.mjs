/**
 * Offline verify: drought/flood classifiers + UTM grid vs field bbox for land 10072.
 * Reads /tmp/s2_10072.json and /tmp/s1_10072.json from a prior curl with include_pixels=1.
 */
import fs from "node:fs";

function computeNddi(ndvi, ndmi) {
  const denom = ndvi + ndmi;
  if (Math.abs(denom) < 1e-6) return null;
  return (ndvi - ndmi) / denom;
}
function classifyDrought(ndvi, ndmi) {
  const nddi = computeNddi(ndvi, ndmi);
  if (nddi != null) {
    if (nddi >= 0.5) return "severe";
    if (nddi >= 0.4) return "moderate";
    if (nddi >= 0.3) return "mild";
    return "normal";
  }
  if (ndmi < -0.2) return "severe";
  return "normal";
}
function classifyFlood(vv, vh) {
  if (vv <= -18 && (vh == null || vh <= -22)) return "flood";
  if (vv <= -18) return "flood";
  if (vv <= -15 || (vh != null && vv <= -14 && vh <= -20)) return "wet";
  return "dry";
}

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
          d * d * d * d * d * d) /
          720);
  const lon =
    (d -
      ((1 + 2 * t1 + c1) * d * d * d) / 6 +
      ((5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * c1 + 24 * t1 * t1) * d * d * d * d * d) /
        120) /
    Math.cos(phi1Rad);
  return [(lon * 180) / Math.PI + longOrigin, (lat * 180) / Math.PI];
}

const s2 = JSON.parse(fs.readFileSync("/tmp/s2_10072.json", "utf8")).items[0];
const s1 = JSON.parse(fs.readFileSync("/tmp/s1_10072.json", "utf8")).items[0];
const g = s2.pixel_data.grid;
const tl = utmToLonLat(g.origin_x, g.origin_y, g.epsg);
const br = utmToLonLat(
  g.origin_x + g.width * g.resolution,
  g.origin_y - g.height * g.resolution,
  g.epsg,
);
const fieldBbox = [115.71065115414392, 41.4439733655301, 115.72162509166735, 41.44747762179824];
const overlap =
  tl[0] <= fieldBbox[2] &&
  br[0] >= fieldBbox[0] &&
  br[1] <= fieldBbox[3] &&
  tl[1] >= fieldBbox[1];

let droughtPainted = 0;
for (const row of s2.pixel_data.pixels) {
  classifyDrought(row[6], row[4]);
  droughtPainted++;
}
let floodPainted = 0;
for (const row of s1.pixel_data.pixels) {
  const cls = classifyFlood(row[2], row[3]);
  if (cls !== "dry") floodPainted++;
}

console.log(
  JSON.stringify(
    {
      s2_pixels: s2.pixel_data.pixels.length,
      s1_pixels: s1.pixel_data.pixels.length,
      droughtPainted,
      floodPainted,
      heatmapTL: tl,
      heatmapBR: br,
      fieldBbox,
      overlap,
      ok: droughtPainted > 0 && floodPainted > 0 && overlap,
    },
    null,
    2,
  ),
);
if (!(droughtPainted > 0 && floodPainted > 0 && overlap)) process.exit(1);

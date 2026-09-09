/**
 * Agri 色斑图 helpers — prefer OSS lon/lat pixels (pixels_lonlat) rasterized at
 * ~10 m in Web Mercator into a continuous canvas color film (MapLibre image
 * source). Legacy DB grid pixel_data ([row,col,...]) is fallback only.
 * Optional field.geom mask in WebMercator canvas space (skipped if alpha≈0).
 * Map overlay uses image film only (no GeoJSON fill — white seams between cells).
 * geojson/points on AgriHeatmapImage remain for metadata / rebuilds, not map layers.
 *
 * OSS S2 pixel: {lon,lat,clear?,NDVI,EVI,NDMI,NDRE,CIre,MNDWI}
 * OSS S1 pixel: {lon,lat,VV_db,VH_db}
 * Legacy S2 row: [row, col, evi, cire, ndmi, ndre, ndvi, mndwi]
 * Legacy S1 row: [row, col, vv, vh]
 *
 * Modes:
 * - Continuous vegetation/SAR indices (NDVI/EVI/…)
 * - drought: NDDI (Gu et al., 2007) + NDMI moisture complement
 * - flood: Sentinel-1 VV/VH backscatter thresholding (operational S1 flood mapping)
 */

import * as turf from "@turf/turf";

export type AgriHeatIndex =
    | "ndvi"
    | "evi"
    | "ndmi"
    | "ndre"
    | "mndwi"
    | "cire"
    | "vv"
    | "vh"
    | "drought"
    | "flood";

export interface AgriPixelGrid {
    epsg: number;
    width: number;
    height: number;
    origin_x: number;
    origin_y: number;
    resolution: number;
}

export interface AgriPixelData {
    grid: AgriPixelGrid;
    pixels: number[][];
}

/** Preferred OSS lon/lat pixel (S2 optical or S1 SAR). */
export interface AgriLonLatPixel {
    lon: number;
    lat: number;
    clear?: number;
    NDVI?: number;
    EVI?: number;
    NDMI?: number;
    NDRE?: number;
    CIre?: number;
    MNDWI?: number;
    VV_db?: number;
    VH_db?: number;
    /** Allow alternate key casings from producers */
    [key: string]: number | undefined;
}


const S2_VALUE_INDEX: Record<"evi" | "cire" | "ndmi" | "ndre" | "ndvi" | "mndwi", number> = {
    evi: 2,
    cire: 3,
    ndmi: 4,
    ndre: 5,
    ndvi: 6,
    mndwi: 7,
};

const S1_VALUE_INDEX: Record<"vv" | "vh", number> = {
    vv: 2,
    vh: 3,
};

export function valueIndexFor(index: AgriHeatIndex, sensor: "S1" | "S2"): number | null {
    if (index === "drought") return sensor === "S2" ? S2_VALUE_INDEX.ndvi : null;
    if (index === "flood") return sensor === "S1" ? S1_VALUE_INDEX.vv : null;
    if (sensor === "S2" && index in S2_VALUE_INDEX) {
        return S2_VALUE_INDEX[index as keyof typeof S2_VALUE_INDEX];
    }
    if (sensor === "S1" && index in S1_VALUE_INDEX) {
        return S1_VALUE_INDEX[index as keyof typeof S1_VALUE_INDEX];
    }
    return null;
}

/** Sensor required for a heat mode. */
export function sensorForIndex(index: AgriHeatIndex): "S1" | "S2" {
    if (index === "flood" || index === "vv" || index === "vh") return "S1";
    return "S2";
}

/**
 * Default rescale ranges.
 * NDVI/EVI use figure-3 style fixed scales (0→high green) for continuous 色斑.
 */
export const HEAT_RESCALE: Record<Exclude<AgriHeatIndex, "drought" | "flood">, [number, number]> = {
    ndvi: [0, 0.9],
    evi: [0, 0.8],
    ndmi: [-0.5, 0.5],
    ndre: [-0.2, 0.8],
    mndwi: [-0.5, 0.5],
    cire: [0, 1.5],
    vv: [-25, 0],
    vh: [-30, -5],
};

function lerp(a: number, b: number, t: number) {
    return a + (b - a) * t;
}

function sampleStops(stops: [number, number, number][], t: number): [number, number, number] {
    const x = Math.max(0, Math.min(1, t));
    if (x <= 0) return stops[0];
    if (x >= 1) return stops[stops.length - 1];
    const pos = x * (stops.length - 1);
    const i = Math.floor(pos);
    const f = pos - i;
    const a = stops[i];
    const b = stops[Math.min(i + 1, stops.length - 1)];
    return [lerp(a[0], b[0], f), lerp(a[1], b[1], f), lerp(a[2], b[2], f)];
}

/** RdYlGn-like continuous ramp (red → orange → yellow → green), figure-3 style. */
const VEG_STOPS: [number, number, number][] = [
    [165, 0, 38],
    [215, 48, 39],
    [244, 109, 67],
    [253, 174, 97],
    [254, 224, 139],
    [255, 255, 191],
    [217, 239, 139],
    [166, 217, 106],
    [102, 189, 99],
    [26, 152, 80],
    [0, 104, 55],
];

const SAR_STOPS: [number, number, number][] = [
    [13, 8, 135],
    [70, 40, 180],
    [120, 80, 200],
    [180, 140, 220],
    [240, 200, 230],
    [255, 255, 255],
];

/** CSS gradient matching VEG_STOPS for map legends. */
export const VEG_GRADIENT_CSS =
    "linear-gradient(90deg, rgb(165,0,38), rgb(215,48,39), rgb(244,109,67), rgb(253,174,97), rgb(254,224,139), rgb(255,255,191), rgb(217,239,139), rgb(166,217,106), rgb(102,189,99), rgb(26,152,80), rgb(0,104,55))";

export type AgriDroughtClass = "severe" | "moderate" | "mild" | "normal";
export type AgriFloodClass = "flood" | "wet" | "dry";

export const DROUGHT_CLASS_STYLE: Record<
    AgriDroughtClass,
    { label: string; color: string; rgba: [number, number, number, number] }
> = {
    // Discrete drought classes — transparent outside painted pixels.
    severe: { label: "重度干旱", color: "#d73027", rgba: [215, 48, 39, 230] },
    moderate: { label: "中度干旱", color: "#fc8d59", rgba: [252, 141, 89, 220] },
    mild: { label: "轻度干旱", color: "#fee08b", rgba: [254, 224, 139, 210] },
    normal: { label: "正常/湿润", color: "#1a9850", rgba: [26, 152, 80, 200] },
};

export const FLOOD_CLASS_STYLE: Record<
    AgriFloodClass,
    { label: string; color: string; rgba: [number, number, number, number] }
> = {
    // Open water / flood: deep blue; wet soils: light blue; dry: transparent.
    flood: { label: "积水/洪涝", color: "#08519c", rgba: [8, 81, 156, 235] },
    wet: { label: "偏湿", color: "#6baed6", rgba: [107, 174, 214, 200] },
    dry: { label: "干燥地表", color: "#74c476", rgba: [116, 196, 118, 0] }, // alpha 0
};

/**
 * NDDI drought index (Gu, Brown, Verdin & Wardlow, 2007):
 *   NDDI = (NDVI − NDWI) / (NDVI + NDWI)
 * Here NDWI is approximated by Gao (1996) NDMI (NIR/SWIR moisture index),
 * available as `ndmi` in S2 pixel_data. Higher NDDI ⇒ stronger moisture stress.
 * Fallback: low NDMI alone indicates moisture stress when NDVI+NDMI≈0.
 */
export function computeNddi(ndvi: number, ndmi: number): number | null {
    if (!Number.isFinite(ndvi) || !Number.isFinite(ndmi)) return null;
    const denom = ndvi + ndmi;
    if (Math.abs(denom) < 1e-6) return null;
    return (ndvi - ndmi) / denom;
}

export function classifyDrought(ndvi: number, ndmi: number): AgriDroughtClass {
    const nddi = computeNddi(ndvi, ndmi);
    if (nddi == null) {
        // Complement: Gao NDMI moisture stress thresholds
        if (ndmi < -0.2) return "severe";
        if (ndmi < 0) return "moderate";
        if (ndmi < 0.1) return "mild";
        return "normal";
    }
    if (nddi >= 0.5 || ndmi < -0.2) return "severe";
    if (nddi >= 0.3 || ndmi < 0) return "moderate";
    if (nddi >= 0.1 || ndmi < 0.1) return "mild";
    return "normal";
}

/**
 * Sentinel-1 open-water / flood mapping via VV (and VH) backscatter thresholds.
 * Operational S1 flood literature (e.g. Martinis-style / global flood mapping)
 * commonly treats calm open water as very low VV (often ≲ −15…−18 dB);
 * dual-pol (low VV + low VH) strengthens the water class.
 * Values expected in dB from agri pixel_data.
 */
export function classifyFlood(vvDb: number, vhDb: number | null): AgriFloodClass {
    if (!Number.isFinite(vvDb)) return "dry";
    const vh = vhDb != null && Number.isFinite(vhDb) ? vhDb : null;
    // Strong water: VV very low; dual-pol confirms when VH available
    if (vvDb <= -18 && (vh == null || vh <= -22)) return "flood";
    if (vvDb <= -18) return "flood";
    if (vvDb <= -15 || (vh != null && vvDb <= -14 && vh <= -20)) return "wet";
    return "dry";
}

export function colorizeValue(
    value: number,
    index: Exclude<AgriHeatIndex, "drought" | "flood">,
    rescale: [number, number] = HEAT_RESCALE[index],
): [number, number, number, number] {
    const [lo, hi] = rescale;
    const t = hi === lo ? 0.5 : (value - lo) / (hi - lo);
    const stops = index === "vv" || index === "vh" ? SAR_STOPS : VEG_STOPS;
    const [r, g, b] = sampleStops(stops, t);
    return [Math.round(r), Math.round(g), Math.round(b), 230];
}

/** Convert WGS84 UTM (EPSG:326xx / 327xx) → lon/lat. */
export function utmToLonLat(
    easting: number,
    northing: number,
    epsg: number,
): [number, number] {
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


/** WGS84 lon/lat → Web Mercator meters (EPSG:3857). */
export function lonLatToWebMercator(lon: number, lat: number): [number, number] {
    const x = (lon * 20037508.342789244) / 180;
    let y = Math.log(Math.tan(((90 + lat) * Math.PI) / 360)) / (Math.PI / 180);
    y = (y * 20037508.342789244) / 180;
    return [x, y];
}

/** Web Mercator meters → WGS84 lon/lat. */
export function webMercatorToLonLat(x: number, y: number): [number, number] {
    const lon = (x / 20037508.342789244) * 180;
    let lat = (y / 20037508.342789244) * 180;
    lat = (180 / Math.PI) * (2 * Math.atan(Math.exp((lat * Math.PI) / 180)) - Math.PI / 2);
    return [lon, lat];
}

function numProp(p: AgriLonLatPixel, ...keys: string[]): number {
    for (const k of keys) {
        const v = p[k];
        if (typeof v === "number" && Number.isFinite(v)) return v;
        const lower = k.toLowerCase();
        for (const [pk, pv] of Object.entries(p)) {
            if (pk.toLowerCase() === lower && typeof pv === "number" && Number.isFinite(pv)) {
                return pv;
            }
        }
    }
    return NaN;
}

/** Convert lon/lat → WGS84 UTM easting/northing for the given EPSG:326xx / 327xx zone. */
export function lonLatToUtm(lon: number, lat: number, epsg: number): [number, number] {
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
                ((5 - 18 * T + T * T + 72 * C - 58 * eccPrimeSquared) * A * A * A * A * A) / 120) +
        500000.0;
    let northing =
        k0 *
        (M +
            N *
                Math.tan(latRad) *
                ((A * A) / 2 +
                    ((5 - T + 9 * C + 4 * C * C) * A * A * A * A) / 24 +
                    ((61 - 58 * T + T * T + 600 * C - 330 * eccPrimeSquared) * A * A * A * A * A * A) /
                        720));
    if (!northern) northing += 10000000.0;
    return [easting, northing];
}

export type AgriHeatmapLegend =
    | {
          kind: "continuous";
          label: string;
          min: number;
          max: number;
          gradient: string;
      }
    | {
          kind: "classes";
          label: string;
          hint?: string;
          classes: { key: string; label: string; color: string }[];
      };

export type AgriHeatmapGeoJSON = GeoJSON.FeatureCollection<
    GeoJSON.Polygon,
    {
        color: string;
        value: number;
        class?: string;
        row?: number;
        col?: number;
        rgba?: [number, number, number, number];
    }
>;

export type AgriHeatmapPoints = GeoJSON.FeatureCollection<
    GeoJSON.Point,
    {
        color: string;
        value: number;
        class?: string;
        rgba?: [number, number, number, number];
    }
>;

export interface AgriHeatmapImage {
    /** Continuous color-film PNG (primary MapLibre image source). */
    dataUrl?: string;
    /** MapLibre image coordinates: TL, TR, BR, BL as [lng, lat]. */
    coordinates: [[number, number], [number, number], [number, number], [number, number]];
    /** Sparse 10 m UTM cells as WGS84 polygons — fallback if image overlay unavailable. */
    geojson: AgriHeatmapGeoJSON;
    /**
     * Lon/lat sample centers (optional / debug). Primary map overlay is the
     * canvas image film — circle layer is disabled by default.
     */
    points?: AgriHeatmapPoints;
    /** True when built from OSS lon/lat pixels (WebMercator film). */
    fromLonLat?: boolean;
    /** Source grid (WebMercator 3857 for lon/lat films; UTM for legacy DB). */
    grid: AgriPixelGrid;
    width: number;
    height: number;
    index: AgriHeatIndex;
    pixelCount: number;
    /** Mean of continuous index values (or NDDI / VV for modes). */
    mean: number | null;
    min: number | null;
    max: number | null;
    legend: AgriHeatmapLegend;
}

function continuousLegend(index: Exclude<AgriHeatIndex, "drought" | "flood">): AgriHeatmapLegend {
    const [min, max] = HEAT_RESCALE[index];
    const isSar = index === "vv" || index === "vh";
    return {
        kind: "continuous",
        label: index.toUpperCase(),
        min,
        max,
        gradient: isSar
            ? "linear-gradient(90deg, rgb(13,8,135), rgb(120,80,200), rgb(255,255,255))"
            : VEG_GRADIENT_CSS,
    };
}

function droughtLegend(): AgriHeatmapLegend {
    return {
        kind: "classes",
        label: "干旱 NDDI",
        hint: "NDDI=(NDVI−NDMI)/(NDVI+NDMI) · Gu et al. 2007",
        classes: (Object.keys(DROUGHT_CLASS_STYLE) as AgriDroughtClass[]).map((k) => ({
            key: k,
            label: DROUGHT_CLASS_STYLE[k].label,
            color: DROUGHT_CLASS_STYLE[k].color,
        })),
    };
}

function floodLegend(): AgriHeatmapLegend {
    return {
        kind: "classes",
        label: "洪涝 S1",
        hint: "VV/VH 后向散射阈值 · 积水≈VV≲−18 dB",
        classes: (["flood", "wet"] as AgriFloodClass[]).map((k) => ({
            key: k,
            label: FLOOD_CLASS_STYLE[k].label,
            color: FLOOD_CLASS_STYLE[k].color,
        })),
    };
}

function gridCorners(grid: AgriPixelGrid): AgriHeatmapImage["coordinates"] {
    const { width, height, origin_x, origin_y, resolution, epsg } = grid;
    const tl = utmToLonLat(origin_x, origin_y, epsg);
    const tr = utmToLonLat(origin_x + width * resolution, origin_y, epsg);
    const br = utmToLonLat(origin_x + width * resolution, origin_y - height * resolution, epsg);
    const bl = utmToLonLat(origin_x, origin_y - height * resolution, epsg);
    return [tl, tr, br, bl];
}

function rgbaToHex(r: number, g: number, b: number): string {
    const h = (n: number) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0");
    return `#${h(r)}${h(g)}${h(b)}`;
}

interface PaintedCell {
    row: number;
    col: number;
    color: string;
    rgba: [number, number, number, number];
    value: number;
    class?: string;
}

interface PaintedResult {
    cells: PaintedCell[];
    sum: number;
    vmin: number;
    vmax: number;
}

/** Shared coloring for continuous / drought / flood modes. */
function collectPaintedCells(
    pixelData: AgriPixelData,
    index: AgriHeatIndex,
    sensor: "S1" | "S2",
    rescale?: [number, number],
): PaintedResult | null {
    const grid = pixelData?.grid;
    const pixels = pixelData?.pixels;
    if (!grid || !pixels?.length) return null;

    const expected = sensorForIndex(index);
    if (sensor !== expected) return null;

    const { width, height } = grid;
    if (!width || !height || !grid.resolution || !grid.epsg) return null;

    const cells: PaintedCell[] = [];
    let sum = 0;
    let vmin = Infinity;
    let vmax = -Infinity;

    if (index === "drought") {
        const iNdvi = S2_VALUE_INDEX.ndvi;
        const iNdmi = S2_VALUE_INDEX.ndmi;
        for (const row of pixels) {
            if (!Array.isArray(row) || row.length <= Math.max(iNdvi, iNdmi)) continue;
            const r = Number(row[0]);
            const c = Number(row[1]);
            const ndvi = Number(row[iNdvi]);
            const ndmi = Number(row[iNdmi]);
            if (!Number.isFinite(r) || !Number.isFinite(c)) continue;
            if (r < 0 || r >= height || c < 0 || c >= width) continue;
            if (!Number.isFinite(ndvi) || !Number.isFinite(ndmi)) continue;
            const cls = classifyDrought(ndvi, ndmi);
            const rgba = DROUGHT_CLASS_STYLE[cls].rgba;
            if (rgba[3] === 0) continue;
            const nddi = computeNddi(ndvi, ndmi);
            const metric = nddi ?? ndmi;
            if (!Number.isFinite(metric)) continue;
            cells.push({
                row: r,
                col: c,
                color: DROUGHT_CLASS_STYLE[cls].color,
                rgba,
                value: metric,
                class: cls,
            });
            sum += metric;
            vmin = Math.min(vmin, metric);
            vmax = Math.max(vmax, metric);
        }
    } else if (index === "flood") {
        const iVv = S1_VALUE_INDEX.vv;
        const iVh = S1_VALUE_INDEX.vh;
        for (const row of pixels) {
            if (!Array.isArray(row) || row.length <= iVv) continue;
            const r = Number(row[0]);
            const c = Number(row[1]);
            const vv = Number(row[iVv]);
            const vh = row.length > iVh ? Number(row[iVh]) : NaN;
            if (!Number.isFinite(r) || !Number.isFinite(c) || !Number.isFinite(vv)) continue;
            if (r < 0 || r >= height || c < 0 || c >= width) continue;
            const cls = classifyFlood(vv, Number.isFinite(vh) ? vh : null);
            const rgba = FLOOD_CLASS_STYLE[cls].rgba;
            if (rgba[3] === 0) continue; // dry → transparent
            cells.push({
                row: r,
                col: c,
                color: FLOOD_CLASS_STYLE[cls].color,
                rgba,
                value: vv,
                class: cls,
            });
            sum += vv;
            vmin = Math.min(vmin, vv);
            vmax = Math.max(vmax, vv);
        }
    } else {
        const cont = index as Exclude<AgriHeatIndex, "drought" | "flood">;
        const vi = valueIndexFor(cont, sensor);
        if (vi == null) return null;
        const rs = rescale ?? HEAT_RESCALE[cont];
        for (const row of pixels) {
            if (!Array.isArray(row) || row.length <= vi) continue;
            const r = Number(row[0]);
            const c = Number(row[1]);
            const v = Number(row[vi]);
            if (!Number.isFinite(r) || !Number.isFinite(c) || !Number.isFinite(v)) continue;
            if (r < 0 || r >= height || c < 0 || c >= width) continue;
            const rgba = colorizeValue(v, cont, rs);
            cells.push({
                row: r,
                col: c,
                color: rgbaToHex(rgba[0], rgba[1], rgba[2]),
                rgba,
                value: v,
            });
            sum += v;
            vmin = Math.min(vmin, v);
            vmax = Math.max(vmax, v);
        }
    }

    if (cells.length === 0) return null;
    // Real DB pixels only — no NN hole-filling / fake cells.
    return { cells, sum, vmin, vmax };
}

/** One 10 m UTM cell → exact WGS84 square polygon (ring closed). No padding; clip to field boundary instead. */
function cellPolygon(
    grid: AgriPixelGrid,
    row: number,
    col: number,
): GeoJSON.Polygon {
    const { origin_x, origin_y, resolution, epsg } = grid;
    const west = origin_x + col * resolution;
    const east = origin_x + (col + 1) * resolution;
    const north = origin_y - row * resolution;
    const south = origin_y - (row + 1) * resolution;
    const tl = utmToLonLat(west, north, epsg);
    const tr = utmToLonLat(east, north, epsg);
    const br = utmToLonLat(east, south, epsg);
    const bl = utmToLonLat(west, south, epsg);
    return {
        type: "Polygon",
        coordinates: [[tl, tr, br, bl, tl]],
    };
}

/**
 * Sparse pixel cells → GeoJSON FeatureCollection of fill polygons.
 * Transparent outside cells (no features). Properties drive MapLibre fill-color.
 */
export function pixelsToGeoJSON(
    pixelData: AgriPixelData,
    index: AgriHeatIndex,
    sensor: "S1" | "S2",
    rescale?: [number, number],
): AgriHeatmapGeoJSON | null {
    const painted = collectPaintedCells(pixelData, index, sensor, rescale);
    if (!painted) return null;
    const grid = pixelData.grid;
    const features: AgriHeatmapGeoJSON["features"] = painted.cells.map((cell) => {
        const props: AgriHeatmapGeoJSON["features"][number]["properties"] = {
            color: cell.color,
            value: cell.value,
            row: cell.row,
            col: cell.col,
            rgba: cell.rgba,
        };
        if (cell.class) props.class = cell.class;
        return {
            type: "Feature",
            properties: props,
            geometry: cellPolygon(grid, cell.row, cell.col),
        };
    });
    return { type: "FeatureCollection", features };
}

/** Project a lon/lat ring into canvas pixel space (col, row) for the grid CRS. */
function projectRingToCanvas(ring: number[][], grid: AgriPixelGrid): [number, number][] {
    const out: [number, number][] = [];
    for (const pt of ring) {
        if (!pt || pt.length < 2) continue;
        const lon = Number(pt[0]);
        const lat = Number(pt[1]);
        if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
        let x: number;
        let y: number;
        if (grid.epsg === 4326) {
            x = lon;
            y = lat;
        } else if (grid.epsg === 3857) {
            [x, y] = lonLatToWebMercator(lon, lat);
        } else {
            [x, y] = lonLatToUtm(lon, lat, grid.epsg);
        }
        const col = (x - grid.origin_x) / grid.resolution;
        const row = (grid.origin_y - y) / grid.resolution;
        out.push([col, row]);
    }
    return out;
}

/** Count pixels with non-zero alpha on a canvas (post-paint / post-mask health check). */
export function countNonZeroAlpha(
    ctx: CanvasRenderingContext2D,
    width: number,
    height: number,
): number {
    if (!width || !height) return 0;
    const data = ctx.getImageData(0, 0, width, height).data;
    let n = 0;
    for (let i = 3; i < data.length; i += 4) {
        if (data[i] !== 0) n++;
    }
    return n;
}

/**
 * True when a heatmap image overlay is safe to show on the map.
 * Empty / fully-transparent dataUrls are truthy strings — callers must not treat
 * them as success or GeoJSON fallback will never run.
 */
export function heatmapImageHasContent(
    hm: Pick<AgriHeatmapImage, "dataUrl" | "pixelCount"> | null | undefined,
): boolean {
    if (!hm?.dataUrl) return false;
    if (typeof hm.pixelCount === "number" && hm.pixelCount <= 0) return false;
    return true;
}


/** Decode a data:image/...;base64 URL into a blob: object URL (MapLibre ImageSource). */
export function dataUrlToObjectUrl(dataUrl: string): string {
    const comma = dataUrl.indexOf(",");
    if (comma < 0) throw new Error("invalid data URL");
    const header = dataUrl.slice(0, comma);
    const payload = dataUrl.slice(comma + 1);
    const mime = /data:([^;,]+)/i.exec(header)?.[1] || "image/png";
    const isBase64 = /;base64/i.test(header);
    let bytes: Uint8Array;
    if (isBase64) {
        const bin = atob(payload);
        bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    } else {
        const decoded = decodeURIComponent(payload);
        bytes = new Uint8Array(decoded.length);
        for (let i = 0; i < decoded.length; i++) bytes[i] = decoded.charCodeAt(i);
    }
    const ab = new ArrayBuffer(bytes.byteLength);
    new Uint8Array(ab).set(bytes);
    return URL.createObjectURL(new Blob([ab], { type: mime }));
}

/**
 * Prefer canvas.toBlob → URL.createObjectURL for MapLibre ImageSource.
 * Falls back to toDataURL → blob URL if toBlob yields null.
 */
export function canvasToObjectUrl(canvas: HTMLCanvasElement): Promise<string> {
    return new Promise((resolve, reject) => {
        try {
            canvas.toBlob((blob) => {
                if (blob) {
                    resolve(URL.createObjectURL(blob));
                    return;
                }
                try {
                    resolve(dataUrlToObjectUrl(canvas.toDataURL("image/png")));
                } catch (e) {
                    reject(e);
                }
            }, "image/png");
        } catch (e) {
            reject(e);
        }
    });
}

/** Revoke blob: object URLs created for heatmap ImageSource. */
export function revokeHeatmapObjectUrl(url: string | null | undefined): void {
    if (!url || !url.startsWith("blob:")) return;
    try {
        URL.revokeObjectURL(url);
    } catch {
        /* ignore */
    }
}

/**
 * Zero-out canvas pixels outside field.geom (destination-in polygon mask in grid space).
 * Returns false if no usable ring was projected (caller should skip / restore).
 * Empty / outside stays transparent so the basemap shows through.
 */
export function maskCanvasToFieldGeom(
    ctx: CanvasRenderingContext2D,
    grid: AgriPixelGrid,
    fieldGeom: GeoJSON.Polygon | GeoJSON.MultiPolygon,
): boolean {
    const polys =
        fieldGeom.type === "Polygon"
            ? [fieldGeom.coordinates]
            : fieldGeom.type === "MultiPolygon"
              ? fieldGeom.coordinates
              : [];
    if (!polys.length) return false;

    let rings = 0;
    ctx.save();
    ctx.globalCompositeOperation = "destination-in";
    ctx.beginPath();
    for (const poly of polys) {
        if (!poly?.length) continue;
        const exterior = projectRingToCanvas(poly[0], grid);
        if (exterior.length < 3) continue;
        ctx.moveTo(exterior[0][0], exterior[0][1]);
        for (let i = 1; i < exterior.length; i++) {
            ctx.lineTo(exterior[i][0], exterior[i][1]);
        }
        ctx.closePath();
        rings++;
        for (let h = 1; h < poly.length; h++) {
            const hole = projectRingToCanvas(poly[h], grid);
            if (hole.length < 3) continue;
            ctx.moveTo(hole[0][0], hole[0][1]);
            for (let i = 1; i < hole.length; i++) {
                ctx.lineTo(hole[i][0], hole[i][1]);
            }
            ctx.closePath();
        }
    }
    if (rings === 0) {
        ctx.restore();
        return false;
    }
    ctx.fill("evenodd");
    ctx.restore();
    return true;
}

/**
 * Apply destination-in field mask only when it keeps enough painted alpha.
 * Broken lonLat↔UTM masks can wipe the canvas to a transparent PNG — that must
 * not become the MapLibre image source (GeoJSON fallback would never run).
 * On failure, restores the unmasked film so color still shows near the boundary.
 */
/**
 * Best-effort field mask. Destination-in lonLat↔UTM masks have wiped the film
 * before (transparent dataUrl → blank map, GeoJSON never ran). For EPSG:3857 /
 * 4326 lon/lat films, apply mask but restore unmasked when alpha is wiped.
 */
/**
 * Apply destination-in field mask only for WebMercator / lonlat canvas grids.
 * Never run lonLatToUtm against an EPSG:3857 grid (that wiped the film before).
 * UTM legacy grids skip mask here; page may still soft-clip GeoJSON separately.
 * applyFieldMaskIfHealthy restores unmasked pixels if alpha≈0 after mask.
 */
function shouldApplyCanvasFieldMask(grid: AgriPixelGrid): boolean {
    return grid.epsg === 3857 || grid.epsg === 4326;
}

function applyFieldMaskIfHealthy(
    ctx: CanvasRenderingContext2D,
    grid: AgriPixelGrid,
    fieldGeom: GeoJSON.Polygon | GeoJSON.MultiPolygon | null | undefined,
    width: number,
    height: number,
    expectedPainted: number,
): number {
    const painted = countNonZeroAlpha(ctx, width, height);
    if (!shouldApplyCanvasFieldMask(grid)) {
        return painted;
    }
    if (
        !fieldGeom ||
        (fieldGeom.type !== "Polygon" && fieldGeom.type !== "MultiPolygon") ||
        painted === 0
    ) {
        return painted;
    }
    const unmasked = ctx.getImageData(0, 0, width, height);
    const maskedOk = maskCanvasToFieldGeom(ctx, grid, fieldGeom);
    if (!maskedOk) {
        ctx.putImageData(unmasked, 0, 0);
        return painted;
    }
    const after = countNonZeroAlpha(ctx, width, height);
    const minKeep = Math.max(1, Math.floor(Math.min(painted, Math.max(expectedPainted, 1)) * 0.02));
    if (after === 0 || after < minKeep) {
        // Mask wiped (or nearly wiped) the film — keep unmasked pixels.
        ctx.putImageData(unmasked, 0, 0);
        return painted;
    }
    return after;
}

function hexToRgba(hex: string, alpha = 230): [number, number, number, number] {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
    if (!m) return [0, 0, 0, alpha];
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, alpha];
}

/**
 * Rebuild continuous color-film dataUrl from geojson cells.
 * Field mask is best-effort (WebMercator/lonlat grids); empty alpha → no dataUrl
 * so callers can detect empty film (no GeoJSON fill fallback — seams).
 */
export function clipHeatmapImageToField(
    hm: AgriHeatmapImage,
    fieldGeom: GeoJSON.Polygon | GeoJSON.MultiPolygon | null | undefined,
): AgriHeatmapImage {
    if (typeof document === "undefined") return hm;
    const grid = hm.grid;
    if (!grid?.width || !grid?.height) return hm;

    try {
        const canvas = document.createElement("canvas");
        canvas.width = hm.width;
        canvas.height = hm.height;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        if (!ctx) return hm;

        // Full-cell film (expand slightly) — same style as rasterizeAgriLonLatPixels.
        ctx.imageSmoothingEnabled = false;
        const expand = 1.35;
        const inset = (expand - 1) / 2;
        let paintedCells = 0;
        for (const feat of hm.geojson?.features ?? []) {
            const row = Number(feat.properties?.row);
            const col = Number(feat.properties?.col);
            if (!Number.isFinite(row) || !Number.isFinite(col)) continue;
            if (row < 0 || row >= hm.height || col < 0 || col >= hm.width) continue;
            const rgbaProp = feat.properties?.rgba;
            const [r, g, b, a] =
                Array.isArray(rgbaProp) && rgbaProp.length >= 4
                    ? [
                          Number(rgbaProp[0]),
                          Number(rgbaProp[1]),
                          Number(rgbaProp[2]),
                          Number(rgbaProp[3]),
                      ]
                    : hexToRgba(feat.properties?.color ?? "#000000", 230);
            if (![r, g, b, a].every(Number.isFinite) || a === 0) continue;
            ctx.fillStyle = `rgba(${r},${g},${b},${a / 255})`;
            ctx.fillRect(col - inset, row - inset, expand, expand);
            paintedCells++;
        }
        const painted = applyFieldMaskIfHealthy(
            ctx,
            grid,
            fieldGeom,
            hm.width,
            hm.height,
            paintedCells || hm.pixelCount || 0,
        );
        // Transparent PNG is still a truthy dataUrl — omit it so GeoJSON fallback runs.
        if (painted === 0) {
            return { ...hm, dataUrl: undefined };
        }
        return { ...hm, dataUrl: canvas.toDataURL("image/png") };
    } catch {
        return hm;
    }
}

/**
 * Clip heatmap cell polygons to the field boundary (Polygon / MultiPolygon).
 * Reliable boundary clip via turf intersect — used when image film is empty/unusable.
 * Drops cells with empty intersection so 色斑 stays near the green outline.
 */
export function clipHeatmapToField(
    geojson: AgriHeatmapGeoJSON,
    fieldGeom: GeoJSON.Polygon | GeoJSON.MultiPolygon | null | undefined,
): AgriHeatmapGeoJSON {
    if (!fieldGeom || !geojson?.features?.length) return geojson;
    let clipFeature: GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>;
    try {
        clipFeature = turf.feature(fieldGeom) as GeoJSON.Feature<
            GeoJSON.Polygon | GeoJSON.MultiPolygon
        >;
    } catch {
        return geojson;
    }

    const features: AgriHeatmapGeoJSON["features"] = [];
    for (const feat of geojson.features) {
        if (!feat?.geometry || feat.geometry.type !== "Polygon") continue;
        try {
            if (!turf.booleanIntersects(feat as GeoJSON.Feature, clipFeature as GeoJSON.Feature)) {
                continue;
            }
            const props: AgriHeatmapGeoJSON["features"][number]["properties"] = {
                color: feat.properties?.color ?? "#000000",
                value: feat.properties?.value ?? 0,
            };
            if (feat.properties?.class) props.class = feat.properties.class;
            if (feat.properties?.row != null) props.row = feat.properties.row;
            if (feat.properties?.col != null) props.col = feat.properties.col;
            if (feat.properties?.rgba) props.rgba = feat.properties.rgba;
            // Tiny ~10 m lon/lat squares often fail turf.intersect (empty geom /
            // winding). Keep the whole cell whenever it intersects the field.
            let geom: GeoJSON.Polygon | GeoJSON.MultiPolygon | null = null;
            try {
                const clipped = turf.intersect(
                    turf.featureCollection([
                        turf.feature(feat.geometry) as GeoJSON.Feature<GeoJSON.Polygon>,
                        clipFeature,
                    ]),
                );
                if (clipped?.geometry && (clipped.geometry.type === "Polygon" || clipped.geometry.type === "MultiPolygon")) {
                    geom = clipped.geometry;
                }
            } catch {
                geom = null;
            }
            if (!geom) {
                features.push({ type: "Feature", properties: props, geometry: feat.geometry });
                continue;
            }
            if (geom.type === "Polygon") {
                features.push({ type: "Feature", properties: props, geometry: geom });
            } else if (geom.type === "MultiPolygon") {
                for (const coordinates of geom.coordinates) {
                    features.push({
                        type: "Feature",
                        properties: props,
                        geometry: { type: "Polygon", coordinates },
                    });
                }
            }
        } catch {
            /* skip degenerate intersections */
        }
    }
    return { type: "FeatureCollection", features };
}

/**
 * Soft field filter for lon/lat sample points: keep points inside the parcel via
 * booleanPointInPolygon only (no polygon intersect). If filtering empties the
 * set, return originals — OSS pixels are already land-sampled.
 */
export function filterHeatmapPointsInField(
    points: AgriHeatmapPoints,
    fieldGeom: GeoJSON.Polygon | GeoJSON.MultiPolygon | null | undefined,
): AgriHeatmapPoints {
    if (!fieldGeom || !points?.features?.length) return points;
    let poly: GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>;
    try {
        poly = turf.feature(fieldGeom) as GeoJSON.Feature<
            GeoJSON.Polygon | GeoJSON.MultiPolygon
        >;
    } catch {
        return points;
    }
    const features: AgriHeatmapPoints["features"] = [];
    for (const feat of points.features) {
        if (!feat?.geometry || feat.geometry.type !== "Point") continue;
        try {
            if (turf.booleanPointInPolygon(feat as GeoJSON.Feature<GeoJSON.Point>, poly)) {
                features.push(feat);
            }
        } catch {
            features.push(feat);
        }
    }
    if (features.length === 0 && points.features.length > 0) return points;
    return { type: "FeatureCollection", features };
}


/**
 * Build agri heatmap: continuous canvas color film (map overlay) + GeoJSON cells (metadata only).
 * Empty grid cells stay transparent — basemap shows through; only real pixel_data values.
 * Optional fieldGeom masks the canvas so nothing draws outside the parcel boundary.
 */
export function rasterizeAgriPixels(
    pixelData: AgriPixelData,
    index: AgriHeatIndex,
    sensor: "S1" | "S2",
    rescale?: [number, number],
    fieldGeom?: GeoJSON.Polygon | GeoJSON.MultiPolygon | null,
): AgriHeatmapImage | null {
    const painted = collectPaintedCells(pixelData, index, sensor, rescale);
    if (!painted) return null;
    const grid = pixelData.grid;
    const { width, height } = grid;
    const features: AgriHeatmapGeoJSON["features"] = painted.cells.map((cell) => {
        const props: AgriHeatmapGeoJSON["features"][number]["properties"] = {
            color: cell.color,
            value: cell.value,
            row: cell.row,
            col: cell.col,
            rgba: cell.rgba,
        };
        if (cell.class) props.class = cell.class;
        return {
            type: "Feature",
            properties: props,
            geometry: cellPolygon(grid, cell.row, cell.col),
        };
    });
    const geojson: AgriHeatmapGeoJSON = { type: "FeatureCollection", features };
    if (features.length === 0) return null;

    let dataUrl: string | undefined;
    try {
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        if (ctx) {
            const img = ctx.createImageData(width, height);
            for (const cell of painted.cells) {
                const i = (cell.row * width + cell.col) * 4;
                img.data[i] = cell.rgba[0];
                img.data[i + 1] = cell.rgba[1];
                img.data[i + 2] = cell.rgba[2];
                img.data[i + 3] = cell.rgba[3];
            }
            ctx.putImageData(img, 0, 0);
            const alphaCount = applyFieldMaskIfHealthy(
                ctx,
                grid,
                fieldGeom,
                width,
                height,
                painted.cells.length,
            );
            if (alphaCount > 0) {
                dataUrl = canvas.toDataURL("image/png");
            }
        }
    } catch {
        /* canvas optional in non-DOM contexts — GeoJSON fallback still returned */
    }

    const legend =
        index === "drought"
            ? droughtLegend()
            : index === "flood"
              ? floodLegend()
              : continuousLegend(index as Exclude<AgriHeatIndex, "drought" | "flood">);

    const paintedCount = painted.cells.length;
    return {
        dataUrl,
        coordinates: gridCorners(grid),
        geojson,
        grid,
        width,
        height,
        index,
        pixelCount: paintedCount,
        mean: paintedCount > 0 ? painted.sum / paintedCount : null,
        min: Number.isFinite(painted.vmin) ? painted.vmin : null,
        max: Number.isFinite(painted.vmax) ? painted.vmax : null,
        legend,
    };
}


interface LonLatPainted {
    lon: number;
    lat: number;
    color: string;
    rgba: [number, number, number, number];
    value: number;
    class?: string;
    row: number;
    col: number;
}

function collectLonLatPainted(
    pixels: AgriLonLatPixel[],
    index: AgriHeatIndex,
    sensor: "S1" | "S2",
    rescale?: [number, number],
): { cells: Omit<LonLatPainted, "row" | "col">[]; sum: number; vmin: number; vmax: number } | null {
    if (!pixels?.length) return null;
    const expected = sensorForIndex(index);
    if (sensor !== expected) return null;

    const cells: Omit<LonLatPainted, "row" | "col">[] = [];
    let sum = 0;
    let vmin = Infinity;
    let vmax = -Infinity;

    for (const p of pixels) {
        const lon = Number(p.lon);
        const lat = Number(p.lat);
        if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;

        if (index === "drought") {
            const ndvi = numProp(p, "NDVI");
            const ndmi = numProp(p, "NDMI");
            if (!Number.isFinite(ndvi) || !Number.isFinite(ndmi)) continue;
            const cls = classifyDrought(ndvi, ndmi);
            const rgba = DROUGHT_CLASS_STYLE[cls].rgba;
            if (rgba[3] === 0) continue;
            const nddi = computeNddi(ndvi, ndmi);
            const metric = nddi ?? ndmi;
            if (!Number.isFinite(metric)) continue;
            cells.push({
                lon,
                lat,
                color: DROUGHT_CLASS_STYLE[cls].color,
                rgba,
                value: metric,
                class: cls,
            });
            sum += metric;
            vmin = Math.min(vmin, metric);
            vmax = Math.max(vmax, metric);
            continue;
        }

        if (index === "flood") {
            const vv = numProp(p, "VV_db", "VV", "vv");
            const vh = numProp(p, "VH_db", "VH", "vh");
            if (!Number.isFinite(vv)) continue;
            const cls = classifyFlood(vv, Number.isFinite(vh) ? vh : null);
            const rgba = FLOOD_CLASS_STYLE[cls].rgba;
            if (rgba[3] === 0) continue;
            cells.push({
                lon,
                lat,
                color: FLOOD_CLASS_STYLE[cls].color,
                rgba,
                value: vv,
                class: cls,
            });
            sum += vv;
            vmin = Math.min(vmin, vv);
            vmax = Math.max(vmax, vv);
            continue;
        }

        const cont = index as Exclude<AgriHeatIndex, "drought" | "flood">;
        const keyMap: Record<typeof cont, string[]> = {
            ndvi: ["NDVI"],
            evi: ["EVI"],
            ndmi: ["NDMI"],
            ndre: ["NDRE"],
            mndwi: ["MNDWI"],
            cire: ["CIre", "CIRE", "cire"],
            vv: ["VV_db", "VV", "vv"],
            vh: ["VH_db", "VH", "vh"],
        };
        const v = numProp(p, ...(keyMap[cont] ?? [cont.toUpperCase()]));
        if (!Number.isFinite(v)) continue;
        const rs = rescale ?? HEAT_RESCALE[cont];
        const rgba = colorizeValue(v, cont, rs);
        cells.push({
            lon,
            lat,
            color: rgbaToHex(rgba[0], rgba[1], rgba[2]),
            rgba,
            value: v,
        });
        sum += v;
        vmin = Math.min(vmin, v);
        vmax = Math.max(vmax, v);
    }

    if (!cells.length) return null;
    return { cells, sum, vmin, vmax };
}

function lonLatCellPolygon(lon: number, lat: number, halfDegLon: number, halfDegLat: number): GeoJSON.Polygon {
    const west = lon - halfDegLon;
    const east = lon + halfDegLon;
    const south = lat - halfDegLat;
    const north = lat + halfDegLat;
    const tl: [number, number] = [west, north];
    const tr: [number, number] = [east, north];
    const br: [number, number] = [east, south];
    const bl: [number, number] = [west, south];
    return { type: "Polygon", coordinates: [[tl, tr, br, bl, tl]] };
}

/**
 * Rasterize OSS lon/lat point list into a ~10 m WebMercator color film + GeoJSON cells.
 * Primary path for 色膜 — avoids lossy DB grid row/col collisions/holes.
 */
export function rasterizeAgriLonLatPixels(
    pixels: AgriLonLatPixel[],
    index: AgriHeatIndex,
    sensor: "S1" | "S2",
    rescale?: [number, number],
    fieldGeom?: GeoJSON.Polygon | GeoJSON.MultiPolygon | null,
    resM = 10,
): AgriHeatmapImage | null {
    const painted = collectLonLatPainted(pixels, index, sensor, rescale);
    if (!painted) return null;

    const merc = painted.cells.map((c) => {
        const [x, y] = lonLatToWebMercator(c.lon, c.lat);
        return { ...c, x, y };
    });

    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const c of merc) {
        minX = Math.min(minX, c.x);
        maxX = Math.max(maxX, c.x);
        minY = Math.min(minY, c.y);
        maxY = Math.max(maxY, c.y);
    }
    // Pad half a cell so edge points are fully inside the canvas.
    const pad = resM * 0.5;
    minX -= pad;
    maxX += pad;
    minY -= pad;
    maxY += pad;

    const width = Math.max(1, Math.ceil((maxX - minX) / resM));
    const height = Math.max(1, Math.ceil((maxY - minY) / resM));
    // Cap pathological sizes (should be small for single parcels).
    if (width * height > 2000 * 2000) return null;

    const grid: AgriPixelGrid = {
        epsg: 3857,
        width,
        height,
        origin_x: minX,
        origin_y: maxY,
        resolution: resM,
    };

    const meanLat =
        painted.cells.reduce((s, c) => s + c.lat, 0) / Math.max(1, painted.cells.length);
    const metersPerDegLat = 111320;
    const metersPerDegLon = Math.max(1e-6, 111320 * Math.cos((meanLat * Math.PI) / 180));
    // Slight expand so adjacent ~10 m cells abut without hairline gaps (no turf clip).
    const halfDegLat = ((resM / 2) * 1.02) / metersPerDegLat;
    const halfDegLon = ((resM / 2) * 1.02) / metersPerDegLon;

    const cellsWithRc: LonLatPainted[] = merc.map((c) => {
        const col = Math.min(width - 1, Math.max(0, Math.floor((c.x - minX) / resM)));
        const row = Math.min(height - 1, Math.max(0, Math.floor((maxY - c.y) / resM)));
        return { ...c, row, col };
    });

    const features: AgriHeatmapGeoJSON["features"] = cellsWithRc.map((cell) => {
        const props: AgriHeatmapGeoJSON["features"][number]["properties"] = {
            color: cell.color,
            value: cell.value,
            row: cell.row,
            col: cell.col,
            rgba: cell.rgba,
        };
        if (cell.class) props.class = cell.class;
        return {
            type: "Feature",
            properties: props,
            geometry: lonLatCellPolygon(cell.lon, cell.lat, halfDegLon, halfDegLat),
        };
    });
    const geojson: AgriHeatmapGeoJSON = { type: "FeatureCollection", features };

    const points: AgriHeatmapPoints = {
        type: "FeatureCollection",
        features: cellsWithRc.map((cell) => {
            const props: AgriHeatmapPoints["features"][number]["properties"] = {
                color: cell.color,
                value: cell.value,
                rgba: cell.rgba,
            };
            if (cell.class) props.class = cell.class;
            return {
                type: "Feature",
                properties: props,
                geometry: { type: "Point", coordinates: [cell.lon, cell.lat] },
            };
        }),
    };

    const tl = webMercatorToLonLat(minX, maxY);
    const tr = webMercatorToLonLat(minX + width * resM, maxY);
    const br = webMercatorToLonLat(minX + width * resM, maxY - height * resM);
    const bl = webMercatorToLonLat(minX, maxY - height * resM);
    const coordinates: AgriHeatmapImage["coordinates"] = [tl, tr, br, bl];

    let dataUrl: string | undefined;
    try {
        if (typeof document !== "undefined") {
            const canvas = document.createElement("canvas");
            canvas.width = width;
            canvas.height = height;
            const ctx = canvas.getContext("2d", { willReadFrequently: true });
            if (ctx) {
                // Continuous solid film: each sample paints its full ~10 m cell.
                // Slightly expand (≥1.35) to kill hairline gaps between adjacent cells.
                // No arcs/circles — those leave satellite basemap showing through.
                ctx.imageSmoothingEnabled = false;
                const expand = 1.35;
                const inset = (expand - 1) / 2;
                for (const cell of cellsWithRc) {
                    const [r, g, b, a] = cell.rgba;
                    ctx.fillStyle = `rgba(${r},${g},${b},${a / 255})`;
                    ctx.fillRect(cell.col - inset, cell.row - inset, expand, expand);
                }
                const alphaCount = applyFieldMaskIfHealthy(
                    ctx,
                    grid,
                    fieldGeom,
                    width,
                    height,
                    cellsWithRc.length,
                );
                if (alphaCount > 0) {
                    dataUrl = canvas.toDataURL("image/png");
                }
            }
        }
    } catch {
        /* canvas optional */
    }

    const legend =
        index === "drought"
            ? droughtLegend()
            : index === "flood"
              ? floodLegend()
              : continuousLegend(index as Exclude<AgriHeatIndex, "drought" | "flood">);

    const paintedCount = cellsWithRc.length;
    return {
        dataUrl,
        coordinates,
        geojson,
        points,
        fromLonLat: true,
        grid,
        width,
        height,
        index,
        pixelCount: paintedCount,
        mean: paintedCount > 0 ? painted.sum / paintedCount : null,
        min: Number.isFinite(painted.vmin) ? painted.vmin : null,
        max: Number.isFinite(painted.vmax) ? painted.vmax : null,
        legend,
    };
}

/** Mode labels for UI chips (Chinese). */
export const AGRI_MODE_LABELS: Record<AgriHeatIndex, string> = {
    ndvi: "NDVI",
    evi: "EVI",
    ndmi: "NDMI",
    ndre: "NDRE",
    mndwi: "MNDWI",
    cire: "CIRE",
    vv: "VV",
    vh: "VH",
    drought: "干旱",
    flood: "洪涝",
};

/** Primary map-bottom modes requested next to NDVI/EVI. */
export const AGRI_PRIMARY_MODES: AgriHeatIndex[] = ["ndvi", "evi", "drought", "flood"];

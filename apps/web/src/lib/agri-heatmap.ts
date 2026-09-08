/**
 * Agri 色斑图 helpers — convert parcel_scene_products.pixel_data sparse pixels
 * into WGS84 GeoJSON fill cells (primary MapLibre overlay) + optional canvas.
 *
 * S2 pixel row: [row, col, evi, cire, ndmi, ndre, ndvi, mndwi]
 * S1 pixel row: [row, col, vv, vh]
 *
 * Modes:
 * - Continuous vegetation/SAR indices (NDVI/EVI/…)
 * - drought: NDDI (Gu et al., 2007) + NDMI moisture complement
 * - flood: Sentinel-1 VV/VH backscatter thresholding (operational S1 flood mapping)
 */

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
    { color: string; value: number; class?: string }
>;

export interface AgriHeatmapImage {
    /** Optional canvas data URL (legend / debug); map uses geojson. */
    dataUrl?: string;
    /** MapLibre image coordinates: TL, TR, BR, BL as [lng, lat] (legacy image overlay). */
    coordinates: [[number, number], [number, number], [number, number], [number, number]];
    /** Sparse 10 m UTM cells as WGS84 polygons — primary map overlay. */
    geojson: AgriHeatmapGeoJSON;
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
    return { cells, sum, vmin, vmax };
}

/** One 10 m UTM cell → WGS84 square polygon (ring closed). */
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
        const props: { color: string; value: number; class?: string } = {
            color: cell.color,
            value: cell.value,
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

/**
 * Build agri 色斑图: GeoJSON fill cells (primary) + optional canvas dataUrl (legacy/debug).
 * Empty cells stay absent so the satellite basemap shows through outside painted spots.
 */
export function rasterizeAgriPixels(
    pixelData: AgriPixelData,
    index: AgriHeatIndex,
    sensor: "S1" | "S2",
    rescale?: [number, number],
): AgriHeatmapImage | null {
    const painted = collectPaintedCells(pixelData, index, sensor, rescale);
    if (!painted) return null;
    const grid = pixelData.grid;
    const { width, height } = grid;
    const features: AgriHeatmapGeoJSON["features"] = painted.cells.map((cell) => {
        const props: { color: string; value: number; class?: string } = {
            color: cell.color,
            value: cell.value,
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
        const ctx = canvas.getContext("2d");
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
            dataUrl = canvas.toDataURL("image/png");
        }
    } catch {
        /* canvas optional in non-DOM contexts */
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

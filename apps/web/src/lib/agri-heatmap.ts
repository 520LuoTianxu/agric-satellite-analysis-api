/**
 * Agri 色斑图 helpers — rasterize parcel_scene_products.pixel_data onto a canvas
 * and project UTM grid corners to WGS84 for MapLibre image sources.
 *
 * S2 pixel row: [row, col, evi, cire, ndmi, ndre, ndvi, mndwi]
 * S1 pixel row: [row, col, vv, vh]
 */

export type AgriHeatIndex = "ndvi" | "evi" | "ndmi" | "ndre" | "mndwi" | "cire" | "vv" | "vh";

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
    if (sensor === "S2" && index in S2_VALUE_INDEX) {
        return S2_VALUE_INDEX[index as keyof typeof S2_VALUE_INDEX];
    }
    if (sensor === "S1" && index in S1_VALUE_INDEX) {
        return S1_VALUE_INDEX[index as keyof typeof S1_VALUE_INDEX];
    }
    return null;
}

/** Default rescale ranges (match OpenFarm INDEX_CONFIG / SAR dB). */
export const HEAT_RESCALE: Record<AgriHeatIndex, [number, number]> = {
    ndvi: [-0.2, 0.9],
    evi: [-0.2, 0.8],
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

export function colorizeValue(
    value: number,
    index: AgriHeatIndex,
    rescale: [number, number] = HEAT_RESCALE[index],
): [number, number, number, number] {
    const [lo, hi] = rescale;
    const t = hi === lo ? 0.5 : (value - lo) / (hi - lo);
    const stops = index === "vv" || index === "vh" ? SAR_STOPS : VEG_STOPS;
    const [r, g, b] = sampleStops(stops, t);
    return [Math.round(r), Math.round(g), Math.round(b), 220];
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
                    d * d * d * d * d * d) /
                    720);
    const lon =
        (d -
            ((1 + 2 * t1 + c1) * d * d * d) / 6 +
            ((5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * c1 + 24 * t1 * t1) *
                d * d * d * d * d) /
                120) /
        Math.cos(phi1Rad);

    return [(lon * 180) / Math.PI + longOrigin, (lat * 180) / Math.PI];
}

export interface AgriHeatmapImage {
    dataUrl: string;
    /** MapLibre image coordinates: TL, TR, BR, BL as [lng, lat] */
    coordinates: [[number, number], [number, number], [number, number], [number, number]];
    width: number;
    height: number;
    index: AgriHeatIndex;
    pixelCount: number;
}

/**
 * Rasterize sparse pixel list onto a transparent canvas (origin = upper-left, row↓).
 */
export function rasterizeAgriPixels(
    pixelData: AgriPixelData,
    index: AgriHeatIndex,
    sensor: "S1" | "S2",
    rescale?: [number, number],
): AgriHeatmapImage | null {
    const grid = pixelData?.grid;
    const pixels = pixelData?.pixels;
    if (!grid || !pixels?.length) return null;
    const vi = valueIndexFor(index, sensor);
    if (vi == null) return null;

    const { width, height, origin_x, origin_y, resolution, epsg } = grid;
    if (!width || !height || !resolution || !epsg) return null;

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    const img = ctx.createImageData(width, height);
    const rs = rescale ?? HEAT_RESCALE[index];
    let painted = 0;

    for (const row of pixels) {
        if (!Array.isArray(row) || row.length <= vi) continue;
        const r = Number(row[0]);
        const c = Number(row[1]);
        const v = Number(row[vi]);
        if (!Number.isFinite(r) || !Number.isFinite(c) || !Number.isFinite(v)) continue;
        if (r < 0 || r >= height || c < 0 || c >= width) continue;
        const [rr, gg, bb, aa] = colorizeValue(v, index, rs);
        const i = (r * width + c) * 4;
        img.data[i] = rr;
        img.data[i + 1] = gg;
        img.data[i + 2] = bb;
        img.data[i + 3] = aa;
        painted += 1;
    }
    if (painted === 0) return null;
    ctx.putImageData(img, 0, 0);

    const tl = utmToLonLat(origin_x, origin_y, epsg);
    const tr = utmToLonLat(origin_x + width * resolution, origin_y, epsg);
    const br = utmToLonLat(origin_x + width * resolution, origin_y - height * resolution, epsg);
    const bl = utmToLonLat(origin_x, origin_y - height * resolution, epsg);

    return {
        dataUrl: canvas.toDataURL("image/png"),
        coordinates: [tl, tr, br, bl],
        width,
        height,
        index,
        pixelCount: painted,
    };
}

/**
 * Drought / flood classifiers + official optical product picking.
 * Keep in sync with services/api/app/core/agri_classify.py
 * (thresholds documented there and in docs/agri-drought-flood.md).
 */

export const CLOUD_MAX_PCT = 30;
export const DROUGHT_CLOUD_MAX_PCT = CLOUD_MAX_PCT;
/** Official drought window: June-September (override per crop if needed). */
export const PHENOLOGY_MONTHS = [6, 7, 8, 9] as const;

export const PARCEL_CLOUD_SOURCE_SCL = "scl";
export const PARCEL_CLOUD_SOURCE_LONLAT = "lonlat_clear";
export const PARCEL_CLOUD_SOURCES_TRUSTED = new Set([
    PARCEL_CLOUD_SOURCE_SCL,
    PARCEL_CLOUD_SOURCE_LONLAT,
]);

/** Legacy window-fill parcel cloud: (1 - finite/window)*100 on small padded fields. */
export const LEGACY_PARCEL_CLOUD_MIN = 70;
export const LEGACY_STAC_CLOUD_MAX = 40;
export const LEGACY_PARCEL_STAC_GAP = 40;
export const CLEAR_PIXEL_FRACTION_TRUST = 0.9;
export const SUSPICIOUS_PARCEL_VS_CLEAR = 50;
/** Parcel ~0% while STAC is nearly overcast: nodata counted as clear, or invented 0. */
export const SUSPICIOUS_CLEAR_PARCEL_MAX = 5;
export const SUSPICIOUS_STAC_OVERCAST_MIN = 80;
export const NEARBY_CLEAR_DAYS = 45;
export const BORDERLINE_PARCEL_MIN = 20;
export const BORDERLINE_PARCEL_MAX = 40;
/** NDVI/growth pick vs nearby clear-raw phenology (not used for drought). */
export const PHYSIOLOGY_NDVI_ABSURD_GAP = 0.2;
export const PHYSIOLOGY_GROWING_NDVI_FLOOR = 0.15;
export const PHYSIOLOGY_GREEN_NEIGHBOR_NDVI = 0.4;

export const NDDI_MILD_MIN = 0.3;
export const NDDI_MODERATE_MIN = 0.4;
export const NDDI_SEVERE_MIN = 0.5;
export const NDMI_FALLBACK_SEVERE = -0.2;

export const NDDI_PCTL_DRY = 80;
export const MIN_MONTH_SAMPLES = 3;
export const NDMI_DRY_ABS = 0.1;
export const NDMI_DROP_VS_MEDIAN = 0.05;
export const NDVI_DROP_VS_MEDIAN = 0.08;
export const NDVI_DROP_MODERATE = 0.12;
export const NDVI_DROP_SEVERE = 0.2;

export const FLOOD_VV_MAX = -17;
export const FLOOD_VV_DROP = -3;
export const FLOOD_VH_MAX = -22;
export const WATCH_VV_MAX = -15;
export const WATCH_VV_DROP = -2;
export const WATCH_VH_MAX = -20;
export const FLOOD_VV_SEVERE = -20;
export const MIN_ORBIT_SAMPLES = 3;
export const VV_VH_DIFF_PCTL = 40;
export const FLOOD_SPRING_MONTHS = [3, 4, 5] as const;

export const DECLOUD_SOURCE = "uncrtaints_decloud";
export const DECLOUD_SCENE_ID_SUFFIX = "_decloud";

export type AgriDroughtClass =
    | "severe"
    | "moderate"
    | "mild"
    | "normal"
    | "unreliable"
    | "out_of_season";
export type AgriDroughtPixelClass = "severe" | "moderate" | "mild" | "normal";
export type AgriFloodClass = "flood_severe" | "flood_moderate" | "watch" | "dry";

export type OpticalSceneLike = {
    date?: string | null;
    source?: string | null;
    scene_id?: string | null;
    decloud_quality?: string | null;
    decloud_reasons?: string[] | string | null;
    cloud_cover_over_30?: boolean | null;
    cloud_cover?: number | null;
    parcel_cloud_cover_pct?: number | null;
    parcel_cloud_source?: string | null;
    clear_frac?: number | null;
    ndvi_avg?: number | null;
    ndmi_avg?: number | null;
    sensor?: string | null;
};

export type SarSceneLike = {
    date?: string | null;
    scene_id?: string | null;
    relative_orbit?: number | null;
    vv_avg?: number | null;
    vh_avg?: number | null;
    sensor?: string | null;
};

export type OpticalTooltipFields = {
    cloudCover: number | null;
    decloudQuality: string | null;
    decloudReasons: string[];
    productSource: string | null;
    sceneId: string | null;
    isDecloud: boolean;
    isOfficial: boolean;
    mayBeUnreliable: boolean;
};

const S1_ORBIT_OFFSET: Record<string, number> = { S1A: 73, S1B: 27, S1C: 172 };
const S1_ID_RE =
    /^(S1[ABC])_IW_GRD[HM]?_1S[DS][VH]_\d{8}T\d{6}_\d{8}T\d{6}_(\d{6})/i;

function finiteNum(v: unknown): number | null {
    if (typeof v !== "number" || !Number.isFinite(v)) return null;
    return v;
}

export function computeNddi(ndvi: number, ndmi: number): number | null {
    if (!Number.isFinite(ndvi) || !Number.isFinite(ndmi)) return null;
    const denom = ndvi + ndmi;
    if (Math.abs(denom) < 1e-6) return null;
    return (ndvi - ndmi) / denom;
}

/** Pixel / heatmap NDDI class (not season-gated). */
export function classifyDrought(ndvi: number, ndmi: number): AgriDroughtPixelClass {
    const nddi = computeNddi(ndvi, ndmi);
    if (nddi != null) {
        if (nddi >= NDDI_SEVERE_MIN) return "severe";
        if (nddi >= NDDI_MODERATE_MIN) return "moderate";
        if (nddi >= NDDI_MILD_MIN) return "mild";
        return "normal";
    }
    if (Number.isFinite(ndmi) && ndmi < NDMI_FALLBACK_SEVERE) return "severe";
    return "normal";
}

export function droughtClassFromAvgs(
    ndvi: number | null | undefined,
    ndmi: number | null | undefined,
): AgriDroughtPixelClass | null {
    if (typeof ndvi !== "number" || !Number.isFinite(ndvi)) return null;
    if (typeof ndmi !== "number" || !Number.isFinite(ndmi)) return null;
    return classifyDrought(ndvi, ndmi);
}

export function isDroughtDayClass(cls: AgriDroughtClass | null | undefined): boolean {
    return cls === "mild" || cls === "moderate" || cls === "severe";
}

export function isDecloudProduct(scene: {
    source?: string | null;
    scene_id?: string | null;
}): boolean {
    if (scene.source === DECLOUD_SOURCE) return true;
    return typeof scene.scene_id === "string" && scene.scene_id.endsWith(DECLOUD_SCENE_ID_SUFFIX);
}

export function parcelCloudIsUntrustedClear(scene: {
    parcel_cloud_cover_pct?: number | null;
    cloud_cover?: number | null;
    source?: string | null;
    scene_id?: string | null;
}): boolean {
    const parcel = finiteNum(scene.parcel_cloud_cover_pct);
    const stac = finiteNum(scene.cloud_cover);
    if (parcel == null || parcel > SUSPICIOUS_CLEAR_PARCEL_MAX) return false;
    if (isDecloudProduct(scene) && stac != null) return true;
    if (stac == null) return false;
    return stac >= SUSPICIOUS_STAC_OVERCAST_MIN;
}

export function parcelCloudIsLegacyWindowFill(scene: {
    parcel_cloud_cover_pct?: number | null;
    cloud_cover?: number | null;
    parcel_cloud_source?: string | null;
    clear_frac?: number | null;
}): boolean {
    const src = (scene.parcel_cloud_source ?? "").trim().toLowerCase();
    if (PARCEL_CLOUD_SOURCES_TRUSTED.has(src)) return false;
    const parcel = finiteNum(scene.parcel_cloud_cover_pct);
    const stac = finiteNum(scene.cloud_cover);
    const clearFrac = finiteNum(scene.clear_frac);
    if (
        clearFrac != null &&
        clearFrac >= CLEAR_PIXEL_FRACTION_TRUST &&
        parcel != null &&
        parcel > SUSPICIOUS_PARCEL_VS_CLEAR
    ) {
        return true;
    }
    if (parcel == null || stac == null) return false;
    return (
        parcel >= LEGACY_PARCEL_CLOUD_MIN &&
        stac <= LEGACY_STAC_CLOUD_MAX &&
        parcel - stac >= LEGACY_PARCEL_STAC_GAP
    );
}

export function sceneCloudPct(scene: OpticalSceneLike | null | undefined): number | null {
    if (!scene) return null;
    if (parcelCloudIsUntrustedClear(scene)) {
        return finiteNum(scene.cloud_cover);
    }
    if (parcelCloudIsLegacyWindowFill(scene)) {
        return finiteNum(scene.cloud_cover);
    }
    const parcel = finiteNum(scene.parcel_cloud_cover_pct);
    if (parcel != null) return parcel;
    return finiteNum(scene.cloud_cover);
}

export function isOfficialOpticalScene(scene: {
    source?: string | null;
    scene_id?: string | null;
    decloud_quality?: string | null;
    cloud_cover_over_30?: boolean | null;
    cloud_cover?: number | null;
    parcel_cloud_cover_pct?: number | null;
    parcel_cloud_source?: string | null;
    clear_frac?: number | null;
}): boolean {
    if (isDecloudProduct(scene)) {
        return scene.decloud_quality === "good";
    }
    const pct = sceneCloudPct(scene);
    if (pct != null) return pct <= DROUGHT_CLOUD_MAX_PCT;
    if (scene.cloud_cover_over_30 === true) return false;
    if (scene.cloud_cover_over_30 === false) return true;
    return false;
}

function cloudSortKey(scene: OpticalSceneLike): [number, string] {
    const pct = sceneCloudPct(scene);
    return [pct ?? 999, String(scene.scene_id ?? "")];
}

function sceneIsoDate(scene: OpticalSceneLike): string {
    return String(scene.date ?? "").slice(0, 10);
}

function parseIsoDate(dateStr: string): Date | null {
    if (!dateStr || dateStr.length < 10) return null;
    const d = new Date(`${dateStr.slice(0, 10)}T00:00:00Z`);
    return Number.isNaN(d.getTime()) ? null : d;
}

function daysBetween(a: Date, b: Date): number {
    return Math.abs((a.getTime() - b.getTime()) / 86400000);
}

export function nearbyClearIndexMedians<T extends OpticalSceneLike>(
    neighbors: T[] | undefined,
    targetDate: string,
    windowDays = NEARBY_CLEAR_DAYS,
): { ndvi: number | null; ndmi: number | null } {
    const target = parseIsoDate(targetDate);
    if (!target || !neighbors?.length) return { ndvi: null, ndmi: null };
    const windowed: Array<{ ndvi: number; ndmi: number | null }> = [];
    const sameMonth: Array<{ ndvi: number; ndmi: number | null }> = [];
    for (const s of neighbors) {
        if (isDecloudProduct(s)) continue;
        const ds = sceneIsoDate(s);
        const d = parseIsoDate(ds);
        if (!d || ds === targetDate.slice(0, 10)) continue;
        if (!isOfficialOpticalScene(s)) continue;
        const ndvi = finiteNum(s.ndvi_avg);
        if (ndvi == null) continue;
        const rec = { ndvi, ndmi: finiteNum(s.ndmi_avg) };
        if (daysBetween(d, target) <= windowDays) windowed.push(rec);
        if (d.getUTCMonth() + 1 === target.getUTCMonth() + 1) sameMonth.push(rec);
    }
    const pool = windowed.length ? windowed : sameMonth;
    if (!pool.length) return { ndvi: null, ndmi: null };
    const ndmiVals = pool.map((p) => p.ndmi).filter((v): v is number => v != null);
    return {
        ndvi: median(pool.map((p) => p.ndvi)),
        ndmi: ndmiVals.length ? median(ndmiVals) : null,
    };
}

function needsCloserToTruth(raw: OpticalSceneLike): boolean {
    if (parcelCloudIsLegacyWindowFill(raw)) return false;
    const parcel = finiteNum(raw.parcel_cloud_cover_pct);
    const stac = finiteNum(raw.cloud_cover);
    if (parcel == null) return false;
    if (parcel > BORDERLINE_PARCEL_MIN && parcel <= BORDERLINE_PARCEL_MAX) return true;
    if (parcel > DROUGHT_CLOUD_MAX_PCT && stac != null && stac <= DROUGHT_CLOUD_MAX_PCT) {
        return true;
    }
    return false;
}

function physiologySortKey(
    scene: OpticalSceneLike,
    ndviMed: number | null,
    ndmiMed: number | null,
    targetDate: string,
): [number, number, number, number, string] {
    const ndvi = finiteNum(scene.ndvi_avg);
    const ndmi = finiteNum(scene.ndmi_avg);
    const dNdvi = ndvi != null && ndviMed != null ? Math.abs(ndvi - ndviMed) : 999;
    const dNdmi = ndmi != null && ndmiMed != null ? Math.abs(ndmi - ndmiMed) : 999;
    let absurd = 0;
    if (ndvi != null && ndviMed != null) {
        if (Math.abs(ndvi - ndviMed) >= PHYSIOLOGY_NDVI_ABSURD_GAP) absurd = 1;
        const month = monthFromDate(targetDate);
        if (
            month != null &&
            (PHENOLOGY_MONTHS as readonly number[]).includes(month) &&
            ndviMed >= PHYSIOLOGY_GREEN_NEIGHBOR_NDVI &&
            ndvi < PHYSIOLOGY_GROWING_NDVI_FLOOR
        ) {
            absurd = 1;
        }
    }
    const decloudRank = isDecloudProduct(scene) ? 1 : 0;
    return [absurd, dNdvi, dNdmi, decloudRank, String(scene.scene_id ?? "")];
}

function truthSortKey(
    scene: OpticalSceneLike,
    ndviMed: number | null,
    ndmiMed: number | null,
): [number, number, number, string] {
    const ndvi = finiteNum(scene.ndvi_avg);
    const ndmi = finiteNum(scene.ndmi_avg);
    const dNdvi = ndvi != null && ndviMed != null ? Math.abs(ndvi - ndviMed) : 999;
    const dNdmi = ndmi != null && ndmiMed != null ? Math.abs(ndmi - ndmiMed) : 999;
    const decloudRank = isDecloudProduct(scene) ? 1 : 0;
    return [dNdvi, dNdmi, decloudRank, String(scene.scene_id ?? "")];
}

function rawFitsPhenologyBetter(raw: OpticalSceneLike, decloud: OpticalSceneLike): boolean {
    const target = sceneIsoDate(raw) || sceneIsoDate(decloud);
    const month = monthFromDate(target);
    const rawNdvi = finiteNum(raw.ndvi_avg);
    const decNdvi = finiteNum(decloud.ndvi_avg);
    if (rawNdvi == null || decNdvi == null) return false;
    const inSeason = month != null && (PHENOLOGY_MONTHS as readonly number[]).includes(month);
    if (!inSeason) return false;
    const rawOk = rawNdvi >= PHYSIOLOGY_GROWING_NDVI_FLOOR && rawNdvi <= 0.95;
    return rawOk && decNdvi < PHYSIOLOGY_GROWING_NDVI_FLOOR;
}

export type PickOpticalOpts<T extends OpticalSceneLike> = {
    neighbors?: T[];
};

/** Clear raw, else good decloud. Same-date scenes. See docs/agri-drought-flood.md. */
export function pickOfficialOptical<T extends OpticalSceneLike>(
    scenes: T[],
    opts?: PickOpticalOpts<T>,
): T | null {
    if (!scenes.length) return null;
    const rawScenes = scenes.filter((s) => !isDecloudProduct(s));
    const goodDecloud = scenes.filter(
        (s) => isDecloudProduct(s) && s.decloud_quality === "good",
    );
    const byCloud = (a: T, b: T) => {
        const [pa, ia] = cloudSortKey(a);
        const [pb, ib] = cloudSortKey(b);
        return pa - pb || ia.localeCompare(ib);
    };
    const bestRaw = rawScenes.length ? [...rawScenes].sort(byCloud)[0]! : null;
    const bestDecloud = goodDecloud.length ? [...goodDecloud].sort(byCloud)[0]! : null;
    const rawClear = bestRaw != null && isOfficialOpticalScene(bestRaw);

    if (bestRaw && !bestDecloud) return rawClear ? bestRaw : null;
    if (!bestRaw) return bestDecloud;

    if (needsCloserToTruth(bestRaw) && bestDecloud) {
        const { ndvi, ndmi } = nearbyClearIndexMedians(opts?.neighbors, sceneIsoDate(bestRaw));
        if (ndvi != null) {
            const candidates: T[] = [bestRaw, bestDecloud];
            return candidates.sort((a, b) => {
                const ka = truthSortKey(a, ndvi, ndmi);
                const kb = truthSortKey(b, ndvi, ndmi);
                return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2] || ka[3].localeCompare(kb[3]);
            })[0]!;
        }
    }
    if (rawClear) return bestRaw;
    return bestDecloud;
}

/** Growth series: physiology-aware raw vs good decloud. Never fair/bad. */
export function pickOpticalForNdvi<T extends OpticalSceneLike>(
    scenes: T[],
    opts?: PickOpticalOpts<T>,
): T | null {
    if (!scenes.length) return null;
    const byCloud = (a: T, b: T) => {
        const [pa, ia] = cloudSortKey(a);
        const [pb, ib] = cloudSortKey(b);
        return pa - pb || ia.localeCompare(ib);
    };
    const rawScenes = scenes.filter((s) => !isDecloudProduct(s));
    const goodDecloud = scenes.filter(
        (s) => isDecloudProduct(s) && s.decloud_quality === "good",
    );
    const bestRaw = rawScenes.length ? [...rawScenes].sort(byCloud)[0]! : null;
    const bestDecloud = goodDecloud.length ? [...goodDecloud].sort(byCloud)[0]! : null;
    if (bestRaw && bestDecloud) {
        const target = sceneIsoDate(bestRaw) || sceneIsoDate(bestDecloud);
        const { ndvi, ndmi } = nearbyClearIndexMedians(opts?.neighbors, target);
        if (ndvi != null) {
            const candidates: T[] = [bestRaw, bestDecloud];
            return candidates.sort((a, b) => {
                const ka = physiologySortKey(a, ndvi, ndmi, target);
                const kb = physiologySortKey(b, ndvi, ndmi, target);
                return (
                    ka[0] - kb[0] ||
                    ka[1] - kb[1] ||
                    ka[2] - kb[2] ||
                    ka[3] - kb[3] ||
                    ka[4].localeCompare(kb[4])
                );
            })[0]!;
        }
        if (rawFitsPhenologyBetter(bestRaw, bestDecloud)) return bestRaw;
    }
    const official = pickOfficialOptical(scenes, opts);
    if (official) return official;
    if (!rawScenes.length) return null;
    return [...rawScenes].sort(byCloud)[0]!;
}

export function opticalTooltipFields(scene: OpticalSceneLike | null | undefined): OpticalTooltipFields {
    if (!scene) {
        return {
            cloudCover: null,
            decloudQuality: null,
            decloudReasons: [],
            productSource: null,
            sceneId: null,
            isDecloud: false,
            isOfficial: false,
            mayBeUnreliable: false,
        };
    }
    const rawReasons = scene.decloud_reasons;
    const reasons = Array.isArray(rawReasons)
        ? rawReasons.map(String).filter(Boolean)
        : typeof rawReasons === "string" && rawReasons
          ? [rawReasons]
          : [];
    const isDecloud = isDecloudProduct(scene);
    const quality = scene.decloud_quality ?? null;
    return {
        cloudCover: sceneCloudPct(scene),
        decloudQuality: quality,
        decloudReasons: reasons,
        productSource: scene.source ?? null,
        sceneId: scene.scene_id ?? null,
        isDecloud,
        isOfficial: isOfficialOpticalScene(scene),
        mayBeUnreliable: isDecloud && quality !== "good",
    };
}

export function monthFromDate(dateStr: string | null | undefined): number | null {
    if (!dateStr || dateStr.length < 7) return null;
    const m = Number(String(dateStr).slice(5, 7));
    return Number.isFinite(m) && m >= 1 && m <= 12 ? m : null;
}

export function isDroughtSeason(
    dateStr: string | null | undefined,
    seasonMonths: readonly number[] = PHENOLOGY_MONTHS,
): boolean {
    const m = monthFromDate(dateStr);
    return m != null && seasonMonths.includes(m);
}

function median(values: number[]): number | null {
    if (!values.length) return null;
    const s = [...values].sort((a, b) => a - b);
    const mid = Math.floor(s.length / 2);
    return s.length % 2 ? s[mid]! : (s[mid - 1]! + s[mid]!) / 2;
}

function percentileRank(value: number, values: number[]): number | null {
    if (!values.length) return null;
    const below = values.filter((v) => v < value).length;
    const equal = values.filter((v) => v === value).length;
    return ((below + 0.5 * equal) / values.length) * 100;
}

function percentile(values: number[], p: number): number | null {
    if (!values.length) return null;
    const s = [...values].sort((a, b) => a - b);
    if (s.length === 1) return s[0]!;
    const x = (Math.max(0, Math.min(100, p)) / 100) * (s.length - 1);
    const lo = Math.floor(x);
    const hi = Math.ceil(x);
    if (lo === hi) return s[lo]!;
    const t = x - lo;
    return s[lo]! * (1 - t) + s[hi]! * t;
}

export type MonthDroughtBaseline = {
    ndviMedian: number | null;
    ndmiMedian: number | null;
    nddiValues: number[];
    n: number;
};

export function buildMonthDroughtBaselines(
    scenes: OpticalSceneLike[],
    seasonMonths: readonly number[] = PHENOLOGY_MONTHS,
): Map<number, MonthDroughtBaseline> {
    const buckets = new Map<number, { ndvi: number[]; ndmi: number[]; nddi: number[] }>();
    for (const s of scenes) {
        if (!isOfficialOpticalScene(s)) continue;
        const date = String(s.date ?? "");
        if (!isDroughtSeason(date, seasonMonths)) continue;
        const month = monthFromDate(date);
        if (month == null) continue;
        const ndvi = finiteNum(s.ndvi_avg);
        const ndmi = finiteNum(s.ndmi_avg);
        if (ndvi == null || ndmi == null) continue;
        const b = buckets.get(month) ?? { ndvi: [], ndmi: [], nddi: [] };
        b.ndvi.push(ndvi);
        b.ndmi.push(ndmi);
        const nddi = computeNddi(ndvi, ndmi);
        if (nddi != null) b.nddi.push(nddi);
        buckets.set(month, b);
    }
    const out = new Map<number, MonthDroughtBaseline>();
    for (const [month, b] of buckets) {
        out.set(month, {
            ndviMedian: median(b.ndvi),
            ndmiMedian: median(b.ndmi),
            nddiValues: b.nddi,
            n: b.ndvi.length,
        });
    }
    return out;
}

function droughtSeverity(nddi: number | null, ndviDrop: number | null): AgriDroughtClass {
    if ((nddi != null && nddi >= NDDI_SEVERE_MIN) || (ndviDrop != null && ndviDrop >= NDVI_DROP_SEVERE)) {
        return "severe";
    }
    if (
        (nddi != null && nddi >= NDDI_MODERATE_MIN) ||
        (ndviDrop != null && ndviDrop >= NDVI_DROP_MODERATE)
    ) {
        return "moderate";
    }
    return "mild";
}

export function classifyDroughtScene(
    scene: OpticalSceneLike,
    monthStats: MonthDroughtBaseline | null | undefined,
    seasonMonths: readonly number[] = PHENOLOGY_MONTHS,
): AgriDroughtClass {
    const date = String(scene.date ?? "");
    if (!isDroughtSeason(date, seasonMonths)) return "out_of_season";
    if (!isOfficialOpticalScene(scene)) return "unreliable";
    const ndvi = finiteNum(scene.ndvi_avg);
    const ndmi = finiteNum(scene.ndmi_avg);
    if (ndvi == null || ndmi == null) return "unreliable";
    const nddi = computeNddi(ndvi, ndmi);
    const n = monthStats?.n ?? 0;
    const ndviMed = monthStats?.ndviMedian ?? null;
    const ndmiMed = monthStats?.ndmiMedian ?? null;
    const nddiVals = monthStats?.nddiValues ?? [];

    const nddiAbs = nddi != null && nddi >= NDDI_MILD_MIN;
    let nddiAnom = false;
    if (n >= MIN_MONTH_SAMPLES && nddi != null && nddiVals.length) {
        const rank = percentileRank(nddi, nddiVals);
        nddiAnom = rank != null && rank >= NDDI_PCTL_DRY;
    }

    let ndmiDry = ndmi < NDMI_DRY_ABS;
    if (ndmiMed != null) ndmiDry = ndmiDry || ndmi <= ndmiMed - NDMI_DROP_VS_MEDIAN;

    let ndviDrop: number | null = null;
    let ndviDropped = false;
    if (ndviMed != null) {
        ndviDrop = ndviMed - ndvi;
        ndviDropped = ndvi <= ndviMed - NDVI_DROP_VS_MEDIAN;
    }

    if ((nddiAbs || nddiAnom) && (ndmiDry || ndviDropped)) {
        return droughtSeverity(nddi, ndviDrop);
    }
    return "normal";
}

export function classifyDroughtSeries(
    scenes: OpticalSceneLike[],
    seasonMonths: readonly number[] = PHENOLOGY_MONTHS,
): Map<string, AgriDroughtClass> {
    const groups = new Map<string, OpticalSceneLike[]>();
    for (const s of scenes) {
        if (s.sensor && s.sensor !== "S2") continue;
        const date = String(s.date ?? "");
        if (!date) continue;
        const arr = groups.get(date) ?? [];
        arr.push(s);
        groups.set(date, arr);
    }
    const allS2 = [...groups.values()].flat();
    const list: OpticalSceneLike[] = [];
    for (const group of groups.values()) {
        const picked =
            pickOfficialOptical(group, { neighbors: allS2 }) ??
            pickOpticalForNdvi(group, { neighbors: allS2 });
        if (picked) list.push(picked);
        else if (group[0]) list.push(group[0]);
    }
    const baselines = buildMonthDroughtBaselines(list, seasonMonths);
    const out = new Map<string, AgriDroughtClass>();
    for (const s of list) {
        const date = String(s.date ?? "");
        const month = monthFromDate(date);
        const stats = month != null ? (baselines.get(month) ?? null) : null;
        out.set(date, classifyDroughtScene(s, stats, seasonMonths));
    }
    return out;
}

/**
 * Pixel heatmap: spatial open-water-like backscatter. Not a date-level flood
 * event (that needs the per-orbit drop in classifyFloodScene).
 * VV-VH difference alone never flags.
 */
export function classifyFloodPixel(vvDb: number, vhDb: number | null): AgriFloodClass {
    if (!Number.isFinite(vvDb)) return "dry";
    const vh = vhDb != null && Number.isFinite(vhDb) ? vhDb : null;
    const helper = vh != null && vh <= FLOOD_VH_MAX;
    if (vvDb <= FLOOD_VV_MAX && helper) {
        return vvDb <= FLOOD_VV_SEVERE ? "flood_severe" : "flood_moderate";
    }
    const watchVh = vh != null && vh <= WATCH_VH_MAX;
    if (vvDb <= WATCH_VV_MAX && (helper || watchVh || vvDb <= FLOOD_VV_MAX)) {
        return "watch";
    }
    return "dry";
}

export function parseS1RelativeOrbit(
    sceneId: string | null | undefined,
    relativeOrbit?: number | null,
): number | null {
    if (typeof relativeOrbit === "number" && relativeOrbit >= 1 && relativeOrbit <= 175) {
        return Math.trunc(relativeOrbit);
    }
    if (!sceneId) return null;
    const m = S1_ID_RE.exec(sceneId.trim());
    if (!m) return null;
    const mission = m[1]!.toUpperCase();
    const absOrbit = Number(m[2]);
    if (!Number.isFinite(absOrbit)) return null;
    const offset = S1_ORBIT_OFFSET[mission] ?? 73;
    return ((((absOrbit - offset) % 175) + 175) % 175) + 1;
}

export function orbitGroupKey(scene: SarSceneLike): string {
    const rel = parseS1RelativeOrbit(scene.scene_id, scene.relative_orbit);
    return rel == null ? "unknown" : `ron${rel}`;
}

export function classifyFloodScene(
    vv: number | null,
    vh: number | null,
    baselineVv: number | null,
    orbitDiffP40: number | null,
): AgriFloodClass | null {
    if (vv == null || !Number.isFinite(vv)) return null;
    const vhF = vh != null && Number.isFinite(vh) ? vh : null;
    const drop = baselineVv != null && Number.isFinite(baselineVv) ? vv - baselineVv : null;
    const vvVh = vhF != null ? vv - vhF : null;

    let helper = false;
    if (vhF != null && vhF <= FLOOD_VH_MAX) helper = true;
    if (vvVh != null && orbitDiffP40 != null && Number.isFinite(orbitDiffP40) && vvVh <= orbitDiffP40) {
        helper = true;
    }

    const lowVv = vv <= FLOOD_VV_MAX;
    const dropped = drop != null && drop <= FLOOD_VV_DROP;
    if (lowVv && dropped && helper) {
        return vv <= FLOOD_VV_SEVERE ? "flood_severe" : "flood_moderate";
    }

    const watchVv = vv <= WATCH_VV_MAX;
    const watchDrop = drop != null && drop <= WATCH_VV_DROP;
    const watchVh = vhF != null && vhF <= WATCH_VH_MAX;
    if (watchVv && (watchDrop || watchVh || helper || lowVv)) return "watch";
    return "dry";
}

export function classifyFloodSeries(scenes: SarSceneLike[]): Map<string, AgriFloodClass> {
    const valid = scenes.filter((s) => finiteNum(s.vv_avg) != null);
    const groups = new Map<string, SarSceneLike[]>();
    for (const s of valid) {
        const key = orbitGroupKey(s);
        const arr = groups.get(key) ?? [];
        arr.push(s);
        groups.set(key, arr);
    }

    const allVv = valid.map((s) => s.vv_avg!).filter((v) => Number.isFinite(v));
    const allDiff: number[] = [];
    for (const s of valid) {
        const vv = finiteNum(s.vv_avg);
        const vh = finiteNum(s.vh_avg);
        if (vv != null && vh != null) allDiff.push(vv - vh);
    }

    const baselines = new Map<string, { vv: number | null; p40: number | null }>();
    for (const [key, rows] of groups) {
        const vvs = rows.map((r) => r.vv_avg!).filter((v) => Number.isFinite(v));
        const diffs: number[] = [];
        for (const r of rows) {
            const vv = finiteNum(r.vv_avg);
            const vh = finiteNum(r.vh_avg);
            if (vv != null && vh != null) diffs.push(vv - vh);
        }
        if (vvs.length >= MIN_ORBIT_SAMPLES) {
            baselines.set(key, { vv: median(vvs), p40: percentile(diffs, VV_VH_DIFF_PCTL) });
        } else {
            baselines.set(key, {
                vv: allVv.length >= MIN_ORBIT_SAMPLES ? median(allVv) : median(vvs),
                p40:
                    allDiff.length >= MIN_ORBIT_SAMPLES
                        ? percentile(allDiff, VV_VH_DIFF_PCTL)
                        : percentile(diffs, VV_VH_DIFF_PCTL),
            });
        }
    }

    const out = new Map<string, AgriFloodClass>();
    for (const s of valid) {
        const date = String(s.date ?? "");
        if (!date) continue;
        const base = baselines.get(orbitGroupKey(s));
        const cls = classifyFloodScene(
            finiteNum(s.vv_avg),
            finiteNum(s.vh_avg),
            base?.vv ?? null,
            base?.p40 ?? null,
        );
        if (cls) {
            const prev = out.get(date);
            if (!prev || floodRank(cls) > floodRank(prev)) out.set(date, cls);
        }
    }
    return out;
}

function floodRank(cls: AgriFloodClass): number {
    if (cls === "flood_severe") return 3;
    if (cls === "flood_moderate") return 2;
    if (cls === "watch") return 1;
    return 0;
}

export function isFloodDayClass(cls: AgriFloodClass | null | undefined): boolean {
    return cls === "flood_severe" || cls === "flood_moderate";
}

export function isFloodWatchClass(cls: AgriFloodClass | null | undefined): boolean {
    return cls === "watch";
}

export function isSpringFloodMonth(dateStr: string | null | undefined): boolean {
    const m = monthFromDate(dateStr);
    return m != null && (FLOOD_SPRING_MONTHS as readonly number[]).includes(m);
}

import type { FieldStat } from "@/lib/api";

export type NdviRealisticFilterOpts = {
    seasonMonths?: number[];
    peakMonths?: number[];
};

/** Rules 1–2: flagged unreliable or parcel/STAC cloud cover above agri skip threshold. */
export function isBasicUnreliableNdviPoint(
    point: Pick<FieldStat, "may_be_unreliable" | "cloud_cover">,
): boolean {
    if (point.may_be_unreliable === true) return true;
    const cc = point.cloud_cover;
    return cc != null && Number.isFinite(cc) && cc > 30;
}

function monthOf(date: string): number {
    return Number(String(date).slice(5, 7));
}

function median(values: number[]): number | null {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    if (sorted.length % 2 === 1) return sorted[mid]!;
    return (sorted[mid - 1]! + sorted[mid]!) / 2;
}

/** Up to 2 nearest reliable neighbors on each side (skip basic-unreliable points). */
function neighborMeans(stats: FieldStat[], index: number): number[] {
    const out: number[] = [];
    let taken = 0;
    for (let i = index - 1; i >= 0 && taken < 2; i--) {
        const s = stats[i]!;
        if (isBasicUnreliableNdviPoint(s)) continue;
        const m = s.mean;
        if (typeof m === "number" && Number.isFinite(m)) {
            out.push(m);
            taken++;
        }
    }
    taken = 0;
    for (let i = index + 1; i < stats.length && taken < 2; i++) {
        const s = stats[i]!;
        if (isBasicUnreliableNdviPoint(s)) continue;
        const m = s.mean;
        if (typeof m === "number" && Number.isFinite(m)) {
            out.push(m);
            taken++;
        }
    }
    return out;
}

function inSeasonOrPeakWindow(
    month: number,
    seasonMonths?: number[],
    peakMonths?: number[],
): boolean {
    const inPeak = !!peakMonths?.length && peakMonths.includes(month);
    const inSeason = !!seasonMonths?.length && seasonMonths.includes(month);
    return inPeak || inSeason;
}

/**
 * True when a FieldStat should be hidden by the "only valid observations" filter.
 * Cloud-hole spike (rule 3) only runs when seasonMonths and/or peakMonths are provided.
 */
export function isUnrealisticNdviPoint(
    point: FieldStat,
    allStats: FieldStat[],
    index: number,
    opts: NdviRealisticFilterOpts = {},
): boolean {
    if (isBasicUnreliableNdviPoint(point)) return true;

    const { seasonMonths, peakMonths } = opts;
    const hasSeasonCtx = !!seasonMonths?.length || !!peakMonths?.length;
    if (!hasSeasonCtx) return false;

    const mean = point.mean;
    if (!(typeof mean === "number" && Number.isFinite(mean)) || mean >= 0.08) {
        return false;
    }

    const month = monthOf(point.date);
    if (!inSeasonOrPeakWindow(month, seasonMonths, peakMonths)) {
        return false;
    }

    const med = median(neighborMeans(allStats, index));
    return med != null && med >= 0.35;
}

/** Drop unrealistic points; preserves order of `stats`. */
export function filterRealisticNdviStats(
    stats: FieldStat[],
    opts: NdviRealisticFilterOpts = {},
): FieldStat[] {
    return stats.filter((point, index) => !isUnrealisticNdviPoint(point, stats, index, opts));
}

/** Quick self-test for NDVI realistic-point filter (mirrors src/lib/ndvi-realistic-filter.ts). */
import assert from "node:assert/strict";

function isBasicUnreliableNdviPoint(point) {
  if (point.may_be_unreliable === true) return true;
  const cc = point.cloud_cover;
  return cc != null && Number.isFinite(cc) && cc > 30;
}

function monthOf(date) {
  return Number(String(date).slice(5, 7));
}

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[mid];
  return (sorted[mid - 1] + sorted[mid]) / 2;
}

function neighborMeans(stats, index) {
  const out = [];
  let taken = 0;
  for (let i = index - 1; i >= 0 && taken < 2; i--) {
    const s = stats[i];
    if (isBasicUnreliableNdviPoint(s)) continue;
    const m = s.mean;
    if (typeof m === "number" && Number.isFinite(m)) {
      out.push(m);
      taken++;
    }
  }
  taken = 0;
  for (let i = index + 1; i < stats.length && taken < 2; i++) {
    const s = stats[i];
    if (isBasicUnreliableNdviPoint(s)) continue;
    const m = s.mean;
    if (typeof m === "number" && Number.isFinite(m)) {
      out.push(m);
      taken++;
    }
  }
  return out;
}

function inSeasonOrPeakWindow(month, seasonMonths, peakMonths) {
  const inPeak = !!peakMonths?.length && peakMonths.includes(month);
  const inSeason = !!seasonMonths?.length && seasonMonths.includes(month);
  return inPeak || inSeason;
}

function isUnrealisticNdviPoint(point, allStats, index, opts = {}) {
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

function filterRealisticNdviStats(stats, opts = {}) {
  return stats.filter((point, index) => !isUnrealisticNdviPoint(point, stats, index, opts));
}

function pt(date, mean, extra = {}) {
  return { date, mean, ...extra };
}

// Rule 1: may_be_unreliable
assert.equal(
  isUnrealisticNdviPoint(pt("2025-07-01", 0.6, { may_be_unreliable: true }), [], 0, {}),
  true,
);

// Rule 2: cloud > 30
assert.equal(
  isUnrealisticNdviPoint(pt("2025-07-01", 0.6, { cloud_cover: 31 }), [], 0, {}),
  true,
);
assert.equal(
  isUnrealisticNdviPoint(pt("2025-07-01", 0.6, { cloud_cover: 30 }), [], 0, {}),
  false,
);

// Without season/peak context: only rules 1–2 (SAR-like)
assert.equal(
  isUnrealisticNdviPoint(pt("2025-07-15", 0.01), [pt("2025-07-15", 0.01)], 0, {}),
  false,
);

const season = { seasonMonths: [4, 5, 6, 7, 8, 9], peakMonths: [6, 7, 8] };

// Cloud-hole spike in peak: low mean + green neighbors
const series = [
  pt("2025-06-20", 0.55),
  pt("2025-07-01", 0.62),
  pt("2025-07-15", 0.02), // hole
  pt("2025-07-28", 0.58),
  pt("2025-08-10", 0.5),
];
assert.equal(isUnrealisticNdviPoint(series[2], series, 2, season), true);
assert.deepEqual(
  filterRealisticNdviStats(series, season).map((s) => s.date),
  ["2025-06-20", "2025-07-01", "2025-07-28", "2025-08-10"],
);

// Normal winter low outside season/peak must NOT hide
const winter = [
  pt("2025-01-10", 0.55),
  pt("2025-01-20", 0.02),
  pt("2025-02-01", 0.5),
];
assert.equal(isUnrealisticNdviPoint(winter[1], winter, 1, season), false);

// Low in season but neighbors also low → not a cloud-hole spike
const lowCanopy = [
  pt("2025-07-01", 0.12),
  pt("2025-07-10", 0.05),
  pt("2025-07-20", 0.1),
];
assert.equal(isUnrealisticNdviPoint(lowCanopy[1], lowCanopy, 1, season), false);

// Neighbors that are cloudy are skipped when computing median
const withCloudyNeighbor = [
  pt("2025-07-01", 0.55),
  pt("2025-07-05", 0.6, { cloud_cover: 80 }),
  pt("2025-07-15", 0.01),
  pt("2025-07-20", 0.58),
  pt("2025-07-25", 0.52),
];
assert.equal(isUnrealisticNdviPoint(withCloudyNeighbor[2], withCloudyNeighbor, 2, season), true);

console.log("verify-ndvi-realistic-filter: ok");

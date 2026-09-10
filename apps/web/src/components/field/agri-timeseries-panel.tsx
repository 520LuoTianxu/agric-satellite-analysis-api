"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import {
    agriApi,
    cropsApi,
    fieldsApi,
    parseAgriLandId,
    type AgriLandScenesSummary,
    type AgriSceneProduct,
    type BackfillStatusResponse,
    type CropOption,
    type FieldStat,
    type IndexType,
} from "@/lib/api";
import { useTranslations } from "next-intl";
import {
    rasterizeAgriPixels,
    rasterizeAgriLonLatPixels,
    sensorForIndex,
    AGRI_MODE_LABELS,
    AGRI_PRIMARY_MODES,
    DROUGHT_CLASS_STYLE,
    DROUGHT_CLOUD_MAX_PCT,
    droughtClassFromAvgs,
    isDroughtDayClass,
    type AgriDroughtClass,
    type AgriHeatIndex,
    type AgriHeatmapImage,
} from "@/lib/agri-heatmap";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { Loader2, Satellite, Eye, EyeOff, RefreshCw, History, MoreHorizontal, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { AgriIndexGlossary } from "@/components/field/agri-index-glossary";
import { toast } from "sonner";
import { haToMu } from "@/lib/area";
import type { DayGradeShare } from "@/components/charts/ndvi-grade-shares-chart";
import {
    computePixelNdviGradeShares,
    NDVI_DAY_GRADE_RULE_ZH,
} from "@/components/charts/ndvi-grade-shares-chart";

const NdviChart = dynamic(() => import("@/components/charts/ndvi-chart"), {
    ssr: false,
    loading: () => <Skeleton className="h-[200px] w-full rounded-md" />,
});

const NdviGradeSharesChart = dynamic(
    () => import("@/components/charts/ndvi-grade-shares-chart"),
    {
        ssr: false,
        loading: () => <Skeleton className="h-[220px] w-full rounded-md" />,
    },
);

type SeriesKey = AgriHeatIndex;

const SERIES_META: Record<
    SeriesKey,
    {
        label: string;
        sensor: "S1" | "S2";
        avgKey: keyof AgriSceneProduct | null;
        hint: string;
        /** Chart / date list derived from this sensor avg column */
        chartKey: keyof AgriSceneProduct | null;
    }
> = {
    ndvi: {
        label: "NDVI（光学）",
        sensor: "S2",
        avgKey: "ndvi_avg",
        chartKey: "ndvi_avg",
        hint: "植被长势 · 色斑图",
    },
    evi: {
        label: "EVI（光学）",
        sensor: "S2",
        avgKey: "evi_avg",
        chartKey: "evi_avg",
        hint: "增强植被指数",
    },
    ndmi: {
        label: "NDMI",
        sensor: "S2",
        avgKey: "ndmi_avg",
        chartKey: "ndmi_avg",
        hint: "水分指数",
    },
    ndre: {
        label: "NDRE",
        sensor: "S2",
        avgKey: "ndre_avg",
        chartKey: "ndre_avg",
        hint: "红边指数",
    },
    mndwi: {
        label: "MNDWI",
        sensor: "S2",
        avgKey: "mndwi_avg",
        chartKey: "mndwi_avg",
        hint: "水体指数",
    },
    cire: {
        label: "CIRE",
        sensor: "S2",
        avgKey: "cire_avg",
        chartKey: "cire_avg",
        hint: "叶绿素红边",
    },
    vv: {
        label: "VV（雷达）",
        sensor: "S1",
        avgKey: "vv_avg",
        chartKey: "vv_avg",
        hint: "Sentinel-1 同极化 · 色斑图",
    },
    vh: {
        label: "VH（雷达）",
        sensor: "S1",
        avgKey: "vh_avg",
        chartKey: "vh_avg",
        hint: "Sentinel-1 交叉极化 · 色斑图",
    },
    drought: {
        label: "干旱",
        sensor: "S2",
        avgKey: null,
        chartKey: "ndvi_avg",
        hint: "NDDI=(NDVI−NDMI)/(NDVI+NDMI) · Gu et al. 2007；云量>30% 的日期不参与干旱",
    },
    flood: {
        label: "洪涝",
        sensor: "S1",
        avgKey: null,
        chartKey: "vv_avg",
        hint: "S1 VV/VH 后向散射阈值 · 积水≈VV≲−18 dB（Martinis 类阈值法）",
    },
};

/** Preferred button order: primary modes first, then other indices. */
const BUTTON_ORDER: SeriesKey[] = [
    ...AGRI_PRIMARY_MODES,
    "ndmi",
    "ndre",
    "mndwi",
    "cire",
    "vv",
    "vh",
];

function scenesToStats(scenes: AgriSceneProduct[], key: SeriesKey): FieldStat[] {
    const meta = SERIES_META[key];
    const avgKey = meta.chartKey;
    if (!avgKey) return [];
    const byDate = new Map<string, number[]>();
    for (const s of scenes) {
        if (s.sensor !== meta.sensor) continue;
        if (key === "drought" && !isLowCloud(s)) continue;
        const v = s[avgKey];
        if (typeof v !== "number" || Number.isNaN(v)) continue;
        const arr = byDate.get(s.date) ?? [];
        arr.push(v);
        byDate.set(s.date, arr);
    }
    return [...byDate.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([date, vals], i) => {
            const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
            const min = Math.min(...vals);
            const max = Math.max(...vals);
            return {
                id: `agri-${key}-${date}-${i}`,
                field_id: "",
                date,
                mean,
                median: mean,
                min,
                max,
                p10: min,
                p90: max,
                stddev: null,
                quality_score: null,
                created_at: "",
            } satisfies FieldStat;
        });
}


/** Avg field used to score a scene for default-date picking. */
function sceneSeriesAvg(scene: AgriSceneProduct, key: SeriesKey): number | null {
    const avgKey = SERIES_META[key].avgKey ?? SERIES_META[key].chartKey;
    if (!avgKey) return null;
    const v = scene[avgKey];
    return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function isLowCloud(scene: AgriSceneProduct): boolean {
    if (scene.cloud_cover_over_30 === true) return false;
    if (scene.cloud_cover_over_30 === false) return true;
    if (typeof scene.cloud_cover === "number" && Number.isFinite(scene.cloud_cover)) {
        return scene.cloud_cover <= DROUGHT_CLOUD_MAX_PCT;
    }
    if (
        typeof scene.parcel_cloud_cover_pct === "number" &&
        Number.isFinite(scene.parcel_cloud_cover_pct)
    ) {
        return scene.parcel_cloud_cover_pct <= DROUGHT_CLOUD_MAX_PCT;
    }
    return false;
}

function sceneCloudPct(scene: AgriSceneProduct | null | undefined): number | null {
    if (!scene) return null;
    const parcel = scene.parcel_cloud_cover_pct;
    if (typeof parcel === "number" && Number.isFinite(parcel)) return parcel;
    const cc = scene.cloud_cover;
    if (typeof cc === "number" && Number.isFinite(cc)) return cc;
    return null;
}

const RECENT_DATE_WINDOW_MS = 60 * 24 * 60 * 60 * 1000;
const RECENT_DATE_CHIP_CAP = 14;
const PRIMARY_SERIES_KEYS: SeriesKey[] = ["ndvi", "evi", "drought", "flood"];

function seriesIsAvailable(key: SeriesKey, scenes: AgriSceneProduct[]): boolean {
    const meta = SERIES_META[key];
    const hasSensor = scenes.some((s) => s.sensor === meta.sensor);
    if (!hasSensor) return false;
    if (key === "drought" || key === "flood") return true;
    if (
        meta.avgKey &&
        !scenes.some(
            (s) => s.sensor === meta.sensor && typeof s[meta.avgKey!] === "number",
        )
    ) {
        return false;
    }
    return true;
}

/** Optical veg indices where avg > 0.1 is a useful clear-sky signal. */
const VEG_AVG_KEYS = new Set<SeriesKey>(["ndvi", "evi", "ndmi", "ndre", "cire", "drought"]);

/**
 * Prefer latest scene with usable vegetation / low cloud — not raw latest
 * (which is often fully cloudy → solid red NDVI film).
 */
function pickBestDefaultDate(scenes: AgriSceneProduct[], key: SeriesKey): string | null {
    const sensor = sensorForIndex(key);
    const list = scenes.filter((s) => s.sensor === sensor);
    if (!list.length) return null;
    const sorted = [...list].sort((a, b) => a.date.localeCompare(b.date));

    // S1 / flood: cloud/NDVI heuristics do not apply — raw latest.
    if (sensor === "S1") {
        return sorted[sorted.length - 1]!.date;
    }

    // 1) Latest with low cloud AND (for veg modes) avg > 0.1
    for (let i = sorted.length - 1; i >= 0; i--) {
        const s = sorted[i]!;
        if (!isLowCloud(s)) continue;
        const avg = sceneSeriesAvg(s, key);
        if (avg == null) continue;
        if (VEG_AVG_KEYS.has(key) && !(avg > 0.1)) continue;
        return s.date;
    }

    // 2) Fallback: date with max series avg (prefer strongest veg signal)
    const scoreKey = SERIES_META[key].chartKey ?? ("ndvi_avg" as const);
    let bestDate: string | null = null;
    let bestAvg = -Infinity;
    for (const s of sorted) {
        const v = s[scoreKey];
        if (typeof v === "number" && Number.isFinite(v) && v > bestAvg) {
            bestAvg = v;
            bestDate = s.date;
        }
    }
    if (bestDate) return bestDate;

    // 3) Last resort: raw latest
    return sorted[sorted.length - 1]!.date;
}

function sceneLooksCloudyOrLowVeg(scene: AgriSceneProduct | undefined, key: SeriesKey): boolean {
    if (!scene || sensorForIndex(key) !== "S2") return false;
    const cloudy =
        scene.cloud_cover_over_30 === true ||
        (typeof scene.cloud_cover === "number" && scene.cloud_cover > DROUGHT_CLOUD_MAX_PCT) ||
        (typeof scene.parcel_cloud_cover_pct === "number" &&
            scene.parcel_cloud_cover_pct > DROUGHT_CLOUD_MAX_PCT);
    const avg = sceneSeriesAvg(scene, key);
    const lowVeg = avg != null && avg <= 0.1;
    return cloudy || lowVeg;
}

const UNCROPPED_NDVI = 0.25;

/** Default maize-like stage bands (month ranges) — overridden by crop season when available */

export interface AgriTimeseriesPanelProps {
    fieldId: string;
    fieldTags: string[] | null | undefined;
    /** Bound crop key / label for season calendar */
    cropType?: string | null;
    /** Field area in hectares — donut center shows 亩 (×15) */
    areaHa?: number | null;
    /** When true, parent already has monitoring layers */
    hasMonitoringData?: boolean;
    /** Push 色斑图 overlay to the field map */
    onHeatmapChange?: (heatmap: AgriHeatmapImage | null) => void;
    /** Controlled heat mode (from map-bottom chips) */
    mode?: AgriHeatIndex;
    onModeChange?: (mode: AgriHeatIndex) => void;
    /** Parent 指数 tab active — clear overlay when false, reload when true */
    enabled?: boolean;
}

function defaultRsDateFrom(): string {
    const d = new Date();
    d.setMonth(d.getMonth() - 24);
    return d.toISOString().slice(0, 10);
}

export default function AgriTimeseriesPanel({
    fieldId,
    fieldTags,
    cropType,
    areaHa = null,
    hasMonitoringData = false,
    onHeatmapChange,
    mode: modeProp,
    onModeChange,
    enabled = true,
}: AgriTimeseriesPanelProps) {
    const t = useTranslations("agriPanel");
    const landId = useMemo(() => parseAgriLandId(fieldTags), [fieldTags]);
    const [backfilling, setBackfilling] = useState(false);
    const [backfillActive, setBackfillActive] = useState(false);
    const [refreshDateOpen, setRefreshDateOpen] = useState(false);
    const [refreshDateFrom, setRefreshDateFrom] = useState(defaultRsDateFrom);
    const [backfillProgress, setBackfillProgress] = useState<BackfillStatusResponse | null>(null);
    const [reloadKey, setReloadKey] = useState(0);
    const backfillPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const [summary, setSummary] = useState<AgriLandScenesSummary | null>(null);
    const [scenes, setScenes] = useState<AgriSceneProduct[]>([]);
    const [cropOption, setCropOption] = useState<CropOption | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [seriesInternal, setSeriesInternal] = useState<SeriesKey>("ndvi");
    const series = modeProp ?? seriesInternal;
    const setSeries = useCallback(
        (next: SeriesKey) => {
            setSeriesInternal(next);
            onModeChange?.(next);
        },
        [onModeChange],
    );
    const [selectedDate, setSelectedDate] = useState<string | null>(null);
    const [heatmapVisible, setHeatmapVisible] = useState(true);
    const [heatmapLoading, setHeatmapLoading] = useState(false);
    const [heatmapMeta, setHeatmapMeta] = useState<{
        pixels: number;
        date: string;
        index: string;
        mean: number | null;
    } | null>(null);
    /** Per-date pixel NDVI grade shares (图一 bands) — fills as heatmaps/prefetch load. */
    const [dayGradeByDate, setDayGradeByDate] = useState<Record<string, DayGradeShare>>({});
    const dayGradeByDateRef = useRef(dayGradeByDate);
    dayGradeByDateRef.current = dayGradeByDate;
    const gradePrefetchDoneRef = useRef<string | null>(null);
    /** Bumps on every loadHeatmap call; stale async results are ignored. */
    const heatmapLoadGenRef = useRef(0);
    /** Prefetched film — kept even when enabled=false (map cleared, cache retained). */
    const cachedHeatmapRef = useRef<{
        date: string;
        index: SeriesKey;
        img: AgriHeatmapImage | null;
    } | null>(null);
    const loadHeatmapRef = useRef<(date: string, index: SeriesKey) => Promise<void>>(async () => {});
    const enabledRef = useRef(enabled);
    enabledRef.current = enabled;
    const heatmapVisibleRef = useRef(heatmapVisible);
    heatmapVisibleRef.current = heatmapVisible;

    useEffect(() => {
        if (modeProp && modeProp !== seriesInternal) {
            setSeriesInternal(modeProp);
        }
    }, [modeProp, seriesInternal]);

    useEffect(() => {
        let cancelled = false;
        cropsApi
            .list()
            .then((list) => {
                if (cancelled) return;
                const key = (cropType || "").toLowerCase();
                const hit =
                    list.find((c) => c.key === key) ||
                    list.find((c) => c.name_zh === cropType) ||
                    list.find((c) => c.key === "corn") ||
                    list[0] ||
                    null;
                setCropOption(hit);
            })
            .catch(() => {
                if (!cancelled) setCropOption(null);
            });
        return () => {
            cancelled = true;
        };
    }, [cropType]);

    useEffect(() => {
        if (!landId) return;
        let cancelled = false;
        setLoading(true);
        setError(null);
        setDayGradeByDate({});
        gradePrefetchDoneRef.current = null;
        (async () => {
            try {
                const [sum, s2, s1] = await Promise.all([
                    agriApi.scenesSummary(landId),
                    agriApi.scenes(landId, { sensor: "S2", limit: 500 }),
                    agriApi.scenes(landId, { sensor: "S1", limit: 500 }),
                ]);
                if (cancelled) return;
                setSummary(sum);
                const all = [...s2.items, ...s1.items];
                setScenes(all);
                const hasS2 = sum.sensors.some((s) => s.sensor === "S2" && s.count > 0);
                const nextSeries: SeriesKey = modeProp ?? (hasS2 ? "ndvi" : "vv");
                setSeriesInternal(nextSeries);
                onModeChange?.(nextSeries);
                const bestDate = pickBestDefaultDate(all, nextSeries);
                if (bestDate) setSelectedDate(bestDate);
                // Prefetch include_pixels=1 as soon as land scenes load (even if 指数 tab
                // inactive). Map overlay is only published when enabled===true.
                if (!cancelled && bestDate) {
                    await loadHeatmapRef.current(bestDate, nextSeries);
                }
            } catch (e: any) {
                if (!cancelled) setError(e?.detail || e?.message || t("loadFailed"));
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
            // Do NOT clear heatmap here — React Strict Mode remount races with loadHeatmap
            // and can wipe a just-loaded overlay. Clear only when enabled flips false or unmount via land change handled by next effect.
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [landId, reloadKey]);

    const stopBackfillPoll = useCallback(() => {
        if (backfillPollRef.current) {
            clearInterval(backfillPollRef.current);
            backfillPollRef.current = null;
        }
    }, []);

    const applyBackfillStatus = useCallback(
        (res: BackfillStatusResponse, opts?: { wasActive?: boolean }) => {
            setBackfillProgress(res);
            setBackfillActive(res.has_active_backfill);
            if (res.has_active_backfill) return true;
            if (opts?.wasActive) {
                setReloadKey((k) => k + 1);
                toast.success(t("refreshComplete"));
            }
            return false;
        },
        [t],
    );

    const startBackfillPoll = useCallback(() => {
        if (!fieldId) return;
        stopBackfillPoll();
        let wasActive = true;
        const tick = async () => {
            try {
                const res = await fieldsApi.backfillStatus(fieldId);
                const stillActive = applyBackfillStatus(res, { wasActive });
                if (stillActive) {
                    wasActive = true;
                } else {
                    wasActive = false;
                    stopBackfillPoll();
                }
            } catch {
                /* ignore transient poll errors */
            }
        };
        void tick();
        backfillPollRef.current = setInterval(tick, 5000);
    }, [fieldId, applyBackfillStatus, stopBackfillPoll]);

    // One-shot on mount: resume polling only if a current-wave job is truly active
    useEffect(() => {
        if (!fieldId) return;
        let cancelled = false;
        (async () => {
            try {
                const res = await fieldsApi.backfillStatus(fieldId);
                if (cancelled) return;
                const active = applyBackfillStatus(res);
                if (active) startBackfillPoll();
            } catch {
                /* ignore */
            }
        })();
        return () => {
            cancelled = true;
            stopBackfillPoll();
        };
    }, [fieldId]); // eslint-disable-line react-hooks/exhaustive-deps

    const openRefreshRsDialog = () => {
        setRefreshDateFrom(defaultRsDateFrom());
        setRefreshDateOpen(true);
    };

    const handleRefreshRs = async () => {
        setRefreshDateOpen(false);
        setBackfilling(true);
        try {
            const today = new Date().toISOString().slice(0, 10);
            await fieldsApi.backfillIndices(fieldId, {
                force: true,
                date_from: refreshDateFrom,
                date_to: today,
            });
            setBackfillActive(true);
            setBackfillProgress((prev) =>
                prev
                    ? { ...prev, has_active_backfill: true, phase: "stac", message: t("refreshInProgress") }
                    : {
                          field_id: fieldId,
                          has_active_backfill: true,
                          pending_jobs: 0,
                          running_jobs: 0,
                          completed_jobs: 0,
                          failed_jobs: 0,
                          total_jobs: 0,
                          percent: 0,
                          phase: "stac",
                          message: t("refreshInProgress"),
                      },
            );
            toast.success(t("refreshStarted"));
            startBackfillPoll();
        } catch (e: any) {
            if (e?.status === 409) {
                setBackfillActive(true);
                startBackfillPoll();
            }
            toast.error(e?.detail || t("refreshFailed"));
        } finally {
            setBackfilling(false);
        }
    };

    // When switching series, keep date if still valid; else prefer usable optical scene
    useEffect(() => {
        if (!scenes.length) return; // wait for initial scenes fetch; do not null out date early
        const sensor = sensorForIndex(series);
        const dates = scenes
            .filter((s) => s.sensor === sensor)
            .map((s) => s.date)
            .sort();
        if (!dates.length) {
            setSelectedDate(null);
            return;
        }
        setSelectedDate((prev) =>
            prev && dates.includes(prev) ? prev : (pickBestDefaultDate(scenes, series) ?? dates[dates.length - 1]),
        );
    }, [series, scenes]);

    const publishHeatmap = useCallback(
        (img: AgriHeatmapImage | null) => {
            if (!onHeatmapChange) return;
            if (enabledRef.current && heatmapVisibleRef.current) {
                onHeatmapChange(img);
            }
        },
        [onHeatmapChange],
    );

    const loadHeatmap = useCallback(
        async (date: string, index: SeriesKey) => {
            if (!landId) return;
            const gen = ++heatmapLoadGenRef.current;
            const meta = SERIES_META[index];
            setHeatmapLoading(true);
            try {
                const res = await agriApi.scenes(landId, {
                    sensor: meta.sensor,
                    from: date,
                    to: date,
                    limit: 5,
                    includePixels: 1,
                });
                // Ignore stale overlapping NDVI/EVI (or date) loads
                if (gen !== heatmapLoadGenRef.current) return;
                const scene =
                    res.items.find((s) => (s.pixels_lonlat?.length ?? 0) > 0) ??
                    res.items.find((s) => (s.pixel_data?.pixels?.length ?? 0) > 0) ??
                    res.items[0];
                if (index === "drought" && scene && !isLowCloud(scene)) {
                    cachedHeatmapRef.current = { date, index, img: null };
                    setHeatmapMeta(null);
                    publishHeatmap(null);
                    if (enabledRef.current) {
                        toast.message(t("cloudSkipDrought"), {
                            description: `${date} · ${AGRI_MODE_LABELS[index]}`,
                        });
                    }
                    return;
                }
                const lonlat = scene?.pixels_lonlat;
                const grid = scene?.pixel_data;
                if (!(lonlat?.length || grid?.pixels?.length)) {
                    cachedHeatmapRef.current = { date, index, img: null };
                    setHeatmapMeta(null);
                    publishHeatmap(null);
                    if (enabledRef.current) {
                        toast.message("该日期无像素数据，无法渲染色斑图", {
                            description: `${meta.sensor} · ${date} · ${AGRI_MODE_LABELS[index]}`,
                        });
                    }
                    console.warn("[agri-heatmap] missing pixels_lonlat/pixel_data", {
                        landId,
                        date,
                        index,
                        source: scene?.pixels_source,
                    });
                    return;
                }
                // Day pixel NDVI grade shares (图一) — prefer lonlat; clear pixels preferred
                if (meta.sensor === "S2" && (index === "ndvi" || index === "drought" || index === "evi")) {
                    const sharePixels = lonlat?.length
                        ? lonlat
                        : null;
                    if (sharePixels?.length) {
                        const share = computePixelNdviGradeShares(sharePixels);
                        if (share) {
                            setDayGradeByDate((prev) =>
                                prev[date] && prev[date]!.n === share.n ? prev : { ...prev, [date]: share },
                            );
                        }
                    }
                }
                const img = lonlat?.length
                    ? rasterizeAgriLonLatPixels(lonlat, index, meta.sensor)
                    : rasterizeAgriPixels(grid!, index, meta.sensor);
                if (img && scene) {
                    img.previewRgbUrl = scene.rgb_url ?? null;
                    img.previewLargeRgbUrl = scene.large_rgb_url ?? null;
                    img.previewHeatmapUrl = scene.heatmap_url ?? null;
                    img.previewS2HeatmapUrl = scene.s2_heatmap_url ?? null;
                }
                if (gen !== heatmapLoadGenRef.current) return;
                cachedHeatmapRef.current = { date, index, img };
                setHeatmapMeta(
                    img
                        ? {
                              pixels: img.pixelCount,
                              date,
                              index: AGRI_MODE_LABELS[index],
                              mean: img.mean,
                          }
                        : null,
                );
                publishHeatmap(img);
                if (!img && enabledRef.current) {
                    toast.message("色斑图未绘制任何像素", {
                        description: `${date} · ${AGRI_MODE_LABELS[index]}`,
                    });
                }
            } catch (e) {
                if (gen !== heatmapLoadGenRef.current) return;
                cachedHeatmapRef.current = { date, index, img: null };
                setHeatmapMeta(null);
                publishHeatmap(null);
                console.warn("[agri-heatmap] load failed", e);
            } finally {
                if (gen === heatmapLoadGenRef.current) {
                    setHeatmapLoading(false);
                }
            }
        },
        [landId, publishHeatmap, t],
    );
    loadHeatmapRef.current = loadHeatmap;

    /** Background-only: fetch pixels for grade shares without touching map overlay. */
    const prefetchDayGradeShares = useCallback(
        async (dates: string[]) => {
            if (!landId || !dates.length) return;
            for (const date of dates) {
                if (dayGradeByDateRef.current[date]) continue;
                try {
                    const res = await agriApi.scenes(landId, {
                        sensor: "S2",
                        from: date,
                        to: date,
                        limit: 3,
                        includePixels: 1,
                    });
                    const scene =
                        res.items.find((s) => (s.pixels_lonlat?.length ?? 0) > 0) ?? res.items[0];
                    const lonlat = scene?.pixels_lonlat;
                    if (!lonlat?.length) continue;
                    const share = computePixelNdviGradeShares(lonlat);
                    if (!share) continue;
                    setDayGradeByDate((prev) => (prev[date] ? prev : { ...prev, [date]: share }));
                } catch {
                    /* ignore prefetch errors */
                }
            }
        },
        [landId],
    );

    const selectDateExplicit = useCallback(
        (date: string) => {
            setSelectedDate(date);
            const sensor = sensorForIndex(series);
            const scene = scenes.find((s) => s.sensor === sensor && s.date === date);
            if (sceneLooksCloudyOrLowVeg(scene, series)) {
                toast.message("该日多为云或植被指数极低，色膜偏红属正常", {
                    description: `${date} · ${AGRI_MODE_LABELS[series]}`,
                });
            }
        },
        [scenes, series],
    );


    // Prefetch/reload film whenever date or series changes (tab may be inactive).
    useEffect(() => {
        if (!selectedDate) return;
        const cache = cachedHeatmapRef.current;
        if (cache && cache.date === selectedDate && cache.index === series) {
            // Already have this film — publish if tab active, else keep cache warm
            if (enabledRef.current && heatmapVisibleRef.current) {
                onHeatmapChange?.(cache.img);
            }
            return;
        }
        void loadHeatmap(selectedDate, series);
    }, [selectedDate, series, loadHeatmap, onHeatmapChange]);

    // enabled gate: clear map overlay only (keep cached film). On rise → apply cache or fetch.
    useEffect(() => {
        if (!enabled) {
            // Do NOT bump gen / cancel prefetch — keep in-flight include_pixels result
            onHeatmapChange?.(null);
            return;
        }
        if (!heatmapVisible) {
            onHeatmapChange?.(null);
            return;
        }
        if (!selectedDate) return;
        const cache = cachedHeatmapRef.current;
        if (cache && cache.date === selectedDate && cache.index === series) {
            onHeatmapChange?.(cache.img);
            return;
        }
        void loadHeatmap(selectedDate, series);
    }, [enabled, heatmapVisible, selectedDate, series, loadHeatmap, onHeatmapChange]);

    const stats = useMemo(() => scenesToStats(scenes, series), [scenes, series]);

    const availableKeys = useMemo(
        () => BUTTON_ORDER.filter((key) => seriesIsAvailable(key, scenes)),
        [scenes],
    );
    const primaryKeys = useMemo(() => {
        const preferred = PRIMARY_SERIES_KEYS.filter((k) => availableKeys.includes(k));
        return preferred.length ? preferred : availableKeys.slice(0, 4);
    }, [availableKeys]);
    const overflowKeys = useMemo(
        () => availableKeys.filter((k) => !primaryKeys.includes(k)),
        [availableKeys, primaryKeys],
    );
    const seriesInOverflow = overflowKeys.includes(series);

    const allDates = useMemo(
        () => [...new Set(stats.map((s) => s.date))].sort((a, b) => b.localeCompare(a)),
        [stats],
    );
    const recentDates = useMemo(() => {
        if (!allDates.length) return [] as string[];
        const latest = allDates[0]!; // already desc
        const latestMs = Date.parse(`${latest}T00:00:00Z`);
        if (!Number.isFinite(latestMs)) return allDates.slice(0, RECENT_DATE_CHIP_CAP);
        const cutoff = latestMs - RECENT_DATE_WINDOW_MS;
        const inWindow = allDates.filter((d) => {
            const ms = Date.parse(`${d}T00:00:00Z`);
            return Number.isFinite(ms) && ms >= cutoff;
        });
        return inWindow.slice(0, RECENT_DATE_CHIP_CAP);
    }, [allDates]);
    const chipDates = useMemo(() => {
        const set = new Set(recentDates);
        if (selectedDate && !set.has(selectedDate) && allDates.includes(selectedDate)) {
            return [selectedDate, ...recentDates];
        }
        return recentDates;
    }, [recentDates, selectedDate, allDates]);

    const droughtByDate = useMemo(() => {
        const out: Record<string, AgriDroughtClass> = {};
        for (const s of scenes) {
            if (s.sensor !== "S2") continue;
            if (!isLowCloud(s)) continue;
            const cls = droughtClassFromAvgs(s.ndvi_avg, s.ndmi_avg);
            if (cls && isDroughtDayClass(cls)) {
                const prev = out[s.date];
                if (!prev) {
                    out[s.date] = cls;
                    continue;
                }
                const rank: Record<AgriDroughtClass, number> = {
                    normal: 0,
                    mild: 1,
                    moderate: 2,
                    severe: 3,
                };
                if (rank[cls] > rank[prev]) out[s.date] = cls;
            }
        }
        return out;
    }, [scenes]);

    const droughtEventMarks = useMemo(
        () =>
            Object.entries(droughtByDate)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([date, cls]) => ({
                    date,
                    label: DROUGHT_CLASS_STYLE[cls].label,
                    level:
                        cls === "severe"
                            ? ("high" as const)
                            : cls === "moderate"
                              ? ("medium" as const)
                              : ("low" as const),
                })),
        [droughtByDate],
    );

    const droughtDayCount = droughtEventMarks.length;
    const clearS2Count = useMemo(() => {
        const dates = new Set<string>();
        for (const s of scenes) {
            if (s.sensor === "S2" && isLowCloud(s) && typeof s.ndvi_avg === "number") {
                dates.add(s.date);
            }
        }
        return dates.size;
    }, [scenes]);

    const cloudPctByDate = useMemo(() => {
        const sensor = sensorForIndex(series);
        const out: Record<string, number | null> = {};
        for (const d of allDates) {
            const scene = scenes.find((s) => s.sensor === sensor && s.date === d);
            out[d] = sceneCloudPct(scene);
        }
        return out;
    }, [allDates, scenes, series]);

    const seasonMonths = useMemo(
        () => cropOption?.season_months ?? [6, 7, 8, 9],
        [cropOption?.season_months],
    );
    const peakMonths = useMemo(
        () => cropOption?.peak_months ?? [7, 8],
        [cropOption?.peak_months],
    );

    const selectedBare = useMemo(() => {
        if (!selectedDate || (series !== "ndvi" && series !== "drought" && series !== "evi")) return false;
        const st = stats.find((s) => s.date === selectedDate);
        if (!st || st.mean == null) return false;
        const m = Number(selectedDate.slice(5, 7));
        return peakMonths.includes(m) && st.mean < UNCROPPED_NDVI;
    }, [selectedDate, stats, series, peakMonths]);

    const areaMu = useMemo(() => {
        if (areaHa == null || !Number.isFinite(areaHa)) return null;
        return haToMu(areaHa);
    }, [areaHa]);

    const selectedDayShare = useMemo(() => {
        if (!selectedDate) return null;
        return dayGradeByDate[selectedDate] ?? null;
    }, [selectedDate, dayGradeByDate]);

    /** Selected scene for current series sensor + date (cloud cover, etc.). */
    const selectedScene = useMemo(() => {
        if (!selectedDate) return null;
        const sensor = sensorForIndex(series);
        return scenes.find((s) => s.sensor === sensor && s.date === selectedDate) ?? null;
    }, [scenes, selectedDate, series]);

    const cloudCoverPct = useMemo(() => {
        if (!selectedScene || sensorForIndex(series) !== "S2") return null;
        return sceneCloudPct(selectedScene);
    }, [selectedScene, series]);

    const cloudCoverOver30 = useMemo(() => {
        if (!selectedScene) return false;
        if (selectedScene.cloud_cover_over_30 === true) return true;
        if (cloudCoverPct != null && cloudCoverPct > 30) return true;
        return false;
    }, [selectedScene, cloudCoverPct]);

    const sceneMeanByDate = useMemo(() => {
        const out: Record<string, number | null> = {};
        for (const s of scenes) {
            if (s.sensor !== "S2") continue;
            if (typeof s.ndvi_avg === "number" && Number.isFinite(s.ndvi_avg)) {
                out[s.date] = s.ndvi_avg;
            }
        }
        return out;
    }, [scenes]);

    // Prefetch up to ~12 recent in-season S2 dates for stacked 图一 (no map publish)
    useEffect(() => {
        if (!landId || !scenes.length) return;
        const s2Dates = [
            ...new Set(
                scenes
                    .filter((s) => s.sensor === "S2" && typeof s.ndvi_avg === "number")
                    .map((s) => s.date),
            ),
        ].sort();
        const inSeason = s2Dates.filter((d) => seasonMonths.includes(Number(d.slice(5, 7))));
        const pool = (inSeason.length ? inSeason : s2Dates).slice(-12);
        const prefetchKey = `${landId}:${pool.join(",")}`;
        if (gradePrefetchDoneRef.current === prefetchKey) return;
        gradePrefetchDoneRef.current = prefetchKey;
        const missing = pool.filter((d) => !dayGradeByDateRef.current[d]);
        if (!missing.length) return;
        void prefetchDayGradeShares(missing);
    }, [landId, scenes, seasonMonths, prefetchDayGradeShares]);

    const total = summary?.total ?? 0;

    if (!landId) return null;
    if (!loading && hasMonitoringData && total === 0) return null;

    const CHART_INDEX_TYPE: Partial<Record<SeriesKey, IndexType>> = {
        ndvi: "NDVI",
        evi: "EVI",
        ndmi: "NDMI",
        ndre: "NDRE",
        cire: "CIRE",
        mndwi: "MNDWI",
        drought: "NDVI",
        vv: "VV",
        vh: "VH",
        flood: "VV",
    };
    const chartIndexType: IndexType = CHART_INDEX_TYPE[series] ?? "NDVI";

    return (
        <Card className="border-primary/20 bg-primary-subtle/30">
            <CardHeader className="pb-3 pt-3.5 px-3.5">
                <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                        <CardTitle className="flex flex-wrap items-center gap-1.5 text-sm font-semibold tracking-tight">
                            <Satellite className="h-3.5 w-3.5 shrink-0 text-primary" />
                            <span>{t("title")}</span>
                            <AgriIndexGlossary
                                initialKey={series}
                                triggerClassName="h-6 ml-0.5 font-normal"
                            />
                        </CardTitle>
                        <p className="text-[11px] leading-snug text-muted-foreground">
                            {t("subtitle", {
                                landId: landId ?? "—",
                                scenes: summary ? t("scenesCount", { total: summary.total }) : "",
                            })}
                        </p>
                    </div>
                    <div className="flex flex-col items-end gap-1.5 shrink-0">
                        <Button
                            type="button"
                            size="sm"
                            variant="default"
                            className="h-7 text-xs gap-1.5"
                            onClick={openRefreshRsDialog}
                            disabled={backfilling || backfillActive}
                            title={backfillActive ? t("refreshInProgress") : t("refreshRsTitle")}
                        >
                            {backfilling || backfillActive ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                                <RefreshCw className="h-3.5 w-3.5" />
                            )}
                            {t("refreshRs")}
                        </Button>
                        {summary && (
                            <div className="flex flex-wrap gap-1 justify-end">
                                {summary.sensors.map((s) => (
                                    <Badge key={s.sensor} variant="secondary" className="text-[10px] tabular-nums">
                                        {s.sensor} {s.count}
                                    </Badge>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </CardHeader>
            <CardContent className="px-3.5 pb-3.5 pt-0 space-y-3">
                <Dialog open={refreshDateOpen} onOpenChange={setRefreshDateOpen}>
                    <DialogContent className="sm:max-w-md">
                        <DialogHeader>
                            <DialogTitle>{t("refreshRsDateTitle")}</DialogTitle>
                            <DialogDescription>{t("refreshRsDateDesc")}</DialogDescription>
                        </DialogHeader>
                        <div className="grid gap-2 py-2">
                            <Label htmlFor="rs-date-from">{t("refreshRsDateFrom")}</Label>
                            <Input
                                id="rs-date-from"
                                type="date"
                                value={refreshDateFrom}
                                max={new Date().toISOString().slice(0, 10)}
                                onChange={(e) => setRefreshDateFrom(e.target.value)}
                            />
                        </div>
                        <DialogFooter className="gap-2 sm:gap-0">
                            <Button type="button" variant="outline" onClick={() => setRefreshDateOpen(false)}>
                                {t("refreshRsDateCancel")}
                            </Button>
                            <Button type="button" onClick={handleRefreshRs} disabled={!refreshDateFrom || backfilling}>
                                {t("refreshRsDateConfirm")}
                            </Button>
                        </DialogFooter>
                    </DialogContent>
                </Dialog>
                {backfillActive && (
                    <div className="rounded-md border border-info/30 bg-info-subtle/60 px-2.5 py-2 space-y-1.5">
                        <p className="text-[11px] text-info flex items-center gap-1.5 font-medium">
                            <History className="h-3 w-3 shrink-0" />
                            {backfillProgress?.phase === "bridge"
                                ? t("progressBridge")
                                : backfillProgress?.message || t("refreshInProgress")}
                        </p>
                        {backfillProgress && backfillProgress.phase !== "bridge" && (
                            <p className="text-[10px] text-info/80 tabular-nums">
                                {t("progressCounts", {
                                    done: backfillProgress.completed_jobs,
                                    total: Math.max(
                                        backfillProgress.total_jobs,
                                        backfillProgress.completed_jobs
                                            + backfillProgress.pending_jobs
                                            + backfillProgress.running_jobs,
                                    ),
                                    running: backfillProgress.running_jobs,
                                    pending: backfillProgress.pending_jobs,
                                })}
                            </p>
                        )}
                        <div className="h-1.5 w-full rounded-full bg-info/15 overflow-hidden">
                            <div
                                className="h-full rounded-full bg-info transition-all duration-500"
                                style={{
                                    width: `${
                                        backfillProgress?.phase === "bridge"
                                            ? 100
                                            : Math.min(100, Math.max(2, backfillProgress?.percent ?? 0))
                                    }%`,
                                }}
                            />
                        </div>
                    </div>
                )}
                {loading && (
                    <div className="flex items-center justify-center py-8 gap-2 text-muted-foreground text-xs">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        {t("loading")}
                    </div>
                )}
                {error && <p className="text-xs text-destructive py-2">{error}</p>}
                {!loading && !error && total === 0 && (
                    <p className="text-xs text-muted-foreground py-3">
                        {t("empty")}
                    </p>
                )}
                {!loading && total > 0 && (
                    <>
                        <div className="flex flex-nowrap gap-1.5 items-center overflow-x-auto">
                            {primaryKeys.map((key) => {
                                const meta = SERIES_META[key];
                                return (
                                    <Button
                                        key={key}
                                        type="button"
                                        size="sm"
                                        variant={series === key ? "default" : "outline"}
                                        className={cn(
                                            "h-7 text-xs px-2.5 shrink-0",
                                            (key === "drought" || key === "flood") &&
                                                series !== key &&
                                                "border-primary/40",
                                        )}
                                        onClick={() => setSeries(key)}
                                        title={meta.hint}
                                    >
                                        {meta.label}
                                    </Button>
                                );
                            })}
                            {seriesInOverflow && (
                                <Button
                                    type="button"
                                    size="sm"
                                    variant="default"
                                    className="h-7 text-xs px-2.5 shrink-0"
                                    onClick={() => setSeries(series)}
                                    title={SERIES_META[series].hint}
                                >
                                    {SERIES_META[series].label}
                                </Button>
                            )}
                            {overflowKeys.length > 0 && (
                                <DropdownMenu>
                                    <DropdownMenuTrigger asChild>
                                        <Button
                                            type="button"
                                            size="sm"
                                            variant={seriesInOverflow ? "secondary" : "outline"}
                                            className="h-7 w-7 p-0 shrink-0"
                                            title="更多指数"
                                        >
                                            <MoreHorizontal className="h-3.5 w-3.5" />
                                            <span className="sr-only">更多指数</span>
                                        </Button>
                                    </DropdownMenuTrigger>
                                    <DropdownMenuContent align="start" className="min-w-[10rem]">
                                        {overflowKeys.map((key) => {
                                            const meta = SERIES_META[key];
                                            const active = series === key;
                                            return (
                                                <DropdownMenuItem
                                                    key={key}
                                                    onSelect={() => setSeries(key)}
                                                    className="text-xs gap-2"
                                                >
                                                    <span className="flex-1">{meta.label}</span>
                                                    {active ? (
                                                        <Check className="h-3.5 w-3.5 text-primary" />
                                                    ) : null}
                                                </DropdownMenuItem>
                                            );
                                        })}
                                    </DropdownMenuContent>
                                </DropdownMenu>
                            )}
                            <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                className="h-7 w-7 p-0 shrink-0 ml-auto"
                                title={heatmapVisible ? t("hideHeatmap") : t("showHeatmap")}
                                onClick={() => {
                                    setHeatmapVisible((v) => {
                                        const next = !v;
                                        if (!next) onHeatmapChange?.(null);
                                        return next;
                                    });
                                }}
                            >
                                {heatmapVisible ? (
                                    <Eye className="h-3.5 w-3.5" />
                                ) : (
                                    <EyeOff className="h-3.5 w-3.5" />
                                )}
                            </Button>
                        </div>
                        {(series === "drought" || series === "flood") && (
                            <p className="text-[11px] text-muted-foreground leading-snug">
                                {SERIES_META[series].hint}
                                {series === "drought" && clearS2Count > 0
                                    ? ` · ${t("droughtDaysCount", { drought: droughtDayCount, clear: clearS2Count })}`
                                    : ""}
                            </p>
                        )}
                        {(series === "ndvi" || series === "evi" || series === "drought") && (
                            <>
                                <div className="rounded-md border border-border/60 bg-background/70 px-2.5 py-2 space-y-1.5">
                                    <div className="flex flex-wrap items-center gap-1.5">
                                        <span className="text-[11px] font-medium text-foreground">当日长势等级</span>
                                        <Badge variant="secondary" className="text-[10px]">
                                            {cropOption?.season_label_zh || "夏玉米季（6–9月）"}
                                        </Badge>
                                        {selectedBare && (
                                            <Badge variant="destructive" className="text-[10px]">
                                                疑似未种植/裸地（旺季 NDVI 低于 {UNCROPPED_NDVI}）
                                            </Badge>
                                        )}
                                    </div>
                                    <p className="text-[10px] text-muted-foreground leading-snug">
                                        分档按像元 NDVI：{NDVI_DAY_GRADE_RULE_ZH}；圆环中心为地块面积（亩）。
                                    </p>
                                    <NdviGradeSharesChart
                                        variant="donut"
                                        selectedShare={selectedDayShare}
                                        areaMu={areaMu}
                                        selectedDate={selectedDate}
                                        height={200}
                                    />
                                </div>
                                <div className="rounded-md border border-border/60 bg-background/70 px-2.5 py-2 space-y-1">
                                    <span className="text-[11px] font-medium text-foreground">多日长势占比趋势</span>
                                    <NdviGradeSharesChart
                                        variant="stacked"
                                        historyByDate={dayGradeByDate}
                                        meanByDate={sceneMeanByDate}
                                        height={250}
                                    />
                                </div>
                            </>
                        )}
                        <div className="rounded-md border border-border/60 bg-background/70 px-2.5 py-2">
                            {stats.length > 0 ? (
                                <NdviChart
                                    stats={stats}
                                    selectedDate={selectedDate}
                                    onDateSelect={(d) => selectDateExplicit(d)}
                                    height={220}
                                    indexType={chartIndexType}
                                    eventMarks={
                                        series === "drought" || series === "ndvi" || series === "ndmi"
                                            ? droughtEventMarks
                                            : undefined
                                    }
                                />
                            ) : (
                                <p className="text-xs text-muted-foreground py-2">{t("noMeanPoints")}</p>
                            )}
                        </div>
                        <div className="space-y-2 rounded-md border border-border/50 bg-background/40 px-2.5 py-2">
                            <p className="text-[11px] text-muted-foreground leading-relaxed">
                                {selectedDate
                                    ? t("heatmapDate", { date: selectedDate, mode: AGRI_MODE_LABELS[series] })
                                    : t("pickDate")}
                                {cloudCoverPct != null
                                    ? ` · ${t("cloudCover", { percent: Math.round(cloudCoverPct) })}`
                                    : ""}
                                {heatmapLoading ? t("rendering") : ""}
                                {heatmapMeta
                                    ? `${t("pixelsMeta", { pixels: heatmapMeta.pixels })}${
                                          heatmapMeta.mean != null
                                              ? t("meanMeta", { mean: heatmapMeta.mean.toFixed(2) })
                                              : ""
                                      }`
                                    : ""}
                            </p>
                            {selectedDate && (
                                <div className="space-y-2">
                                    <div className="flex flex-wrap gap-1.5 items-center">
                                        {chipDates.map((date) => {
                                            const active = selectedDate === date;
                                            const chipCloud = active ? cloudCoverPct : cloudPctByDate[date];
                                            const chipOver30 =
                                                typeof chipCloud === "number" &&
                                                chipCloud > DROUGHT_CLOUD_MAX_PCT;
                                            const droughtCls = droughtByDate[date];
                                            return (
                                                <Button
                                                    key={date}
                                                    type="button"
                                                    size="sm"
                                                    variant={active ? "default" : "outline"}
                                                    className={cn(
                                                        "h-6 text-[10px] px-1.5 tabular-nums shrink-0 gap-1",
                                                        active && cloudCoverOver30 && "ring-1 ring-warning/50",
                                                    )}
                                                    onClick={() => selectDateExplicit(date)}
                                                >
                                                    {date.slice(5)}
                                                    {droughtCls && (
                                                        <span
                                                            className={cn(
                                                                "rounded px-0.5 text-[9px] font-medium",
                                                                droughtCls === "severe" &&
                                                                    "bg-danger-subtle text-sev-high",
                                                                droughtCls === "moderate" &&
                                                                    "bg-warning-subtle text-warning",
                                                                droughtCls === "mild" &&
                                                                    "bg-caution-subtle text-caution",
                                                            )}
                                                        >
                                                            {droughtCls === "severe"
                                                                ? t("droughtChip_severe")
                                                                : droughtCls === "moderate"
                                                                  ? t("droughtChip_moderate")
                                                                  : t("droughtChip_mild")}
                                                        </span>
                                                    )}
                                                    {active && chipCloud != null && (
                                                        <span
                                                            className={cn(
                                                                "rounded px-0.5 text-[9px] font-normal tabular-nums",
                                                                chipOver30
                                                                    ? "bg-warning-subtle text-warning"
                                                                    : "bg-primary-foreground/15 text-primary-foreground",
                                                            )}
                                                        >
                                                            {Math.round(chipCloud)}%
                                                        </span>
                                                    )}
                                                </Button>
                                            );
                                        })}
                                    </div>
                                    {allDates.length > 0 && (
                                        <div className="flex items-center gap-2 min-w-0">
                                            <span className="text-[10px] text-muted-foreground shrink-0">
                                                全年日期
                                            </span>
                                            <Select
                                                value={selectedDate}
                                                onValueChange={(v) => selectDateExplicit(v)}
                                            >
                                                <SelectTrigger className="h-7 text-[11px] w-full max-w-[14rem]">
                                                    <SelectValue placeholder="选择日期" />
                                                </SelectTrigger>
                                                <SelectContent className="max-h-72">
                                                    {allDates.map((date) => {
                                                        const pct = cloudPctByDate[date];
                                                        const droughtCls = droughtByDate[date];
                                                        const droughtBit = droughtCls
                                                            ? ` · ${
                                                                  droughtCls === "severe"
                                                                      ? t("droughtChip_severe")
                                                                      : droughtCls === "moderate"
                                                                        ? t("droughtChip_moderate")
                                                                        : t("droughtChip_mild")
                                                              }`
                                                            : "";
                                                        const label =
                                                            pct != null
                                                                ? `${date} · ${t("cloudCover", { percent: Math.round(pct) })}${droughtBit}`
                                                                : `${date}${droughtBit}`;
                                                        return (
                                                            <SelectItem
                                                                key={date}
                                                                value={date}
                                                                className="text-xs tabular-nums"
                                                            >
                                                                {label}
                                                            </SelectItem>
                                                        );
                                                    })}
                                                </SelectContent>
                                            </Select>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    </>
                )}
            </CardContent>
        </Card>
    );
}

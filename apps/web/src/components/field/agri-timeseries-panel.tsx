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
    type GrowingSeasonWindow,
    type HarvestDetectResult,
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
    FLOOD_CLASS_STYLE,
    isDroughtDayClass,
    isDecloudProduct,
    isOfficialOpticalScene,
    type AgriDroughtClass,
    type AgriFloodClass,
    type AgriHeatIndex,
    type AgriHeatmapImage,
} from "@/lib/agri-heatmap";
import {
    classifyDroughtSeries,
    classifyFloodSeries,
    isDroughtSeason,
    isFloodDayClass,
    isFloodWatchClass,
    isSpringFloodMonth,
    opticalTooltipFields,
    pickOfficialOptical,
    pickOpticalForNdvi,
    sceneCloudDisplay,
    sceneCloudPct,
} from "@/lib/agri-classify";
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
import { Loader2, Eye, EyeOff, RefreshCw, History, MoreHorizontal, Check, Plus, Trash2 } from "lucide-react";
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
        hint: "生育季 6–9 月 · 官方晴空或去云良好 · (NDDI 偏高或同月百分位) 且 (NDMI 干或 NDVI 偏低)",
    },
    flood: {
        label: "洪涝",
        sensor: "S1",
        avgKey: null,
        chartKey: "vv_avg",
        hint: "S1 分轨道 VV 基线：VV≤−17 dB 且下降≥3 dB 且 VH/差分辅助；近阈值为关注。春灌积水可能不是灾害洪涝",
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
    const byDate = new Map<string, AgriSceneProduct[]>();
    for (const s of scenes) {
        if (s.sensor !== meta.sensor) continue;
        if (key === "drought" && isDecloudProduct(s) && s.decloud_quality !== "good") continue;
        const arr = byDate.get(s.date) ?? [];
        arr.push(s);
        byDate.set(s.date, arr);
    }
    const out: FieldStat[] = [];
    const dates = [...byDate.keys()].sort((a, b) => a.localeCompare(b));
    dates.forEach((date, i) => {
        const group = byDate.get(date) ?? [];
        const picked =
            meta.sensor === "S2"
                ? key === "drought"
                    ? pickOfficialOptical(group, { neighbors: scenes })
                    : pickOpticalForNdvi(group, { neighbors: scenes })
                : group[0];
        if (!picked) return;
        if (key === "drought" && !isOfficialOpticalScene(picked)) return;
        const v = picked[avgKey];
        if (typeof v !== "number" || Number.isNaN(v)) return;
        const tip = opticalTooltipFields(picked);
        out.push({
            id: `agri-${key}-${date}-${i}`,
            field_id: "",
            date,
            mean: v,
            median: v,
            min: v,
            max: v,
            p10: v,
            p90: v,
            stddev: null,
            quality_score: null,
            created_at: "",
            cloud_cover: tip.cloudCover,
            decloud_quality: tip.decloudQuality,
            decloud_reasons: tip.decloudReasons,
            product_source: tip.productSource,
            scene_id: tip.sceneId,
            may_be_unreliable: tip.mayBeUnreliable,
        });
    });
    return out;
}

function formatCloudCoverLabel(
    pct: number | null | undefined,
    source: "parcel" | "stac" | null | undefined,
    t: (key: string, values?: Record<string, string | number>) => string,
): string {
    if (pct == null || !Number.isFinite(pct)) return "";
    const rounded = Math.round(pct);
    if (source === "parcel") return t("parcelCloudCover", { percent: rounded });
    if (source === "stac") return t("sceneCloudCover", { percent: rounded });
    return t("cloudCover", { percent: rounded });
}

function decloudQualityChip(
    quality: string | null | undefined,
    t: (key: string) => string,
): string {
    if (quality === "good") return ` · ${t("decloudChip_good")}`;
    if (quality === "fair") return ` · ${t("decloudChip_fair")}`;
    if (quality === "bad") return ` · ${t("decloudChip_bad")}`;
    return "";
}

function collectDecloudAltStats(
    scenes: AgriSceneProduct[],
    key: SeriesKey,
    mainStats: FieldStat[],
): FieldStat[] {
    const meta = SERIES_META[key];
    if (key === "drought" || meta.sensor !== "S2") return [];
    const avgKey = meta.chartKey;
    if (!avgKey) return [];
    const pickedIds = new Set(mainStats.map((s) => s.scene_id).filter(Boolean));
    const out: FieldStat[] = [];
    for (const s of scenes) {
        if (s.sensor !== "S2" || !isDecloudProduct(s)) continue;
        if (s.scene_id && pickedIds.has(s.scene_id)) continue;
        let v = s[avgKey];
        // Fair/bad reconstructions sometimes persisted with null ndvi_avg while
        // pixels are all 0 — still show the pink alt marker so "去云无" is not the
        // only story for that date.
        if (typeof v !== "number" || Number.isNaN(v)) v = 0;
        const tip = opticalTooltipFields(s);
        out.push({
            id: `agri-${key}-decloud-alt-${s.date}-${s.scene_id ?? out.length}`,
            field_id: "",
            date: s.date,
            mean: v,
            median: v,
            min: v,
            max: v,
            p10: v,
            p90: v,
            stddev: null,
            quality_score: null,
            created_at: "",
            cloud_cover: tip.cloudCover,
            decloud_quality: tip.decloudQuality,
            decloud_reasons: tip.decloudReasons,
            product_source: tip.productSource,
            scene_id: tip.sceneId,
            decloud_alt: true,
            may_be_unreliable: tip.mayBeUnreliable,
        });
    }
    return out;
}


/** Avg field used to score a scene for default-date picking. */
function sceneSeriesAvg(scene: AgriSceneProduct, key: SeriesKey): number | null {
    const avgKey = SERIES_META[key].avgKey ?? SERIES_META[key].chartKey;
    if (!avgKey) return null;
    const v = scene[avgKey];
    return typeof v === "number" && Number.isFinite(v) ? v : null;
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

    // 1) Latest with official optical (clear raw or good decloud) AND (for veg) avg > 0.1
    for (let i = sorted.length - 1; i >= 0; i--) {
        const s = sorted[i]!;
        if (!isOfficialOpticalScene(s)) continue;
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
    const pct = sceneCloudPct(scene);
    const cloudy = pct != null && pct > DROUGHT_CLOUD_MAX_PCT;
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

const MAX_SEASON_WINDOWS = 3;

type SeasonWindowDraft = {
    id: string;
    start_date: string;
    end_date: string;
    crops: string[];
    label: string;
};

function newWindowId(): string {
    return `w-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

function yearFromIso(iso: string): number {
    const y = Number(String(iso || "").slice(0, 4));
    return Number.isFinite(y) && y > 1970 ? y : new Date().getFullYear();
}

function distinctCropsInWindows(windows: SeasonWindowDraft[]): string[] {
    const out: string[] = [];
    for (const w of windows) {
        for (const c of w.crops) {
            if (!out.includes(c)) out.push(c);
        }
    }
    return out;
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
    const [cropCatalog, setCropCatalog] = useState<CropOption[]>([]);
    /** Growing-season windows for this pull (rotation = multi; intercrop = crops length 2). */
    const [seasonWindows, setSeasonWindows] = useState<SeasonWindowDraft[]>([]);
    const [harvestResult, setHarvestResult] = useState<HarvestDetectResult | null>(null);
    const [harvestLoading, setHarvestLoading] = useState(false);
    const [harvestError, setHarvestError] = useState(false);
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
    const [heatmapError, setHeatmapError] = useState(false);
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
                setCropCatalog(list);
                const key = (cropType || "").toLowerCase();
                const hit =
                    list.find((c) => c.key === key) ||
                    list.find((c) => c.name_zh === cropType) ||
                    list.find((c) => c.key === "corn") ||
                    list[0] ||
                    null;
                setCropOption(hit);
                if (hit) {
                    setSeasonWindows((prev) => {
                        if (prev.length) return prev;
                        const y = new Date().getFullYear();
                        const months = hit.season_months?.length ? hit.season_months : [6, 7, 8, 9];
                        const sm = Math.min(...months);
                        const em = Math.max(...months);
                        const endDay = em === 2 ? 28 : [4, 6, 9, 11].includes(em) ? 30 : 31;
                        return [
                            {
                                id: newWindowId(),
                                start_date: `${y}-${String(sm).padStart(2, "0")}-01`,
                                end_date: `${y}-${String(em).padStart(2, "0")}-${String(endDay).padStart(2, "0")}`,
                                crops: [hit.key],
                                label: hit.season_label_zh || hit.name_zh || hit.name,
                            },
                        ];
                    });
                }
            })
            .catch(() => {
                if (!cancelled) {
                    setCropOption(null);
                    setCropCatalog([]);
                }
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
                if (!cancelled) setError(t("loadFailed"));
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
        if (!seasonWindows.length && cropOption?.key) {
            const y = yearFromIso(defaultRsDateFrom());
            const months = cropOption.season_months?.length ? cropOption.season_months : [6, 7, 8, 9];
            const sm = Math.min(...months);
            const em = Math.max(...months);
            const endDay = em === 2 ? 28 : [4, 6, 9, 11].includes(em) ? 30 : 31;
            setSeasonWindows([
                {
                    id: newWindowId(),
                    start_date: `${y}-${String(sm).padStart(2, "0")}-01`,
                    end_date: `${y}-${String(em).padStart(2, "0")}-${String(endDay).padStart(2, "0")}`,
                    crops: [cropOption.key],
                    label: cropOption.season_label_zh || cropOption.name_zh || cropOption.name,
                },
            ]);
        }
        setRefreshDateOpen(true);
    };

    const updateSeasonWindow = (id: string, patch: Partial<SeasonWindowDraft>) => {
        setSeasonWindows((prev) => prev.map((w) => (w.id === id ? { ...w, ...patch } : w)));
    };

    const toggleWindowCrop = (id: string, cropKey: string) => {
        setSeasonWindows((prev) => {
            const target = prev.find((w) => w.id === id);
            if (!target) return prev;
            const has = target.crops.includes(cropKey);
            let nextCrops: string[];
            if (has) {
                nextCrops = target.crops.filter((c) => c !== cropKey);
            } else {
                if (target.crops.length >= 2) return prev;
                const others = distinctCropsInWindows(prev.filter((w) => w.id !== id));
                const distinct = new Set([...others, ...target.crops, cropKey]);
                if (distinct.size > 2) return prev;
                nextCrops = [...target.crops, cropKey];
            }
            return prev.map((w) => (w.id === id ? { ...w, crops: nextCrops } : w));
        });
    };

    const addSeasonWindow = (preset?: "spring_corn" | "summer_corn" | "blank") => {
        const y = yearFromIso(refreshDateFrom);
        let draft: SeasonWindowDraft;
        if (preset === "spring_corn") {
            draft = {
                id: newWindowId(),
                start_date: `${y}-04-01`,
                end_date: `${y}-08-31`,
                crops: ["corn"],
                label: "春玉米",
            };
        } else if (preset === "summer_corn") {
            draft = {
                id: newWindowId(),
                start_date: `${y}-06-01`,
                end_date: `${y}-09-30`,
                crops: ["corn"],
                label: "夏玉米",
            };
        } else {
            const key = cropOption?.key || "corn";
            draft = {
                id: newWindowId(),
                start_date: `${y}-06-01`,
                end_date: `${y}-09-30`,
                crops: [key],
                label: "",
            };
        }
        setSeasonWindows((prev) => {
            if (prev.length >= MAX_SEASON_WINDOWS) {
                return prev; // max 3 rotation windows per year
            }
            const others = distinctCropsInWindows(prev);
            for (const c of draft.crops) {
                if (!others.includes(c) && others.length >= 2) {
                    return prev; // block 3rd distinct crop
                }
            }
            return [...prev, draft];
        });
    };

    const removeSeasonWindow = (id: string) => {
        setSeasonWindows((prev) => prev.filter((w) => w.id !== id));
    };

    const runHarvestDetect = async () => {
        if (!landId) return;
        setHarvestLoading(true);
        setHarvestError(false);
        try {
            const w = seasonWindows[0];
            const res = await agriApi.harvestDetect(landId, {
                start_date: w?.start_date,
                end_date: w?.end_date,
                crops: w?.crops,
                label: w?.label || undefined,
            });
            setHarvestResult(res);
            if (res.status === "detected" && res.harvest_date) {
                setSelectedDate(res.harvest_date);
                toast.success(t("harvestDetected"));
            } else if (res.status === "no_growth") {
                toast(t("harvestNoGrowth"));
            } else {
                toast(t("harvestUncertain"));
            }
        } catch {
            // 请求失败与遥感证据不足是不同状态，避免将网络异常展示为检测结论。
            setHarvestResult(null);
            setHarvestError(true);
            toast.error(t("harvestFailed"));
        } finally {
            setHarvestLoading(false);
        }
    };

    const handleRefreshRs = async () => {
        setRefreshDateOpen(false);
        setBackfilling(true);
        try {
            const today = new Date().toISOString().slice(0, 10);
            const growing_seasons: GrowingSeasonWindow[] = seasonWindows
                .filter((w) => w.start_date && w.end_date && w.crops.length > 0)
                .map((w) => ({
                    crops: w.crops.slice(0, 2),
                    start_date: w.start_date,
                    end_date: w.end_date,
                    ...(w.label.trim() ? { label: w.label.trim() } : {}),
                }));
            await fieldsApi.backfillIndices(fieldId, {
                force: true,
                date_from: refreshDateFrom,
                date_to: today,
                ...(growing_seasons.length ? { growing_seasons } : {}),
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
            setHeatmapError(false);
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
                const official = res.items.filter(isOfficialOpticalScene);
                const withLonlat = (list: AgriSceneProduct[]) =>
                    list.find((s) => (s.pixels_lonlat?.length ?? 0) > 0);
                const withGrid = (list: AgriSceneProduct[]) =>
                    list.find((s) => (s.pixel_data?.pixels?.length ?? 0) > 0);
                const scene =
                    index === "drought"
                        ? (withLonlat(official) ?? withGrid(official) ?? official[0])
                        : (withLonlat(official) ??
                          withLonlat(res.items) ??
                          withGrid(res.items) ??
                          official[0] ??
                          res.items[0]);
                if (index === "drought" && !isDroughtSeason(date, cropOption?.season_months ?? [6, 7, 8, 9])) {
                    cachedHeatmapRef.current = { date, index, img: null };
                    setHeatmapMeta(null);
                    publishHeatmap(null);
                    if (enabledRef.current) {
                        toast(t("outOfSeasonDrought"), {
                            description: `${date} · ${AGRI_MODE_LABELS[index]}`,
                        });
                    }
                    return;
                }
                if (index === "drought" && scene && !isOfficialOpticalScene(scene)) {
                    cachedHeatmapRef.current = { date, index, img: null };
                    setHeatmapMeta(null);
                    publishHeatmap(null);
                    if (enabledRef.current) {
                        toast(t("cloudSkipDrought"), {
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
                        toast("该日期无像素数据，无法渲染色斑图", {
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
                    toast("色斑图未绘制任何像素", {
                        description: `${date} · ${AGRI_MODE_LABELS[index]}`,
                    });
                }
            } catch (e) {
                if (gen !== heatmapLoadGenRef.current) return;
                cachedHeatmapRef.current = { date, index, img: null };
                setHeatmapMeta(null);
                publishHeatmap(null);
                setHeatmapError(true);
            } finally {
                if (gen === heatmapLoadGenRef.current) {
                    setHeatmapLoading(false);
                }
            }
        },
        [landId, publishHeatmap, t, cropOption?.season_months],
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
                toast("该日多为云或植被指数极低，色膜偏红属正常", {
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
    const decloudAltStats = useMemo(
        () => collectDecloudAltStats(scenes, series, stats),
        [scenes, series, stats],
    );

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

    const seasonMonths = useMemo(
        () => cropOption?.season_months ?? [6, 7, 8, 9],
        [cropOption?.season_months],
    );
    const peakMonths = useMemo(
        () => cropOption?.peak_months ?? [7, 8],
        [cropOption?.peak_months],
    );

    const droughtByDate = useMemo(() => {
        const s2 = scenes.filter((s) => s.sensor === "S2");
        const classified = classifyDroughtSeries(s2, seasonMonths);
        const out: Record<string, AgriDroughtClass> = {};
        for (const [date, cls] of classified) {
            if (isDroughtDayClass(cls)) out[date] = cls;
        }
        return out;
    }, [scenes, seasonMonths]);

    const floodByDate = useMemo(() => {
        const s1 = scenes.filter((s) => s.sensor === "S1");
        return classifyFloodSeries(s1);
    }, [scenes]);

    const droughtEventMarks = useMemo(
        () =>
            Object.entries(droughtByDate)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([date, cls]) => ({
                    date,
                    label: DROUGHT_CLASS_STYLE[cls as "mild" | "moderate" | "severe"].label,
                    level:
                        cls === "severe"
                            ? ("high" as const)
                            : cls === "moderate"
                              ? ("medium" as const)
                              : ("low" as const),
                })),
        [droughtByDate],
    );

    const floodEventMarks = useMemo(
        () =>
            [...floodByDate.entries()]
                .filter(([, cls]) => isFloodDayClass(cls) || isFloodWatchClass(cls))
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([date, cls]) => ({
                    date,
                    label: FLOOD_CLASS_STYLE[cls].label,
                    level: cls === "flood_severe" ? ("high" as const) : cls === "flood_moderate" ? ("medium" as const) : ("low" as const),
                })),
        [floodByDate],
    );

    const harvestEventMarks = useMemo(() => {
        if (harvestResult?.status === "detected" && harvestResult.harvest_date) {
            return [
                {
                    date: harvestResult.harvest_date,
                    label: "收获",
                    level: "high" as const,
                },
            ];
        }
        return [];
    }, [harvestResult]);

    const ndviEventMarks = useMemo(
        () => [...droughtEventMarks, ...harvestEventMarks],
        [droughtEventMarks, harvestEventMarks],
    );

    const droughtDayCount = droughtEventMarks.length;
    const floodDayCount = [...floodByDate.values()].filter(isFloodDayClass).length;
    const floodWatchCount = [...floodByDate.values()].filter(isFloodWatchClass).length;
    const clearS2Count = useMemo(() => {
        const dates = new Set<string>();
        for (const s of scenes) {
            if (s.sensor === "S2" && isOfficialOpticalScene(s) && typeof s.ndvi_avg === "number") {
                dates.add(s.date);
            }
        }
        return dates.size;
    }, [scenes]);

    const cloudByDate = useMemo(() => {
        const sensor = sensorForIndex(series);
        const out: Record<string, { pct: number | null; source: "parcel" | "stac" | null }> = {};
        for (const d of allDates) {
            const group = scenes.filter((s) => s.sensor === sensor && s.date === d);
            const picked =
                sensor === "S2"
                    ? series === "drought"
                        ? pickOfficialOptical(group, { neighbors: scenes })
                        : pickOpticalForNdvi(group, { neighbors: scenes })
                    : group[0];
            out[d] = sceneCloudDisplay(picked ?? null);
        }
        return out;
    }, [allDates, scenes, series]);
    const cloudPctByDate = useMemo(() => {
        const out: Record<string, number | null> = {};
        for (const [d, v] of Object.entries(cloudByDate)) out[d] = v.pct;
        return out;
    }, [cloudByDate]);

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
        const matches = scenes.filter((s) => s.sensor === sensor && s.date === selectedDate);
        if (!matches.length) return null;
        if (sensor === "S2") {
            return (
                (series === "drought"
                    ? pickOfficialOptical(matches, { neighbors: scenes })
                    : pickOpticalForNdvi(matches, { neighbors: scenes })) ??
                matches[0] ??
                null
            );
        }
        return matches[0] ?? null;
    }, [scenes, selectedDate, series]);

    const selectedCloud = useMemo(() => {
        if (!selectedScene || sensorForIndex(series) !== "S2") {
            return { pct: null as number | null, source: null as "parcel" | "stac" | null };
        }
        return sceneCloudDisplay(selectedScene);
    }, [selectedScene, series]);
    const cloudCoverPct = selectedCloud.pct;

    const cloudCoverOver30 = useMemo(() => {
        if (!selectedScene) return false;
        if (cloudCoverPct != null) return cloudCoverPct > DROUGHT_CLOUD_MAX_PCT;
        return selectedScene.cloud_cover_over_30 === true;
    }, [selectedScene, cloudCoverPct]);

    const selectedDecloudAlt = useMemo(() => {
        if (!selectedDate) return null;
        return decloudAltStats.find((s) => s.date === selectedDate) ?? null;
    }, [selectedDate, decloudAltStats]);

    const sceneMeanByDate = useMemo(() => {
        const out: Record<string, number | null> = {};
        const byDate = new Map<string, AgriSceneProduct[]>();
        for (const s of scenes) {
            if (s.sensor !== "S2") continue;
            const arr = byDate.get(s.date) ?? [];
            arr.push(s);
            byDate.set(s.date, arr);
        }
        for (const [date, group] of byDate) {
            const picked = pickOpticalForNdvi(group);
            if (picked && typeof picked.ndvi_avg === "number") out[date] = picked.ndvi_avg;
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
    const selectedMean = stats.find((point) => point.date === selectedDate)?.mean;

    return (
        <Card className="overflow-hidden border-border/60 bg-card shadow-sm">
            <CardHeader className="border-b border-border/50 bg-muted/20 px-3 py-2.5">
                <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                        <CardTitle className="flex flex-wrap items-center gap-1.5 text-xs font-semibold tracking-tight">
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
                            className="h-7 text-[11px] gap-1.5"
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
            <CardContent className="p-3 space-y-3">
                <Dialog open={refreshDateOpen} onOpenChange={setRefreshDateOpen}>
                    <DialogContent className="sm:max-w-lg">
                        <DialogHeader>
                            <DialogTitle>{t("refreshRsDateTitle")}</DialogTitle>
                            <DialogDescription>{t("refreshRsDateDesc")}</DialogDescription>
                        </DialogHeader>
                        <div className="grid gap-3 py-2">
                            <div className="grid gap-2">
                                <Label htmlFor="rs-date-from">{t("refreshRsDateFrom")}</Label>
                                <Input
                                    id="rs-date-from"
                                    type="date"
                                    value={refreshDateFrom}
                                    max={new Date().toISOString().slice(0, 10)}
                                    onChange={(e) => setRefreshDateFrom(e.target.value)}
                                />
                            </div>
                            <div className="grid gap-1.5">
                                <Label>{t("refreshRsSeasons")}</Label>
                                <p className="text-[10px] text-muted-foreground leading-snug">
                                    {t("refreshRsSeasonsHint")}
                                </p>
                                <p className="text-[10px] text-muted-foreground">{t("refreshRsMaxCrops")}
                                </p>
                                <p className="text-[10px] text-muted-foreground">{t("refreshRsMaxWindows")}</p>
                                <div className="flex flex-wrap gap-1.5 pt-0.5">
                                    <Button type="button" size="sm" variant="secondary" className="h-7 text-xs" disabled={seasonWindows.length >= MAX_SEASON_WINDOWS} onClick={() => addSeasonWindow("spring_corn")}>
                                        {t("refreshRsPresetSpringCorn")}
                                    </Button>
                                    <Button type="button" size="sm" variant="secondary" className="h-7 text-xs" disabled={seasonWindows.length >= MAX_SEASON_WINDOWS} onClick={() => addSeasonWindow("summer_corn")}>
                                        {t("refreshRsPresetSummerCorn")}
                                    </Button>
                                    <Button type="button" size="sm" variant="outline" className="h-7 text-xs" disabled={seasonWindows.length >= MAX_SEASON_WINDOWS} onClick={() => addSeasonWindow("blank")}>
                                        <Plus className="h-3 w-3 mr-1" />
                                        {t("refreshRsWindowAdd")}
                                    </Button>
                                </div>
                                <div className="space-y-2 max-h-56 overflow-y-auto pt-1">
                                    {seasonWindows.map((w, idx) => (
                                        <div key={w.id} className="rounded-md border border-border/60 p-2 space-y-1.5 bg-muted/20">
                                            <div className="flex items-center justify-between gap-2">
                                                <span className="text-[10px] font-medium text-muted-foreground">#{idx + 1}</span>
                                                <Button type="button" size="sm" variant="ghost" className="h-6 px-1.5 text-xs" onClick={() => removeSeasonWindow(w.id)}>
                                                    <Trash2 className="h-3 w-3 mr-1" />
                                                    {t("refreshRsWindowRemove")}
                                                </Button>
                                            </div>
                                            <div className="grid grid-cols-2 gap-1.5">
                                                <div className="grid gap-0.5">
                                                    <Label className="text-[10px]">{t("refreshRsWindowStart")}</Label>
                                                    <Input
                                                        type="date"
                                                        className="h-8 text-xs"
                                                        value={w.start_date}
                                                        onChange={(e) => updateSeasonWindow(w.id, { start_date: e.target.value })}
                                                    />
                                                </div>
                                                <div className="grid gap-0.5">
                                                    <Label className="text-[10px]">{t("refreshRsWindowEnd")}</Label>
                                                    <Input
                                                        type="date"
                                                        className="h-8 text-xs"
                                                        value={w.end_date}
                                                        onChange={(e) => updateSeasonWindow(w.id, { end_date: e.target.value })}
                                                    />
                                                </div>
                                            </div>
                                            <div className="grid gap-0.5">
                                                <Label className="text-[10px]">{t("refreshRsWindowCrops")}</Label>
                                                <div className="flex flex-wrap gap-1 max-h-20 overflow-y-auto">
                                                    {(cropCatalog.length ? cropCatalog : cropOption ? [cropOption] : []).slice(0, 40).map((c) => {
                                                        const on = w.crops.includes(c.key);
                                                        return (
                                                            <Button
                                                                key={c.key}
                                                                type="button"
                                                                size="sm"
                                                                variant={on ? "default" : "outline"}
                                                                className="h-6 text-[10px] px-1.5"
                                                                onClick={() => toggleWindowCrop(w.id, c.key)}
                                                            >
                                                                {c.name_zh || c.name}
                                                            </Button>
                                                        );
                                                    })}
                                                </div>
                                            </div>
                                            <div className="grid gap-0.5">
                                                <Label className="text-[10px]">{t("refreshRsWindowLabel")}</Label>
                                                <Input
                                                    className="h-8 text-xs"
                                                    value={w.label}
                                                    placeholder="春玉米 / 米豆间作"
                                                    onChange={(e) => updateSeasonWindow(w.id, { label: e.target.value })}
                                                />
                                            </div>
                                        </div>
                                    ))}
                                </div>
                                {!seasonWindows.length ? (
                                    <p className="text-[10px] text-muted-foreground">{t("refreshRsSeasonsEmpty")}</p>
                                ) : null}
                            </div>
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
                {error && (
                    <div role="alert" className="flex items-center justify-between gap-2 rounded-lg bg-destructive/5 px-3 py-2 text-[11px]">
                        <span>{error}</span>
                        <Button size="sm" variant="outline" className="h-7 text-[11px]" onClick={() => setReloadKey((k) => k + 1)}>{t("retry")}</Button>
                    </div>
                )}
                {!loading && !error && total === 0 && (
                    <p className="text-[11px] text-muted-foreground py-3 leading-relaxed">
                        {t("empty")}
                    </p>
                )}
                {!loading && total > 0 && (
                    <>
                        <div className="flex flex-wrap gap-1.5 items-center rounded-xl bg-muted/40 p-2">
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
                                {series === "flood"
                                    ? ` · ${t("floodDaysCount", { flood: floodDayCount, watch: floodWatchCount })}`
                                    : ""}
                            </p>
                        )}
                        <div className="rounded-lg bg-background p-2.5">
                            <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg bg-muted/35 p-3">
                                <Button
                                    type="button"
                                    size="sm"
                                    variant="outline"
                                    className="h-7 shrink-0 text-[11px]"
                                    disabled={!landId || harvestLoading}
                                    onClick={() => void runHarvestDetect()}
                                >
                                    {harvestLoading ? <Loader2 className="h-3 w-3 mr-1 animate-spin" /> : null}
                                    {t("harvestDetect")}
                                </Button>
                                <span className="order-last w-full text-[11px] leading-relaxed text-muted-foreground">{t("harvestDetectHint")}</span>
                                {harvestResult?.status === "detected" && harvestResult.harvest_date ? (
                                    <Badge variant="secondary" className="ml-auto text-[11px] tabular-nums">
                                        {harvestResult.harvest_date} · {t(harvestResult.confidence === "high" ? "confidenceHigh" : harvestResult.confidence === "medium" ? "confidenceMedium" : harvestResult.confidence === "low" ? "confidenceLow" : "confidenceUnknown")}
                                    </Badge>
                                ) : harvestResult ? (
                                    <Badge variant="secondary" className="ml-auto text-[11px]">
                                        {t(harvestResult.status === "no_growth" ? "harvestNoGrowthShort" : "harvestUncertainShort")}
                                    </Badge>
                                ) : null}
                                {harvestError && <p role="alert" className="w-full text-[11px] text-destructive">{t("harvestFailed")}</p>}
                            </div>
                            <div className="mb-1 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
                                <span>{AGRI_MODE_LABELS[series]}</span>
                                <span className="tabular-nums">{selectedDate ?? "—"} · {selectedMean != null ? selectedMean.toFixed(2) : "—"}</span>
                            </div>
                            {stats.length > 0 ? (
                                <NdviChart
                                    stats={stats}
                                    decloudAltStats={decloudAltStats}
                                    selectedDate={selectedDate}
                                    onDateSelect={(d) => selectDateExplicit(d)}
                                    height={280}
                                    indexType={chartIndexType}
                                    seasonMonths={
                                        series === "ndvi" ||
                                        series === "evi" ||
                                        series === "drought" ||
                                        series === "ndmi"
                                            ? seasonMonths
                                            : undefined
                                    }
                                    peakMonths={
                                        series === "ndvi" || series === "evi" || series === "drought"
                                            ? peakMonths
                                            : undefined
                                    }
                                    eventMarks={
                                        series === "drought" || series === "ndvi" || series === "ndmi"
                                            ? ndviEventMarks
                                            : series === "flood" || series === "vv"
                                              ? floodEventMarks
                                              : undefined
                                    }
                                />
                            ) : (
                                <p className="text-xs text-muted-foreground py-2">{t("noMeanPoints")}</p>
                            )}
                        </div>
                        {(series === "ndvi" || series === "evi" || series === "drought") && (
                            <>
                                <div className="rounded-lg bg-background p-2.5 space-y-3">
                                    <div className="flex flex-wrap items-center gap-1.5">
                                        <span className="text-[11px] font-medium text-foreground">当日长势等级</span>
                                        <Badge variant="secondary" className="text-[10px]">
                                            {cropOption?.season_label_zh || cropOption?.name_zh || "作物生育季"}
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
                                <div className="rounded-lg bg-background p-2.5 space-y-3">
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
                        <div className="space-y-3 rounded-lg bg-muted/20 p-2.5">
                            <p className="text-[11px] text-muted-foreground leading-relaxed">
                                {selectedDate
                                    ? t("heatmapDate", { date: selectedDate, mode: AGRI_MODE_LABELS[series] })
                                    : t("pickDate")}
                                {cloudCoverPct != null
                                    ? ` · ${formatCloudCoverLabel(cloudCoverPct, selectedCloud.source, t)}`
                                    : ""}
                                {decloudQualityChip(
                                    selectedScene?.decloud_quality ??
                                        selectedDecloudAlt?.decloud_quality,
                                    t,
                                )}
                                {series === "flood" && selectedDate && isSpringFloodMonth(selectedDate)
                                    ? ` · ${t("floodSpringNote")}`
                                    : ""}
                                {heatmapLoading ? t("rendering") : ""}
                            </p>
                            {heatmapError && (
                                <div role="alert" className="flex items-center justify-between gap-2 text-[11px] text-destructive">
                                    <span>{t("heatmapFailed")}</span>
                                    <Button size="sm" variant="outline" className="h-7 text-[11px]" onClick={() => selectedDate && void loadHeatmap(selectedDate, series)}>{t("retry")}</Button>
                                </div>
                            )}
                            {heatmapMeta && (
                                <details className="text-[11px] text-muted-foreground">
                                    <summary className="cursor-pointer">{t("dataDetails")}</summary>
                                    <p className="mt-1 tabular-nums">{t("pixelsMeta", { pixels: heatmapMeta.pixels })}{heatmapMeta.mean != null ? t("meanMeta", { mean: heatmapMeta.mean.toFixed(2) }) : ""}</p>
                                </details>
                            )}
                            {selectedDate && (
                                <div className="space-y-2">
                                    <div className="flex max-h-36 flex-wrap gap-2 items-center overflow-y-auto">
                                        {chipDates.map((date) => {
                                            const active = selectedDate === date;
                                            const chipCloud = active ? cloudCoverPct : cloudPctByDate[date];
                                            const chipOver30 =
                                                typeof chipCloud === "number" &&
                                                chipCloud > DROUGHT_CLOUD_MAX_PCT;
                                            const droughtCls = droughtByDate[date];
                                            const floodCls = floodByDate.get(date);
                                            return (
                                                <Button
                                                    key={date}
                                                    type="button"
                                                    size="sm"
                                                    variant={active ? "default" : "outline"}
                                                    className={cn(
                                                        "h-7 rounded-lg text-[11px] px-2 tabular-nums shrink-0 gap-1.5",
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
                                                    {floodCls && (isFloodDayClass(floodCls) || isFloodWatchClass(floodCls)) && (
                                                        <span
                                                            className={cn(
                                                                "rounded px-0.5 text-[9px] font-medium",
                                                                floodCls === "flood_severe" &&
                                                                    "bg-danger-subtle text-sev-high",
                                                                floodCls === "flood_moderate" &&
                                                                    "bg-info-subtle text-info",
                                                                floodCls === "watch" &&
                                                                    "bg-caution-subtle text-caution",
                                                            )}
                                                        >
                                                            {floodCls === "watch"
                                                                ? t("floodChip_watch")
                                                                : floodCls === "flood_severe"
                                                                  ? t("floodChip_severe")
                                                                  : t("floodChip_moderate")}
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
                                                        const cloud = cloudByDate[date];
                                                        const pct = cloud?.pct ?? null;
                                                        const droughtCls = droughtByDate[date];
                                                        const floodCls = floodByDate.get(date);
                                                        const droughtBit = droughtCls
                                                            ? ` · ${
                                                                  droughtCls === "severe"
                                                                      ? t("droughtChip_severe")
                                                                      : droughtCls === "moderate"
                                                                        ? t("droughtChip_moderate")
                                                                        : t("droughtChip_mild")
                                                              }`
                                                            : "";
                                                        const floodBit =
                                                            floodCls &&
                                                            (isFloodDayClass(floodCls) || isFloodWatchClass(floodCls))
                                                                ? ` · ${
                                                                      floodCls === "watch"
                                                                          ? t("floodChip_watch")
                                                                          : floodCls === "flood_severe"
                                                                            ? t("floodChip_severe")
                                                                            : t("floodChip_moderate")
                                                                  }`
                                                                : "";
                                                        const cloudBit =
                                                            pct != null
                                                                ? ` · ${formatCloudCoverLabel(pct, cloud?.source, t)}`
                                                                : "";
                                                        const label = `${date}${cloudBit}${droughtBit}${floodBit}`;
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

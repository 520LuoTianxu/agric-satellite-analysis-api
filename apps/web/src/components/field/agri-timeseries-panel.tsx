"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import {
    agriApi,
    parseAgriLandId,
    type AgriLandScenesSummary,
    type AgriSceneProduct,
    type FieldStat,
} from "@/lib/api";
import {
    rasterizeAgriPixels,
    type AgriHeatIndex,
    type AgriHeatmapImage,
} from "@/lib/agri-heatmap";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Loader2, Satellite, Eye, EyeOff } from "lucide-react";
import { cn } from "@/lib/utils";

const NdviChart = dynamic(() => import("@/components/charts/ndvi-chart"), {
    ssr: false,
    loading: () => <Skeleton className="h-[200px] w-full rounded-md" />,
});

type SeriesKey = AgriHeatIndex;

const SERIES_META: Record<
    SeriesKey,
    { label: string; sensor: "S1" | "S2"; avgKey: keyof AgriSceneProduct; hint: string }
> = {
    ndvi: { label: "NDVI（光学）", sensor: "S2", avgKey: "ndvi_avg", hint: "植被长势 · 色斑图" },
    evi: { label: "EVI（光学）", sensor: "S2", avgKey: "evi_avg", hint: "增强植被指数" },
    ndmi: { label: "NDMI", sensor: "S2", avgKey: "ndmi_avg", hint: "水分指数" },
    ndre: { label: "NDRE", sensor: "S2", avgKey: "ndre_avg", hint: "红边指数" },
    mndwi: { label: "MNDWI", sensor: "S2", avgKey: "mndwi_avg", hint: "水体指数" },
    cire: { label: "CIRE", sensor: "S2", avgKey: "cire_avg", hint: "叶绿素红边" },
    vv: { label: "VV（雷达）", sensor: "S1", avgKey: "vv_avg", hint: "Sentinel-1 同极化 · 色斑图" },
    vh: { label: "VH（雷达）", sensor: "S1", avgKey: "vh_avg", hint: "Sentinel-1 交叉极化 · 色斑图" },
};

function scenesToStats(scenes: AgriSceneProduct[], key: SeriesKey): FieldStat[] {
    const avgKey = SERIES_META[key].avgKey;
    const byDate = new Map<string, number[]>();
    for (const s of scenes) {
        if (s.sensor !== SERIES_META[key].sensor) continue;
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

export interface AgriTimeseriesPanelProps {
    fieldTags: string[] | null | undefined;
    /** When true, parent already has monitoring layers */
    hasMonitoringData?: boolean;
    /** Push 色斑图 overlay to the field map */
    onHeatmapChange?: (heatmap: AgriHeatmapImage | null) => void;
}

export default function AgriTimeseriesPanel({
    fieldTags,
    hasMonitoringData = false,
    onHeatmapChange,
}: AgriTimeseriesPanelProps) {
    const landId = useMemo(() => parseAgriLandId(fieldTags), [fieldTags]);
    const [summary, setSummary] = useState<AgriLandScenesSummary | null>(null);
    const [scenes, setScenes] = useState<AgriSceneProduct[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [series, setSeries] = useState<SeriesKey>("ndvi");
    const [selectedDate, setSelectedDate] = useState<string | null>(null);
    const [heatmapVisible, setHeatmapVisible] = useState(true);
    const [heatmapLoading, setHeatmapLoading] = useState(false);
    const [heatmapMeta, setHeatmapMeta] = useState<{ pixels: number; date: string; index: string } | null>(null);

    useEffect(() => {
        if (!landId) return;
        let cancelled = false;
        setLoading(true);
        setError(null);
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
                const nextSeries: SeriesKey = hasS2 ? "ndvi" : "vv";
                setSeries(nextSeries);
                const dates = all
                    .filter((s) => s.sensor === (hasS2 ? "S2" : "S1"))
                    .map((s) => s.date)
                    .sort();
                if (dates.length) setSelectedDate(dates[dates.length - 1]);
            } catch (e: any) {
                if (!cancelled) setError(e?.detail || e?.message || "加载 agri 时序失败");
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
            onHeatmapChange?.(null);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [landId]);

    const loadHeatmap = useCallback(
        async (date: string, index: SeriesKey) => {
            if (!landId || !onHeatmapChange) return;
            if (!heatmapVisible) {
                onHeatmapChange(null);
                setHeatmapMeta(null);
                return;
            }
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
                const scene = res.items.find((s) => s.pixel_data?.pixels?.length) ?? res.items[0];
                if (!scene?.pixel_data) {
                    onHeatmapChange(null);
                    setHeatmapMeta(null);
                    return;
                }
                const img = rasterizeAgriPixels(scene.pixel_data, index, meta.sensor);
                onHeatmapChange(img);
                setHeatmapMeta(
                    img
                        ? { pixels: img.pixelCount, date, index: index.toUpperCase() }
                        : null,
                );
            } catch {
                onHeatmapChange(null);
                setHeatmapMeta(null);
            } finally {
                setHeatmapLoading(false);
            }
        },
        [landId, onHeatmapChange, heatmapVisible],
    );

    useEffect(() => {
        if (!selectedDate) return;
        void loadHeatmap(selectedDate, series);
    }, [selectedDate, series, loadHeatmap]);

    const stats = useMemo(() => scenesToStats(scenes, series), [scenes, series]);
    const total = summary?.total ?? 0;

    if (!landId) return null;
    if (!loading && hasMonitoringData && total === 0) return null;

    return (
        <Card className="border-primary/20 bg-primary-subtle/30">
            <CardHeader className="pb-2 pt-3 px-3">
                <div className="flex items-start justify-between gap-2">
                    <div>
                        <CardTitle className="flex items-center gap-1.5 text-xs font-semibold">
                            <Satellite className="h-3.5 w-3.5 text-primary" />
                            Agri 遥感时序 · 色斑图
                        </CardTitle>
                        <p className="mt-0.5 text-[11px] text-muted-foreground">
                            地块 land_id={landId}
                            {summary ? ` · 共 ${summary.total} 景` : ""}
                            {" · 直接读 pixel_data，无需 COG/Celery"}
                        </p>
                    </div>
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
            </CardHeader>
            <CardContent className="px-3 pb-3 pt-0 space-y-2">
                {loading && (
                    <div className="flex items-center justify-center py-8 gap-2 text-muted-foreground text-xs">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        加载 agri 场景…
                    </div>
                )}
                {error && <p className="text-xs text-destructive py-2">{error}</p>}
                {!loading && !error && total === 0 && (
                    <p className="text-xs text-muted-foreground py-3">
                        本地样例库中该地块暂无 S1/S2 场景（如后广惠屯）。边界已导入；天气/土壤可按地块几何拉取。
                        色斑图与长势曲线需从 OSS 补齐 parcel_scene_products。
                    </p>
                )}
                {!loading && total > 0 && (
                    <>
                        <div className="flex flex-wrap gap-1 items-center">
                            {(Object.keys(SERIES_META) as SeriesKey[]).map((key) => {
                                const meta = SERIES_META[key];
                                const has = scenes.some(
                                    (s) =>
                                        s.sensor === meta.sensor &&
                                        typeof s[meta.avgKey] === "number",
                                );
                                if (!has) return null;
                                return (
                                    <Button
                                        key={key}
                                        type="button"
                                        size="sm"
                                        variant={series === key ? "default" : "outline"}
                                        className={cn("h-7 text-xs px-2.5")}
                                        onClick={() => setSeries(key)}
                                        title={meta.hint}
                                    >
                                        {meta.label}
                                    </Button>
                                );
                            })}
                            <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                className="h-7 w-7 p-0 ml-auto"
                                title={heatmapVisible ? "隐藏色斑图" : "显示色斑图"}
                                onClick={() => {
                                    setHeatmapVisible((v) => {
                                        const next = !v;
                                        if (!next) onHeatmapChange?.(null);
                                        return next;
                                    });
                                }}
                            >
                                {heatmapVisible ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
                            </Button>
                        </div>
                        {stats.length > 0 ? (
                            <NdviChart
                                stats={stats}
                                selectedDate={selectedDate}
                                onDateSelect={(d) => setSelectedDate(d)}
                                height={200}
                                indexType={series === "evi" ? "EVI" : "NDVI"}
                            />
                        ) : (
                            <p className="text-xs text-muted-foreground py-2">当前指数无有效均值点。</p>
                        )}
                        <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                            <span>
                                {selectedDate
                                    ? `色斑图日期：${selectedDate} · ${series.toUpperCase()}`
                                    : "选择曲线上的日期以加载色斑图"}
                                {heatmapLoading ? " · 渲染中…" : ""}
                                {heatmapMeta ? ` · ${heatmapMeta.pixels} 像素` : ""}
                            </span>
                            {selectedDate && (
                                <div className="flex gap-1 max-w-[50%] overflow-x-auto">
                                    {stats
                                        .slice(-8)
                                        .reverse()
                                        .map((s) => (
                                            <Button
                                                key={s.date}
                                                type="button"
                                                size="sm"
                                                variant={selectedDate === s.date ? "default" : "outline"}
                                                className="h-6 text-[10px] px-1.5 tabular-nums shrink-0"
                                                onClick={() => setSelectedDate(s.date)}
                                            >
                                                {s.date.slice(5)}
                                            </Button>
                                        ))}
                                </div>
                            )}
                        </div>
                    </>
                )}
            </CardContent>
        </Card>
    );
}

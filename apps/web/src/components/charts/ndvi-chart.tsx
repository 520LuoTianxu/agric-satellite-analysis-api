"use client";

import React, { useMemo } from "react";
import ReactEChartsCore from "echarts-for-react/lib/core";
import * as echarts from "echarts/core";
import { LineChart, BarChart, ScatterChart } from "echarts/charts";
import {
    GridComponent,
    TooltipComponent,
    LegendComponent,
    MarkLineComponent,
    MarkAreaComponent,
    DataZoomComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { FieldStat, IndexType, WeatherDaily } from "@/lib/api";
import { INDEX_CONFIG } from "@/lib/api";
import { tokenColor } from "@/lib/design-tokens";
import { useTranslations } from "next-intl";
import { DECLOUD_SOURCE } from "@/lib/agri-classify";
import {
    axisLabel,
    baseTooltip,
    indexBandColor,
    indexLineColor,
    legendStyle,
    secondaryValueAxis,
    sig,
    thresholdMarkLine,
    valueAxis,
} from "./chart-base";

// Register only the modules we need (tree-shake friendly)
echarts.use([
    LineChart,
    BarChart,
    ScatterChart,
    GridComponent,
    TooltipComponent,
    LegendComponent,
    MarkLineComponent,
    MarkAreaComponent,
    DataZoomComponent,
    CanvasRenderer,
]);

export type GrowthStageBand = {
    name: string;
    /** Inclusive start YYYY-MM-DD (or year-agnostic applied per year) */
    startMonth: number;
    startDay?: number;
    endMonth: number;
    endDay?: number;
    color?: string;
};

export type ChartEventMark = {
    date: string;
    value?: number | null;
    label: string;
    /** high = severe, medium = moderate, low = mild */
    level?: "high" | "medium" | "low";
};

interface NdviChartProps {
    stats: FieldStat[];
    /** Unused decloud products (fair/bad or unused good) as marked overlays. */
    decloudAltStats?: FieldStat[];
    /** Currently selected date (highlights point) */
    selectedDate?: string | null;
    /** Callback when a chart point is clicked */
    onDateSelect?: (date: string) => void;
    /** Height in pixels */
    height?: number;
    /** Which vegetation index to display */
    indexType?: IndexType;
    /** Optional weather data for overlay (precip bars + ET₀ line) */
    weatherData?: WeatherDaily[];
    /** Whether to show weather overlay (default false) */
    showWeatherOverlay?: boolean;
    /** Crop season months (1-12) — soft shade */
    seasonMonths?: number[];
    /** Peak months — stronger shade */
    peakMonths?: number[];
    /** Phenology stage bands for markArea */
    stageBands?: GrowthStageBand[];
    /** NDVI below this in peak months → treat as likely bare / uncropped */
    bareThreshold?: number;
    /** Timeline markers (e.g. historical drought days) */
    eventMarks?: ChartEventMark[];
    /** Optional caption / legend note under chart is handled by parent */
}

export default function NdviChart({
    stats,
    decloudAltStats,
    selectedDate,
    onDateSelect,
    height = 200,
    indexType = "NDVI",
    weatherData,
    showWeatherOverlay = false,
    seasonMonths,
    peakMonths,
    stageBands,
    bareThreshold = 0.25,
    eventMarks,
}: NdviChartProps) {
    const t = useTranslations("ndviChart");
    const config = INDEX_CONFIG[indexType];
    const seriesName = `Mean ${config.label}`;
    const isSar = indexType === "VV" || indexType === "VH";
    const dataVals = stats
        .map((s) => s.mean)
        .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    let yMin = config.rescaleMin;
    let yMax = config.rescaleMax + 0.1;
    if (isSar && dataVals.length > 0) {
        const dMin = Math.min(...dataVals);
        const dMax = Math.max(...dataVals);
        const pad = Math.max(1, (dMax - dMin) * 0.15 || 2);
        yMin = Math.min(config.rescaleMin, dMin - pad);
        yMax = Math.max(config.rescaleMax, dMax + pad);
    }


    // Weather data sorted by date for overlay
    const weatherSorted = useMemo(() => {
        if (!weatherData || !showWeatherOverlay || weatherData.length === 0) return null;
        return [...weatherData].sort((a, b) => a.date.localeCompare(b.date));
    }, [weatherData, showWeatherOverlay]);

    const option = useMemo(() => {
        // NDVI data as [date, value] pairs for time axis
        const bandLowData = stats.map((s) => [s.date, s.p10 ?? null] as [string, number | null]);
        const bandHighData = stats.map((s) => {
            const p90 = s.p90 ?? null;
            const p10 = s.p10 ?? null;
            return [s.date, p90 != null && p10 != null ? p90 - p10 : null] as [string, number | null];
        });
        const ndviData = stats.map((s) => {
            const d = s.date;
            const v = s.mean ?? null;
            const m = Number(String(d).slice(5, 7));
            const inSeason = !seasonMonths?.length || seasonMonths.includes(m);
            const inPeak = !!peakMonths?.length && peakMonths.includes(m);
            const bare = !isSar && inPeak && typeof v === "number" && v < bareThreshold;
            let color = indexLineColor(indexType);
            let opacity = 1;
            let symbolSize = isSar && stats.length <= 3 ? 8 : 4;
            if (d === selectedDate) {
                color = tokenColor("--danger");
                symbolSize = 10;
            } else if (s.may_be_unreliable) {
                color = tokenColor("--warning");
                opacity = 0.85;
                symbolSize = 8;
            } else if (bare) {
                color = tokenColor("--muted-foreground");
                opacity = 0.6;
                symbolSize = 7;
            } else if (!inSeason && !isSar) {
                color = tokenColor("--muted-foreground");
                opacity = 0.45;
            }
            return {
                value: [d, v] as [string, number | null],
                itemStyle: { color, opacity },
                symbol: s.may_be_unreliable ? "diamond" : "circle",
                symbolSize,
                cloudCover: s.cloud_cover ?? null,
                decloudQuality: s.decloud_quality ?? null,
                decloudReasons: s.decloud_reasons ?? [],
                productSource: s.product_source ?? null,
                sceneId: s.scene_id ?? null,
                mayBeUnreliable: Boolean(s.may_be_unreliable),
            };
        });
        const altName = t("decloudAltSeries");
        const altData = (decloudAltStats ?? []).map((s) => {
            const unreliable = s.may_be_unreliable || s.decloud_quality !== "good";
            return {
                value: [s.date, s.mean ?? null] as [string, number | null],
                itemStyle: {
                    color: tokenColor(unreliable ? "--warning" : "--caution"),
                    opacity: 0.9,
                },
                symbol: "diamond",
                symbolSize: s.date === selectedDate ? 10 : 7,
                cloudCover: s.cloud_cover ?? null,
                decloudQuality: s.decloud_quality ?? null,
                decloudReasons: s.decloud_reasons ?? [],
                productSource: s.product_source ?? null,
                sceneId: s.scene_id ?? null,
                mayBeUnreliable: Boolean(unreliable),
            };
        });

        // Weather overlay: daily precip bars and ET₀ line on their actual dates
        const hasWeather = !!weatherSorted && weatherSorted.length > 0;
        const precipData = weatherSorted
            ? weatherSorted.map((w) => [w.date, w.precipitation_sum ?? 0] as [string, number])
            : [];
        const et0Data = weatherSorted
            ? weatherSorted.map((w) => [w.date, w.et0_fao_mm ?? 0] as [string, number])
            : [];

        const years = Array.from(
            new Set(stats.map((s) => Number(String(s.date).slice(0, 4))).filter((y) => Number.isFinite(y))),
        ).sort();

        const pad2 = (n: number) => String(n).padStart(2, "0");
        const markAreaData: any[] = [];
        if (stageBands?.length && years.length) {
            const bandColors = ["rgba(216,243,220,0.35)", "rgba(149,213,178,0.28)", "rgba(82,183,136,0.22)", "rgba(244,162,97,0.18)"];
            years.forEach((y) => {
                stageBands.forEach((b, i) => {
                    const sd = b.startDay ?? 1;
                    const ed = b.endDay ?? 28;
                    markAreaData.push([
                        {
                            name: years.length <= 2 ? b.name : undefined,
                            xAxis: `${y}-${pad2(b.startMonth)}-${pad2(sd)}`,
                            itemStyle: { color: b.color || bandColors[i % bandColors.length] },
                        },
                        { xAxis: `${y}-${pad2(b.endMonth)}-${pad2(ed)}` },
                    ]);
                });
            });
        } else if (seasonMonths?.length && years.length) {
            years.forEach((y) => {
                const sm = Math.min(...seasonMonths);
                const em = Math.max(...seasonMonths);
                markAreaData.push([
                    {
                        name: years.length <= 2 ? "生育期" : undefined,
                        xAxis: `${y}-${pad2(sm)}-01`,
                        itemStyle: { color: "rgba(216,243,220,0.32)" },
                    },
                    { xAxis: `${y}-${pad2(em)}-28` },
                ]);
                if (peakMonths?.length) {
                    const ps = Math.min(...peakMonths);
                    const pe = Math.max(...peakMonths);
                    markAreaData.push([
                        {
                            name: years.length <= 2 ? "旺长期" : undefined,
                            xAxis: `${y}-${pad2(ps)}-01`,
                            itemStyle: { color: "rgba(149,213,178,0.28)" },
                        },
                        { xAxis: `${y}-${pad2(pe)}-28` },
                    ]);
                }
            });
        }
        const markAreaOption = markAreaData.length
            ? { silent: true, label: { show: true, position: "insideTop", fontSize: 10, color: "#3f6212" }, data: markAreaData }
            : undefined;

        const valueByDate = new Map(stats.map((s) => [s.date, s.mean]));
        const markPointOption =
            eventMarks && eventMarks.length
                ? {
                      silent: true,
                      symbol: "pin",
                      symbolSize: 28,
                      label: { show: false },
                      data: eventMarks.map((m) => {
                          const v =
                              m.value != null && Number.isFinite(m.value)
                                  ? m.value
                                  : (valueByDate.get(m.date) ?? 0);
                          const color =
                              m.level === "high"
                                  ? tokenColor("--sev-high")
                                  : m.level === "medium"
                                    ? tokenColor("--sev-medium")
                                    : tokenColor("--sev-low");
                          return {
                              name: m.label,
                              coord: [m.date, v],
                              itemStyle: { color },
                          };
                      }),
                  }
                : undefined;

        const hasAlt = altData.length > 0;
        const showLegend = hasWeather || hasAlt;
        const legendData = [
            seriesName,
            ...(hasAlt ? [altName] : []),
            ...(hasWeather ? ["Precip (mm)", "ET₀ (mm)"] : []),
        ];
        return {
            grid: { top: showLegend ? 36 : 18, right: hasWeather ? 50 : 10, bottom: 40, left: 40 },
            legend: showLegend
                ? {
                    data: legendData,
                    top: 0,
                    ...legendStyle(),
                }
                : undefined,
            tooltip: {
                ...baseTooltip(),
                trigger: "axis" as const,
                axisPointer: { type: "cross" as const },
                formatter: (params: any) => {
                    if (!Array.isArray(params) || params.length === 0) return "";
                    const date = params[0]?.axisValueLabel || params[0]?.value?.[0] || "";
                    const lines = [`<b>${date}</b>`];
                    let extras: {
                        cloudCover?: number | null;
                        decloudQuality?: string | null;
                        decloudReasons?: string[];
                        productSource?: string | null;
                        sceneId?: string | null;
                    } | null = null;
                    const extraLines: string[] = [];
                    for (const p of params) {
                        if (p.seriesName === "p10" || p.seriesName === "p90") continue;
                        const val = Array.isArray(p.value) ? p.value[1] : p.value;
                        if (val == null) continue;
                        const isIndex = p.seriesName === seriesName || p.seriesName === altName;
                        const unit = isIndex ? (isSar ? " dB" : "") : " mm";
                        const digits = isIndex ? (isSar ? 2 : 3) : 1;
                        lines.push(`${p.marker} ${p.seriesName}: ${Number(val).toFixed(digits)}${unit}`);
                        if (isIndex && p.data && typeof p.data === "object") {
                            if (p.seriesName === seriesName) extras = p.data;
                            extraLines.push(
                                formatDecloudLine(
                                    {
                                        quality: p.data.decloudQuality,
                                        reasons: p.data.decloudReasons,
                                        source: p.data.productSource,
                                        sceneId: p.data.sceneId,
                                        cloudCover: p.data.cloudCover,
                                    },
                                    t,
                                ),
                            );
                        }
                    }
                    if (!isSar && (hasOpticalTooltip(extras) || extraLines.length)) {
                        lines.push(formatCloudLine(extras?.cloudCover, t));
                        // Prefer real decloud status over "无（多云原始）" when an alt
                        // (fair/bad) product is also on this axis date.
                        const unique = [...new Set(extraLines.filter(Boolean))];
                        const hasRealDecloud = unique.some(
                            (line) => line !== t("decloudNone") && line !== t("decloudClear") && line !== t("decloudNA"),
                        );
                        for (const line of unique) {
                            if (hasRealDecloud && line === t("decloudNone")) continue;
                            lines.push(line);
                        }
                    }
                    return lines.join("<br/>");
                },
            },
            xAxis: {
                type: "time" as const,
                axisLabel: axisLabel(),
                axisTick: { alignWithLabel: true },
            },
            yAxis: hasWeather
                ? [
                    valueAxis({ min: yMin, max: yMax }),
                    secondaryValueAxis({ name: "mm", nameTextStyle: axisLabel() }),
                ]
                : valueAxis({ min: yMin, max: yMax }),
            dataZoom: [
                {
                    type: "inside" as const,
                    start: 0,
                    end: 100,
                },
            ],
            series: [
                // p10 band (invisible base)
                {
                    name: "p10",
                    type: "line",
                    data: bandLowData,
                    stack: "band",
                    lineStyle: { opacity: 0 },
                    symbol: "none",
                    areaStyle: { opacity: 0 },
                    emphasis: { disabled: true },
                },
                // p90 band (visible range area)
                {
                    name: "p90",
                    type: "line",
                    data: bandHighData,
                    stack: "band",
                    lineStyle: { opacity: 0 },
                    symbol: "none",
                    areaStyle: {
                        color: indexBandColor(indexType),
                    },
                    emphasis: { disabled: true },
                },
                // Mean line (dim off-season / bare peak points)
                {
                    name: seriesName,
                    type: "line",
                    data: ndviData,
                    smooth: true,
                    lineStyle: { color: indexLineColor(indexType), width: 2 },
                    showSymbol: true,
                    markLine: {
                        silent: true,
                        data: [thresholdMarkLine(config.threshold, "Threshold")],
                    },
                    markArea: markAreaOption,
                    markPoint: markPointOption,
                },
                ...(hasAlt
                    ? [
                        {
                            name: altName,
                            type: "scatter",
                            data: altData,
                            z: 5,
                            tooltip: { trigger: "item" as const },
                        },
                    ]
                    : []),
                // Weather overlay: daily precipitation bars on actual dates
                ...(hasWeather
                    ? [
                        {
                            name: "Precip (mm)",
                            type: "bar",
                            yAxisIndex: 1,
                            data: precipData,
                            barMaxWidth: 6,
                            itemStyle: { color: sig("precip", 0.45) },
                            emphasis: { itemStyle: { color: sig("precip", 0.8) } },
                        },
                        {
                            name: "ET₀ (mm)",
                            type: "line",
                            yAxisIndex: 1,
                            data: et0Data,
                            smooth: true,
                            symbol: "none",
                            lineStyle: { color: sig("et0"), width: 1.5, type: "dashed" as const },
                            itemStyle: { color: sig("et0") },
                        },
                    ]
                    : []),
            ],
        };
    }, [stats, decloudAltStats, selectedDate, config, seriesName, weatherSorted, indexType, yMin, yMax, isSar, seasonMonths, peakMonths, stageBands, bareThreshold, eventMarks, t]);

    const onEvents = useMemo(
        () => ({
            click: (params: any) => {
                const ok =
                    params.seriesName === seriesName ||
                    params.seriesName === t("decloudAltSeries");
                if (ok && params.value != null) {
                    const raw = params.value;
                    const date = Array.isArray(raw) ? raw[0] : (raw?.value ? raw.value[0] : null);
                    if (date) onDateSelect?.(String(date));
                }
            },
        }),
        [onDateSelect, seriesName, t],
    );

    if (stats.length === 0) {
        return (
            <div
                className="flex items-center justify-center text-xs text-muted-foreground"
                style={{ height }}
            >
                No {config.label} data yet.
            </div>
        );
    }

    const lineColor = indexLineColor(indexType);
    const grayColor = tokenColor("--muted-foreground");
    const yellowColor = tokenColor("--warning");
    const redColor = tokenColor("--danger");

    return (
        <div className="w-full space-y-1.5">
            <ReactEChartsCore
                echarts={echarts}
                option={option}
                style={{ height, width: "100%" }}
                onEvents={onEvents}
                notMerge
                lazyUpdate
            />
            {!isSar ? (
                <div className="px-1 space-y-1">
                    <p className="text-[10px] font-medium text-muted-foreground">
                        {t("pointLegendTitle")}
                    </p>
                    <ul className="grid gap-1 text-[10px] text-muted-foreground leading-snug sm:grid-cols-2">
                        <li className="flex items-start gap-1.5">
                            <span
                                className="mt-0.5 h-2.5 w-2.5 shrink-0 rounded-full"
                                style={{ backgroundColor: lineColor }}
                                aria-hidden
                            />
                            <span>{t("pointLegendGreen")}</span>
                        </li>
                        <li className="flex items-start gap-1.5">
                            <span
                                className="mt-0.5 h-2.5 w-2.5 shrink-0 rounded-full opacity-60"
                                style={{ backgroundColor: grayColor }}
                                aria-hidden
                            />
                            <span>{t("pointLegendGray")}</span>
                        </li>
                        <li className="flex items-start gap-1.5">
                            <span
                                className="mt-0.5 h-2.5 w-2.5 shrink-0 rotate-45"
                                style={{ backgroundColor: yellowColor }}
                                aria-hidden
                            />
                            <span>{t("pointLegendYellow")}</span>
                        </li>
                        <li className="flex items-start gap-1.5">
                            <span
                                className="mt-0.5 h-2.5 w-2.5 shrink-0 rounded-full"
                                style={{ backgroundColor: redColor }}
                                aria-hidden
                            />
                            <span>{t("pointLegendSelected")}</span>
                        </li>
                    </ul>
                </div>
            ) : null}
        </div>
    );
}

function hasOpticalTooltip(extras: {
    cloudCover?: number | null;
    decloudQuality?: string | null;
    productSource?: string | null;
    sceneId?: string | null;
} | null): boolean {
    if (!extras) return false;
    if (typeof extras.cloudCover === "number" && Number.isFinite(extras.cloudCover)) return true;
    return Boolean(extras.decloudQuality || extras.productSource || extras.sceneId);
}

const DECLOUD_REASON_KEYS = new Set([
    "rgb_still_bright",
    "rgb_bright",
    "tiny_rgb_delta",
    "spatial_std_collapse",
    "spatial_std_low",
    "ndvi_far_below_neighbors",
    "ndvi_below_neighbors",
    "non_finite_reconstruction",
]);

export function formatCloudLine(
    cloudCover: number | null | undefined,
    t: (key: string, values?: Record<string, string | number>) => string,
): string {
    if (typeof cloudCover === "number" && Number.isFinite(cloudCover)) {
        return t("cloud", { percent: Math.round(cloudCover) });
    }
    return t("cloudNA");
}

export function formatDecloudLine(
    info: {
        quality?: string | null;
        reasons?: string[] | null;
        source?: string | null;
        sceneId?: string | null;
        cloudCover?: number | null;
    },
    t: (key: string, values?: Record<string, string | number>) => string,
): string {
    const isDecloud =
        info.source === DECLOUD_SOURCE ||
        (typeof info.sceneId === "string" && info.sceneId.endsWith("_decloud"));
    const reasonText = (info.reasons ?? [])
        .map((r) => (DECLOUD_REASON_KEYS.has(r) ? t(`reason_${r}`) : r))
        .filter(Boolean)
        .join("; ");
    if (isDecloud) {
        const q = (info.quality || "").toLowerCase();
        if (q === "good") return t("decloudGood");
        if (q === "fair") return t("decloudFair", { reason: reasonText || t("reasonUnknown") });
        if (q === "bad") return t("decloudBad", { reason: reasonText || t("reasonUnknown") });
        return t("decloudUnreliable", { reason: reasonText || t("reasonUnknown") });
    }
    if (typeof info.cloudCover === "number" && Number.isFinite(info.cloudCover) && info.cloudCover > 30) {
        return t("decloudNone");
    }
    if (typeof info.cloudCover === "number" && Number.isFinite(info.cloudCover)) {
        return t("decloudClear");
    }
    return t("decloudNA");
}

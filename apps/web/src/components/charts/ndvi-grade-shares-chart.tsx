"use client";

import React, { useMemo } from "react";
import ReactEChartsCore from "echarts-for-react/lib/core";
import * as echarts from "echarts/core";
import { PieChart, BarChart, LineChart } from "echarts/charts";
import {
    GridComponent,
    TooltipComponent,
    LegendComponent,
    TitleComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { axisLabel, baseTooltip, legendStyle, secondaryValueAxis, valueAxis } from "./chart-base";

echarts.use([
    PieChart,
    BarChart,
    LineChart,
    GridComponent,
    TooltipComponent,
    LegendComponent,
    TitleComponent,
    CanvasRenderer,
]);

/** Day/area 长势等级 — 图一 bands (not 优≥0.7). */
export type NdviDayGrade = "红" | "橙" | "黄" | "绿";

export const NDVI_DAY_GRADE_ORDER: NdviDayGrade[] = ["红", "橙", "黄", "绿"];

export const NDVI_DAY_GRADE_COLORS: Record<NdviDayGrade, string> = {
    红: "#e53935",
    橙: "#fb8c00",
    黄: "#fdd835",
    绿: "#43a047",
};

export const NDVI_DAY_GRADE_RULE_ZH = "红<0.25 / 橙0.25–0.35 / 黄0.35–0.50 / 绿≥0.50";

export type DayGradeShare = {
    counts: Record<NdviDayGrade, number>;
    pct: Record<NdviDayGrade, number>;
    n: number;
    mean: number | null;
};

export function classifyNdviDayGrade(v: number): NdviDayGrade {
    if (v >= 0.5) return "绿";
    if (v >= 0.35) return "黄";
    if (v >= 0.25) return "橙";
    return "红";
}

/** Prefer clear pixels when any clear=1 exists (matches 色斑 rasterize). */
export function computePixelNdviGradeShares(
    pixels: Array<{ clear?: number; NDVI?: number; ndvi?: number; [k: string]: unknown }>,
): DayGradeShare | null {
    if (!pixels?.length) return null;
    const anyClear = pixels.some((p) => Number(p.clear) === 1);
    const counts: Record<NdviDayGrade, number> = { 红: 0, 橙: 0, 黄: 0, 绿: 0 };
    let sum = 0;
    let n = 0;
    for (const p of pixels) {
        if (anyClear && Number(p.clear) === 0) continue;
        const raw = p.NDVI ?? p.ndvi;
        const v = typeof raw === "number" ? raw : Number(raw);
        if (!Number.isFinite(v)) continue;
        counts[classifyNdviDayGrade(v)] += 1;
        sum += v;
        n += 1;
    }
    if (!n) return null;
    const pct = Object.fromEntries(
        NDVI_DAY_GRADE_ORDER.map((k) => [k, Math.round((counts[k] * 1000) / n) / 10]),
    ) as Record<NdviDayGrade, number>;
    return { counts, pct, n, mean: sum / n };
}

type ChartVariant = "donut" | "stacked";

interface NdviGradeSharesChartProps {
    /** Render only one chart — parent supplies card chrome (no inner borders). */
    variant: ChartVariant;
    /** Selected-day shares for the donut */
    selectedShare?: DayGradeShare | null;
    /** History map date → shares (fills as heatmaps/prefetch load) */
    historyByDate?: Record<string, DayGradeShare>;
    /** Optional mean NDVI override per date (scene ndvi_avg) when share.mean missing */
    meanByDate?: Record<string, number | null | undefined>;
    /** Field area in 亩 for donut center */
    areaMu?: number | null;
    selectedDate?: string | null;
    height?: number;
}

export default function NdviGradeSharesChart({
    variant,
    selectedShare = null,
    historyByDate = {},
    meanByDate,
    areaMu = null,
    selectedDate,
    height,
}: NdviGradeSharesChartProps) {
    const historyDates = useMemo(
        () => Object.keys(historyByDate).sort((a, b) => a.localeCompare(b)),
        [historyByDate],
    );
    const showStacked = historyDates.length >= 2;

    const donutOption = useMemo(() => {
        const share = selectedShare;
        const data = NDVI_DAY_GRADE_ORDER.map((g) => ({
            name: g,
            value: share ? share.pct[g] : 0,
            itemStyle: { color: NDVI_DAY_GRADE_COLORS[g] },
        }));
        const centerTop =
            areaMu != null && Number.isFinite(areaMu)
                ? `${areaMu.toFixed(areaMu >= 100 ? 0 : 1)}亩`
                : "—";
        return {
            animation: false,
            tooltip: {
                ...baseTooltip(),
                trigger: "item" as const,
                formatter: (p: { name?: string; value?: number; percent?: number }) => {
                    const g = (p.name || "") as NdviDayGrade;
                    const cnt = share?.counts[g] ?? 0;
                    return `${g} ${p.value ?? 0}%（${cnt} 像元）`;
                },
            },
            legend: {
                orient: "vertical" as const,
                right: 4,
                top: "middle",
                ...legendStyle(),
                data: NDVI_DAY_GRADE_ORDER,
            },
            title: [
                {
                    text: selectedDate ? selectedDate : "当日",
                    left: 8,
                    top: 4,
                    textStyle: { fontSize: 11, fontWeight: 600, color: "#374151" },
                },
                {
                    text: centerTop,
                    subtext: "总面积",
                    left: "34%",
                    top: "42%",
                    textAlign: "center",
                    textStyle: { fontSize: 14, fontWeight: 700, color: "#111827", lineHeight: 18 },
                    subtextStyle: { fontSize: 10, color: "#6b7280", lineHeight: 14 },
                },
            ],
            series: [
                {
                    name: "长势等级占比",
                    type: "pie",
                    radius: ["46%", "70%"],
                    center: ["38%", "54%"],
                    avoidLabelOverlap: true,
                    label: { show: false },
                    labelLine: { show: false },
                    data,
                },
            ],
        };
    }, [selectedShare, areaMu, selectedDate]);

    const stackedOption = useMemo(() => {
        const dates = historyDates;
        const series = NDVI_DAY_GRADE_ORDER.map((g) => ({
            name: g,
            type: "bar" as const,
            stack: "grade",
            barMaxWidth: 36,
            barCategoryGap: "28%",
            itemStyle: { color: NDVI_DAY_GRADE_COLORS[g] },
            emphasis: { focus: "series" as const },
            data: dates.map((d) => historyByDate[d]?.pct[g] ?? 0),
        }));
        const means = dates.map((d) => {
            const fromShare = historyByDate[d]?.mean;
            if (fromShare != null && Number.isFinite(fromShare)) return +fromShare.toFixed(3);
            const alt = meanByDate?.[d];
            return alt != null && Number.isFinite(alt) ? +Number(alt).toFixed(3) : null;
        });
        return {
            animation: false,
            grid: { top: 36, right: 52, bottom: 36, left: 44 },
            legend: {
                data: [...NDVI_DAY_GRADE_ORDER, "平均NDVI"],
                top: 0,
                ...legendStyle(),
            },
            tooltip: {
                ...baseTooltip(),
                trigger: "axis" as const,
                axisPointer: { type: "shadow" as const },
                formatter: (params: Array<{ seriesName?: string; value?: number; axisValue?: string; marker?: string }>) => {
                    if (!params?.length) return "";
                    const date = String(params[0]?.axisValue ?? "");
                    const lines = [`<b>${date}</b>`];
                    for (const p of params) {
                        if (p.value == null) continue;
                        const unit = p.seriesName === "平均NDVI" ? "" : "%";
                        lines.push(`${p.marker ?? ""}${p.seriesName}: ${p.value}${unit}`);
                    }
                    const n = historyByDate[date]?.n;
                    if (n != null) lines.push(`像元 n=${n}`);
                    return lines.join("<br/>");
                },
            },
            xAxis: {
                type: "category" as const,
                data: dates.map((d) => d.slice(5)),
                axisLabel: axisLabel({ rotate: dates.length > 6 ? 30 : 0 }),
                axisTick: { alignWithLabel: true },
            },
            yAxis: [
                valueAxis({
                    name: "%",
                    min: 0,
                    max: 100,
                    nameTextStyle: axisLabel(),
                }),
                secondaryValueAxis({
                    name: "NDVI",
                    min: 0,
                    max: 1,
                    nameTextStyle: axisLabel(),
                }),
            ],
            series: [
                ...series,
                {
                    name: "平均NDVI",
                    type: "line",
                    yAxisIndex: 1,
                    data: means,
                    smooth: true,
                    symbol: "circle",
                    symbolSize: 6,
                    lineStyle: { width: 2, color: "#1e88e5" },
                    itemStyle: { color: "#1e88e5" },
                    z: 10,
                },
            ],
        };
    }, [historyDates, historyByDate, meanByDate]);

    if (variant === "donut") {
        const h = height ?? 200;
        if (!selectedShare) {
            return (
                <div
                    className="flex items-center justify-center text-[11px] text-muted-foreground px-1"
                    style={{ height: h }}
                >
                    当日暂无像素分档（可点选其他日期或等待色斑加载）
                </div>
            );
        }
        return (
            <ReactEChartsCore
                echarts={echarts}
                option={donutOption}
                style={{ height: h, width: "100%" }}
                notMerge
                lazyUpdate
            />
        );
    }

    // stacked
    const stackedHeight = Math.max(height ?? 250, 250);
    if (!showStacked) {
        return (
            <div
                className="flex items-center justify-center text-[11px] text-muted-foreground px-1 text-center"
                style={{ height: Math.min(stackedHeight, 120) }}
            >
                已有 {historyDates.length} 日分档；再加载 ≥1 日后显示占比趋势
            </div>
        );
    }
    return (
        <ReactEChartsCore
            echarts={echarts}
            option={stackedOption}
            style={{ height: stackedHeight, width: "100%" }}
            notMerge
            lazyUpdate
        />
    );
}

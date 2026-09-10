"use client";

/**
 * Six-dimension land-assessment radar (hexagon). Scores come from the
 * field's latest succeeded report scorecard; this component does not
 * invent values.
 */

import React, { useMemo } from "react";
import ReactEChartsCore from "echarts-for-react/lib/core";
import * as echarts from "echarts/core";
import { RadarChart } from "echarts/charts";
import {
    RadarComponent,
    TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useTranslations } from "next-intl";
import { tokenColor } from "@/lib/design-tokens";
import { axisLabel, baseTooltip, viz } from "./chart-base";
import type { AssessmentDimensionKey, AssessmentScorecard } from "@/lib/api";

echarts.use([RadarChart, RadarComponent, TooltipComponent, CanvasRenderer]);

const AXIS_I18N: Record<AssessmentDimensionKey, string> = {
    crop: "axisCrop",
    soil: "axisSoil",
    vigor: "axisVigor",
    weather: "axisWeather",
    wet_safety: "axisWetSafety",
    drought_safety: "axisDroughtSafety",
};

interface LandScorecardRadarProps {
    scorecard: AssessmentScorecard;
    height?: number;
}

export default function LandScorecardRadar({
    scorecard,
    height = 280,
}: LandScorecardRadarProps) {
    const t = useTranslations("landReportTab");

    const option = useMemo(() => {
        const reduceMotion =
            typeof window !== "undefined" &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const seriesColor = viz(1);
        const indicators = scorecard.dimensions.map((d) => ({
            name: t(AXIS_I18N[d.key]),
            max: 100,
        }));
        const values = scorecard.dimensions.map((d) => d.score);

        return {
            animation: !reduceMotion,
            tooltip: {
                ...baseTooltip(),
                trigger: "item" as const,
                formatter: (params: { data?: { value?: number[] } }) => {
                    const vals = params?.data?.value;
                    if (!vals) return "";
                    return scorecard.dimensions
                        .map((d, i) => {
                            const weight = d.weight ? ` · ${d.weight}` : "";
                            return `${t(AXIS_I18N[d.key])}: <span class="font-mono">${vals[i]}</span>${weight}`;
                        })
                        .join("<br/>");
                },
            },
            radar: {
                indicator: indicators,
                shape: "polygon" as const,
                splitNumber: 4,
                radius: "62%",
                axisName: {
                    ...axisLabel(),
                    color: tokenColor("--muted-foreground"),
                },
                axisLine: {
                    lineStyle: { color: tokenColor("--border") },
                },
                splitLine: {
                    lineStyle: {
                        type: "dashed" as const,
                        color: tokenColor("--border"),
                        width: 1,
                    },
                },
                splitArea: {
                    areaStyle: {
                        color: [
                            tokenColor("--surface-2", 0.35),
                            tokenColor("--background", 0.15),
                        ],
                    },
                },
            },
            series: [
                {
                    type: "radar" as const,
                    symbol: "circle",
                    symbolSize: 4,
                    lineStyle: { width: 2, color: seriesColor },
                    itemStyle: { color: seriesColor },
                    areaStyle: { color: viz(1, 0.28) },
                    data: [{ value: values, name: t("chartSeries") }],
                },
            ],
        };
    }, [scorecard, t]);

    return (
        <ReactEChartsCore
            echarts={echarts}
            option={option}
            style={{ height, width: "100%" }}
            notMerge
            lazyUpdate
        />
    );
}

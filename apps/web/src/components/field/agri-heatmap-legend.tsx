"use client";

import React, { useEffect, useMemo, useState } from "react";
import type { AgriHeatmapImage } from "@/lib/agri-heatmap";
import { AGRI_MODE_LABELS } from "@/lib/agri-heatmap";
import { MAP_CHROME } from "@/lib/design-tokens";
import { cn } from "@/lib/utils";

interface AgriHeatmapLegendProps {
    heatmap: AgriHeatmapImage;
    compact?: boolean;
}

function OssPreviewStack({
    rgbUrl,
    heatmapUrl,
    compact,
}: {
    rgbUrl: string | null;
    heatmapUrl: string | null;
    compact: boolean;
}) {
    const [rgbFailed, setRgbFailed] = useState(false);
    const [hmFailed, setHmFailed] = useState(false);

    useEffect(() => {
        setRgbFailed(false);
        setHmFailed(false);
    }, [rgbUrl, heatmapUrl]);

    const showRgb = Boolean(rgbUrl) && !rgbFailed;
    const showHm = Boolean(heatmapUrl) && !hmFailed;
    if (!showRgb && !showHm) return null;

    const caption =
        showRgb && showHm ? "真彩+色斑" : showRgb ? "真彩" : "色斑";

    return (
        <div className={cn(compact ? "mt-1" : "mt-1.5")}>
            <div
                className={cn(
                    "relative w-full overflow-hidden rounded-md border border-border bg-muted/60",
                    compact ? "h-[72px]" : "h-[110px]",
                )}
            >
                {showRgb && (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                        src={rgbUrl!}
                        alt="真彩预览"
                        loading="lazy"
                        className="absolute inset-0 h-full w-full object-contain"
                        onError={() => setRgbFailed(true)}
                    />
                )}
                {showHm && (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                        src={heatmapUrl!}
                        alt="色斑预览"
                        loading="lazy"
                        className={cn(
                            "absolute inset-0 h-full w-full object-contain",
                            showRgb ? "opacity-60" : "opacity-100",
                        )}
                        onError={() => setHmFailed(true)}
                    />
                )}
            </div>
            <p className="mt-0.5 text-[9px] leading-none text-muted-foreground">{caption}</p>
        </div>
    );
}

/** Map overlay legend + mean badge for agri pixel_data 色斑图 (figure-3 style). */
export default function AgriHeatmapLegend({ heatmap, compact = false }: AgriHeatmapLegendProps) {
    const legend = heatmap.legend;
    const meanText =
        heatmap.mean != null && Number.isFinite(heatmap.mean)
            ? heatmap.mean.toFixed(2)
            : null;

    const { rgbUrl, heatmapUrl } = useMemo(() => {
        const rgb =
            (heatmap.previewRgbUrl && heatmap.previewRgbUrl.trim()) ||
            (heatmap.previewLargeRgbUrl && heatmap.previewLargeRgbUrl.trim()) ||
            null;
        const hm =
            (heatmap.previewHeatmapUrl && heatmap.previewHeatmapUrl.trim()) ||
            (heatmap.previewS2HeatmapUrl && heatmap.previewS2HeatmapUrl.trim()) ||
            null;
        return { rgbUrl: rgb, heatmapUrl: hm };
    }, [
        heatmap.previewRgbUrl,
        heatmap.previewLargeRgbUrl,
        heatmap.previewHeatmapUrl,
        heatmap.previewS2HeatmapUrl,
    ]);

    const hasPreview = Boolean(rgbUrl || heatmapUrl);

    return (
        <div
            className={cn(
                MAP_CHROME,
                "rounded-lg",
                compact ? "px-2 py-1.5 w-[150px]" : "px-3 py-2.5 w-[200px]",
            )}
        >
            <div className="flex items-center justify-between gap-2 mb-1.5">
                <p className="font-semibold tracking-wide text-[11px]">{legend.label}</p>
                {meanText != null && (
                    <span className="rounded bg-primary/15 px-1.5 py-0.5 font-mono text-[10px] font-medium text-primary tabular-nums">
                        均≈{meanText}
                    </span>
                )}
            </div>

            {legend.kind === "continuous" ? (
                <>
                    <div
                        className={cn(
                            "w-full rounded-sm border border-border overflow-hidden",
                            compact ? "h-2" : "h-3",
                        )}
                        style={{ background: legend.gradient }}
                    />
                    <div className="flex justify-between mt-1">
                        <span className="text-muted-foreground font-mono text-[11px]">{legend.min}</span>
                        <span className="text-muted-foreground font-mono text-[11px]">{legend.max}</span>
                    </div>
                    {heatmap.min != null && heatmap.max != null && (
                        <p className="text-muted-foreground leading-tight text-[11px] mt-1">
                            地块范围:{" "}
                            <span className="font-mono font-medium text-foreground/80">
                                {heatmap.min.toFixed(2)}
                            </span>
                            {" – "}
                            <span className="font-mono font-medium text-foreground/80">
                                {heatmap.max.toFixed(2)}
                            </span>
                        </p>
                    )}
                </>
            ) : (
                <>
                    <ul className="space-y-1">
                        {legend.classes.map((c) => (
                            <li key={c.key} className="flex items-center gap-1.5 text-[11px]">
                                <span
                                    className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm border border-border/60"
                                    style={{ background: c.color }}
                                />
                                <span className="text-foreground/90">{c.label}</span>
                            </li>
                        ))}
                    </ul>
                    {legend.hint && (
                        <p className="mt-1.5 text-[10px] leading-snug text-muted-foreground">{legend.hint}</p>
                    )}
                </>
            )}

            {hasPreview && (
                <OssPreviewStack rgbUrl={rgbUrl} heatmapUrl={heatmapUrl} compact={compact} />
            )}

            <p className="mt-1.5 text-[10px] text-muted-foreground tabular-nums">
                {AGRI_MODE_LABELS[heatmap.index]} · {heatmap.pixelCount} 像素
            </p>
        </div>
    );
}

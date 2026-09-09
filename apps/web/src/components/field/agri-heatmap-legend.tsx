"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Maximize2 } from "lucide-react";
import type { AgriHeatmapImage } from "@/lib/agri-heatmap";
import { AGRI_MODE_LABELS } from "@/lib/agri-heatmap";
import { MAP_CHROME } from "@/lib/design-tokens";
import { cn } from "@/lib/utils";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";

interface AgriHeatmapLegendProps {
    heatmap: AgriHeatmapImage;
    compact?: boolean;
}

function PreviewImg({
    src,
    alt,
    className,
    onFailed,
}: {
    src: string;
    alt: string;
    className?: string;
    onFailed: () => void;
}) {
    return (
        // eslint-disable-next-line @next/next/no-img-element
        <img
            src={src}
            alt={alt}
            loading="lazy"
            className={cn("h-full w-full object-contain", className)}
            onError={onFailed}
        />
    );
}

function PreviewFrame({
    src,
    alt,
    caption,
    compact,
    overlaySrc,
    onFailed,
    onOverlayFailed,
}: {
    src: string;
    alt: string;
    caption: string;
    compact: boolean;
    overlaySrc?: string | null;
    onFailed: () => void;
    onOverlayFailed?: () => void;
}) {
    const [open, setOpen] = useState(false);

    const frame = cn(
        "relative w-full overflow-hidden rounded-md border border-border bg-muted/60",
        compact ? "h-[80px]" : "h-[120px]",
    );

    return (
        <div>
            <div className={frame}>
                <button
                    type="button"
                    className="absolute inset-0 z-0 cursor-zoom-in"
                    onClick={() => setOpen(true)}
                    aria-label={`放大查看${caption}`}
                >
                    <PreviewImg
                        src={src}
                        alt={alt}
                        className="absolute inset-0 pointer-events-none"
                        onFailed={onFailed}
                    />
                    {overlaySrc ? (
                        <PreviewImg
                            src={overlaySrc}
                            alt={`${alt}叠加`}
                            className="absolute inset-0 opacity-60 pointer-events-none"
                            onFailed={onOverlayFailed ?? (() => undefined)}
                        />
                    ) : null}
                </button>
                <button
                    type="button"
                    onClick={(e) => {
                        e.stopPropagation();
                        setOpen(true);
                    }}
                    className="absolute right-1 top-1 z-10 inline-flex h-6 w-6 items-center justify-center rounded-md border border-border/80 bg-background/90 text-foreground shadow-sm hover:bg-background"
                    title="放大"
                    aria-label={`放大${caption}`}
                >
                    <Maximize2 className="h-3.5 w-3.5" />
                </button>
            </div>
            <p className="mt-0.5 text-[9px] leading-none text-muted-foreground">{caption}</p>

            <Dialog open={open} onOpenChange={setOpen}>
                <DialogContent className="max-w-[min(92vw,56rem)] p-3 sm:p-4">
                    <DialogHeader className="space-y-1 pr-8">
                        <DialogTitle className="text-sm font-medium">{caption}</DialogTitle>
                    </DialogHeader>
                    <div className="relative max-h-[min(78vh,40rem)] overflow-auto rounded-md bg-muted/40">
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                            src={src}
                            alt={alt}
                            className="mx-auto max-h-[min(78vh,40rem)] w-auto max-w-full object-contain"
                        />
                        {overlaySrc ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                                src={overlaySrc}
                                alt={`${alt}叠加`}
                                className="pointer-events-none absolute inset-0 mx-auto max-h-[min(78vh,40rem)] w-auto max-w-full object-contain opacity-55"
                            />
                        ) : null}
                    </div>
                </DialogContent>
            </Dialog>
        </div>
    );
}

/**
 * OSS preview: prefer large_rgb (tile true-color). Parcel field_rgb is often a
 * near-black crop with only a red outline — looks like "no 真彩". Heatmap is
 * same extent as parcel rgb; do not overlay it on large tile (misaligned).
 * Previews stay expanded by default; each frame has a click-to-enlarge control.
 */
function OssPreviewStack({
    parcelRgbUrl,
    largeRgbUrl,
    heatmapUrl,
    compact,
}: {
    parcelRgbUrl: string | null;
    largeRgbUrl: string | null;
    heatmapUrl: string | null;
    compact: boolean;
}) {
    const [largeFailed, setLargeFailed] = useState(false);
    const [parcelFailed, setParcelFailed] = useState(false);
    const [hmFailed, setHmFailed] = useState(false);

    useEffect(() => {
        setLargeFailed(false);
        setParcelFailed(false);
        setHmFailed(false);
    }, [parcelRgbUrl, largeRgbUrl, heatmapUrl]);

    const showLarge = Boolean(largeRgbUrl) && !largeFailed;
    const showParcel = Boolean(parcelRgbUrl) && !parcelFailed;
    const showHm = Boolean(heatmapUrl) && !hmFailed;

    const trueColorUrl = showLarge ? largeRgbUrl : showParcel ? parcelRgbUrl : null;
    const trueColorIsLarge = showLarge;
    const overlayHm = Boolean(trueColorUrl && !trueColorIsLarge && showHm);
    const hmAlone = showHm && (trueColorIsLarge || !trueColorUrl);

    if (!trueColorUrl && !showHm) return null;

    return (
        <div className={cn(compact ? "mt-1 space-y-1" : "mt-1.5 space-y-1.5")}>
            {trueColorUrl && (
                <PreviewFrame
                    src={trueColorUrl}
                    alt="真彩预览"
                    caption={
                        overlayHm
                            ? "真彩+色斑"
                            : trueColorIsLarge
                              ? "真彩（瓦片）"
                              : "真彩"
                    }
                    compact={compact}
                    overlaySrc={overlayHm ? heatmapUrl : null}
                    onFailed={() =>
                        trueColorIsLarge ? setLargeFailed(true) : setParcelFailed(true)
                    }
                    onOverlayFailed={() => setHmFailed(true)}
                />
            )}
            {hmAlone && heatmapUrl && (
                <PreviewFrame
                    src={heatmapUrl}
                    alt="色斑预览"
                    caption="色斑"
                    compact={compact}
                    onFailed={() => setHmFailed(true)}
                />
            )}
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

    const { parcelRgbUrl, largeRgbUrl, heatmapUrl } = useMemo(() => {
        const parcel =
            (heatmap.previewRgbUrl && heatmap.previewRgbUrl.trim()) || null;
        const large =
            (heatmap.previewLargeRgbUrl && heatmap.previewLargeRgbUrl.trim()) || null;
        const hm =
            (heatmap.previewHeatmapUrl && heatmap.previewHeatmapUrl.trim()) ||
            (heatmap.previewS2HeatmapUrl && heatmap.previewS2HeatmapUrl.trim()) ||
            null;
        return { parcelRgbUrl: parcel, largeRgbUrl: large, heatmapUrl: hm };
    }, [
        heatmap.previewRgbUrl,
        heatmap.previewLargeRgbUrl,
        heatmap.previewHeatmapUrl,
        heatmap.previewS2HeatmapUrl,
    ]);

    const hasPreview = Boolean(parcelRgbUrl || largeRgbUrl || heatmapUrl);

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

            {/* Always expanded when URLs exist — no collapse toggle */}
            {hasPreview && (
                <OssPreviewStack
                    parcelRgbUrl={parcelRgbUrl}
                    largeRgbUrl={largeRgbUrl}
                    heatmapUrl={heatmapUrl}
                    compact={compact}
                />
            )}

            <p className="mt-1.5 text-[10px] text-muted-foreground tabular-nums">
                {AGRI_MODE_LABELS[heatmap.index]} · {heatmap.pixelCount} 像素
            </p>
        </div>
    );
}

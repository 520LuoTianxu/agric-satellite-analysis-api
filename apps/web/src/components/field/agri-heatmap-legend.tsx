"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

function ZoomableScene({
    src,
    alt,
    overlaySrc,
    active,
}: {
    src: string;
    alt: string;
    overlaySrc?: string | null;
    active: boolean;
}) {
    const viewportRef = useRef<HTMLDivElement>(null);
    const transformRef = useRef({ scale: 1, x: 0, y: 0 });
    const dragRef = useRef<{ x: number; y: number } | null>(null);
    const [, setTick] = useState(0);

    const apply = useCallback((next: { scale: number; x: number; y: number }) => {
        transformRef.current = next;
        setTick((n) => n + 1);
    }, []);

    const reset = useCallback(() => apply({ scale: 1, x: 0, y: 0 }), [apply]);

    useEffect(() => {
        if (!active) reset();
    }, [active, reset]);

    useEffect(() => {
        const el = viewportRef.current;
        if (!el || !active) return;
        const onWheel = (e: WheelEvent) => {
            if (!(e.ctrlKey || e.metaKey)) return;
            e.preventDefault();
            e.stopPropagation();
            const rect = el.getBoundingClientRect();
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;
            const { scale, x, y } = transformRef.current;
            const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
            const nextScale = Math.min(8, Math.max(1, scale * factor));
            if (Math.abs(nextScale - scale) < 0.0001) return;
            if (nextScale <= 1.001) {
                apply({ scale: 1, x: 0, y: 0 });
                return;
            }
            const worldX = (mx - x) / scale;
            const worldY = (my - y) / scale;
            apply({
                scale: nextScale,
                x: mx - worldX * nextScale,
                y: my - worldY * nextScale,
            });
        };
        el.addEventListener("wheel", onWheel, { passive: false });
        return () => el.removeEventListener("wheel", onWheel);
    }, [active, apply]);

    const { scale, x, y } = transformRef.current;

    return (
        <div
            ref={viewportRef}
            className={cn(
                "relative min-h-0 flex-1 overflow-hidden rounded-md bg-muted/40 touch-none",
                scale > 1 ? "cursor-grab" : "cursor-zoom-in",
            )}
            onDoubleClick={reset}
            onPointerDown={(e) => {
                if (transformRef.current.scale <= 1) return;
                if (e.button !== 0) return;
                dragRef.current = { x: e.clientX, y: e.clientY };
                e.currentTarget.setPointerCapture(e.pointerId);
                e.currentTarget.style.cursor = "grabbing";
            }}
            onPointerMove={(e) => {
                const start = dragRef.current;
                if (!start) return;
                const dx = e.clientX - start.x;
                const dy = e.clientY - start.y;
                start.x = e.clientX;
                start.y = e.clientY;
                const t = transformRef.current;
                apply({ scale: t.scale, x: t.x + dx, y: t.y + dy });
            }}
            onPointerUp={(e) => {
                dragRef.current = null;
                e.currentTarget.style.cursor =
                    transformRef.current.scale > 1 ? "grab" : "zoom-in";
            }}
            onPointerCancel={() => {
                dragRef.current = null;
            }}
        >
            <div
                className="relative flex h-full w-full items-center justify-center"
                style={{
                    transform: `translate(${x}px, ${y}px) scale(${scale})`,
                    transformOrigin: "0 0",
                }}
            >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                    src={src}
                    alt={alt}
                    draggable={false}
                    className="max-h-full max-w-full select-none object-contain"
                />
                {overlaySrc ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                        src={overlaySrc}
                        alt={`${alt}叠加`}
                        draggable={false}
                        className="pointer-events-none absolute inset-0 m-auto max-h-full max-w-full select-none object-contain opacity-55"
                    />
                ) : null}
            </div>
            <p className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 rounded-md bg-background/85 px-2 py-0.5 text-[10px] text-muted-foreground shadow-sm">
                Ctrl / ⌘ + 滚轮缩放
                {scale > 1 ? " · 拖动平移 · 双击重置" : ""}
            </p>
        </div>
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
                <DialogContent className="flex h-[min(85vh,56rem)] w-[min(90vw,80rem)] max-w-[90vw] flex-col gap-3 overflow-hidden p-3 sm:p-4">
                    <DialogHeader className="space-y-1 pr-8">
                        <DialogTitle className="text-sm font-medium">{caption}</DialogTitle>
                    </DialogHeader>
                    <ZoomableScene src={src} alt={alt} overlaySrc={overlaySrc} active={open} />
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

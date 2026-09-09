"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useLocale, useTranslations } from "next-intl";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import {
    agriApi,
    cropsApi,
    type CropOption,
    type OverviewChild,
    type OverviewLevel,
    type OverviewStats,
    type OverviewWeakParcel,
} from "@/lib/api";
import { registerPMTilesProtocol } from "@/lib/pmtiles";
import { createTransformRequest, refreshMapToken } from "@/lib/map-auth";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { DROUGHT_CLASS_STYLE, FLOOD_CLASS_STYLE } from "@/lib/agri-heatmap";

/** China approximate bounds [west, south, east, north]. */
const CHINA_BOUNDS: [[number, number], [number, number]] = [
    [73, 18],
    [135, 54],
];

const CHINA_CENTER: [number, number] = [104.5, 35.5];

const DEFAULT_PHENOLOGY_MONTHS = [6, 7, 8, 9];
const WEAK_PAGE_SIZE = 20;

type DrillState = {
    level: OverviewLevel;
    code?: string;
    name?: string;
};

type MapMetric = "drought" | "flood" | "weak_growth" | "parcel_count";

type DatePreset = "30d" | "60d" | "season" | "custom";

function padAdcode(level: OverviewLevel, code: string | null | undefined): string | null {
    if (!code) return null;
    const c = String(code).trim();
    if (!/^\d+$/.test(c)) return c;
    if (level === "province") return c.padStart(2, "0") + "0000";
    if (level === "city") return c.padStart(4, "0") + "00";
    if (level === "county") return c.padStart(6, "0");
    return c.padStart(6, "0");
}

function geoJsonUrl(level: OverviewLevel, adcode: string | null): string {
    // Prefer static China provinces; deeper levels go through same-origin proxy → DataV.
    const code = level === "country" || !adcode ? "100000" : adcode;
    if (code === "100000") return "/geo/100000_full.json";
    return `/api/geo/${code}`;
}

function pct(n: number, total: number): number {
    if (!total) return 0;
    return Math.round((n / total) * 1000) / 10;
}

function isoDate(d: Date): string {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}

function daysAgo(n: number): { from: string; to: string } {
    const to = new Date();
    const from = new Date(to);
    from.setDate(from.getDate() - n);
    return { from: isoDate(from), to: isoDate(to) };
}

function seasonWindow(months: number[]): { from: string; to: string } {
    const ms = months.length ? months : DEFAULT_PHENOLOGY_MONTHS;
    const year = new Date().getFullYear();
    const minM = Math.min(...ms);
    const maxM = Math.max(...ms);
    const from = new Date(year, minM - 1, 1);
    const to = new Date(year, maxM, 0); // last day of max month
    const today = new Date();
    if (to > today) {
        return { from: isoDate(from), to: isoDate(today) };
    }
    return { from: isoDate(from), to: isoDate(to) };
}

function metricProp(metric: MapMetric): string {
    if (metric === "drought") return "drought_alert";
    return metric;
}

/** Step interpolate fill for a numeric property (0 → muted, high → accent). */
function fillColorExpr(prop: string, highColor: string): unknown {
    return [
        "case",
        ["==", ["get", "has_data"], 0],
        "#64748b",
        [
            "interpolate",
            ["linear"],
            ["get", prop],
            0,
            "#1e293b",
            1,
            "#334155",
            5,
            highColor,
            20,
            highColor,
        ],
    ];
}

function metricHighColor(metric: MapMetric): string {
    switch (metric) {
        case "drought":
            return DROUGHT_CLASS_STYLE.severe.color;
        case "flood":
            return FLOOD_CLASS_STYLE.flood.color;
        case "weak_growth":
            return "#ca8a04";
        case "parcel_count":
            return "#22c55e";
    }
}

function StatBar({
    label,
    color,
    count,
    total,
}: {
    label: string;
    color: string;
    count: number;
    total: number;
}) {
    const p = pct(count, total);
    return (
        <div className="space-y-1">
            <div className="flex justify-between text-xs">
                <span className="flex items-center gap-1.5">
                    <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: color }} />
                    {label}
                </span>
                <span className="text-muted-foreground tabular-nums">
                    {count} ({p}%)
                </span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full transition-all" style={{ width: `${p}%`, background: color }} />
            </div>
        </div>
    );
}

export default function OverviewPage() {
    const t = useTranslations("overviewPage");
    const locale = useLocale();
    const defaultWindow = useMemo(() => daysAgo(60), []);

    const [drill, setDrill] = useState<DrillState>({ level: "country" });
    const [fromDate, setFromDate] = useState(defaultWindow.from);
    const [toDate, setToDate] = useState(defaultWindow.to);
    const [preset, setPreset] = useState<DatePreset>("60d");
    const [crop, setCrop] = useState("");
    const [crops, setCrops] = useState<CropOption[]>([]);
    const [metric, setMetric] = useState<MapMetric>("drought");

    const [stats, setStats] = useState<OverviewStats | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [mapReady, setMapReady] = useState(false);
    const [mapError, setMapError] = useState<string | null>(null);

    const [weakItems, setWeakItems] = useState<OverviewWeakParcel[]>([]);
    const [weakTotal, setWeakTotal] = useState(0);
    const [weakOffset, setWeakOffset] = useState(0);
    const [weakLoading, setWeakLoading] = useState(false);

    const mapContainerRef = useRef<HTMLDivElement>(null);
    const mapRef = useRef<maplibregl.Map | null>(null);
    const statsRef = useRef<OverviewStats | null>(null);
    const metricRef = useRef<MapMetric>(metric);
    const onFeatureClickRef = useRef<(child: OverviewChild) => void>(() => {});

    statsRef.current = stats;
    metricRef.current = metric;

    useEffect(() => {
        let cancelled = false;
        cropsApi
            .list()
            .then((rows) => {
                if (!cancelled) setCrops(rows);
            })
            .catch(() => {
                if (!cancelled) setCrops([]);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    const seasonMonths = useMemo(() => {
        if (!crop) return DEFAULT_PHENOLOGY_MONTHS;
        const found = crops.find((c) => c.key === crop);
        return found?.season_months?.length ? found.season_months : DEFAULT_PHENOLOGY_MONTHS;
    }, [crop, crops]);

    const applyPreset = useCallback(
        (p: DatePreset) => {
            setPreset(p);
            if (p === "30d") {
                const w = daysAgo(30);
                setFromDate(w.from);
                setToDate(w.to);
            } else if (p === "60d") {
                const w = daysAgo(60);
                setFromDate(w.from);
                setToDate(w.to);
            } else if (p === "season") {
                const w = seasonWindow(seasonMonths);
                setFromDate(w.from);
                setToDate(w.to);
            }
        },
        [seasonMonths],
    );

    // When crop changes under 本季 preset, refresh season window
    useEffect(() => {
        if (preset !== "season") return;
        const w = seasonWindow(seasonMonths);
        setFromDate(w.from);
        setToDate(w.to);
    }, [seasonMonths, preset]);

    const loadStats = useCallback(
        async (d: DrillState, from: string, to: string, cropKey: string) => {
            setLoading(true);
            setError(null);
            try {
                const res = await agriApi.overviewStats({
                    level: d.level,
                    code: d.code,
                    name: d.name,
                    from,
                    to,
                    crop: cropKey || undefined,
                });
                setStats(res);
            } catch (e: unknown) {
                const msg =
                    e && typeof e === "object" && "detail" in e
                        ? String((e as { detail: unknown }).detail)
                        : t("loadFailed");
                setError(msg || t("loadFailed"));
                setStats(null);
            } finally {
                setLoading(false);
            }
        },
        [t],
    );

    useEffect(() => {
        void loadStats(drill, fromDate, toDate, crop);
    }, [drill, fromDate, toDate, crop, loadStats]);

    // Reset weak pagination when filters / drill change
    useEffect(() => {
        setWeakOffset(0);
    }, [drill, fromDate, toDate, crop]);

    const loadWeak = useCallback(
        async (d: DrillState, from: string, to: string, cropKey: string, offset: number) => {
            setWeakLoading(true);
            try {
                const res = await agriApi.overviewWeakParcels({
                    level: d.level,
                    code: d.code,
                    name: d.name,
                    from,
                    to,
                    crop: cropKey || undefined,
                    limit: WEAK_PAGE_SIZE,
                    offset,
                });
                setWeakItems(res.items);
                setWeakTotal(res.total);
            } catch {
                setWeakItems([]);
                setWeakTotal(0);
            } finally {
                setWeakLoading(false);
            }
        },
        [],
    );

    useEffect(() => {
        void loadWeak(drill, fromDate, toDate, crop, weakOffset);
    }, [drill, fromDate, toDate, crop, weakOffset, loadWeak]);

    const drillToChild = useCallback((child: OverviewChild) => {
        setDrill({ level: child.level, code: child.code ?? undefined, name: child.name });
    }, []);

    onFeatureClickRef.current = drillToChild;

    // Init map once
    useEffect(() => {
        if (!mapContainerRef.current || mapRef.current) return;
        registerPMTilesProtocol();

        // Dark basemap matches app chrome; OSM-style raster is more reliable than Esri in some networks.
        const dark = {
            version: 8 as const,
            glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
            sources: {
                carto: {
                    type: "raster" as const,
                    tiles: [
                        "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
                        "https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
                        "https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
                    ],
                    tileSize: 256,
                    attribution: '&copy; <a href="https://carto.com">CARTO</a>',
                },
            },
            layers: [
                {
                    id: "background",
                    type: "background" as const,
                    paint: { "background-color": "#0b1220" },
                },
                { id: "carto-layer", type: "raster" as const, source: "carto", minzoom: 0, maxzoom: 19 },
            ],
        };

        const map = new maplibregl.Map({
            container: mapContainerRef.current,
            style: dark,
            center: CHINA_CENTER,
            zoom: 3.4,
            maxBounds: [
                [CHINA_BOUNDS[0][0] - 5, CHINA_BOUNDS[0][1] - 5],
                [CHINA_BOUNDS[1][0] + 5, CHINA_BOUNDS[1][1] + 5],
            ],
            transformRequest: createTransformRequest(),
            attributionControl: { compact: true },
        });
        map.addControl(new maplibregl.NavigationControl(), "top-left");
        const tokenRefresh = setInterval(() => refreshMapToken(), 10 * 60_000);
        mapRef.current = map;

        const resize = () => {
            try {
                map.resize();
            } catch {
                /* ignore */
            }
        };
        const ro = new ResizeObserver(() => resize());
        ro.observe(mapContainerRef.current);
        // Layout often settles after first paint
        requestAnimationFrame(resize);
        setTimeout(resize, 50);
        setTimeout(resize, 300);

        map.on("load", () => {
            resize();
            setMapReady(true);
        });
        map.on("error", (e) => {
            console.warn("overview map error", e);
        });

        map.on("click", "overview-fill", (e) => {
            const f = e.features?.[0];
            if (!f) return;
            const name = String(f.properties?.name ?? f.properties?.adname ?? "");
            const adcode = f.properties?.adcode != null ? String(f.properties.adcode) : null;
            const children = statsRef.current?.children ?? [];
            const match = children.find((c) => {
                const padded = padAdcode(c.level, c.code);
                if (adcode && padded && (padded === adcode || c.code === adcode)) return true;
                return c.name === name;
            });
            if (match) onFeatureClickRef.current(match);
        });

        map.on("mouseenter", "overview-fill", () => {
            map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", "overview-fill", () => {
            map.getCanvas().style.cursor = "";
        });

        return () => {
            clearInterval(tokenRefresh);
            ro.disconnect();
            setMapReady(false);
            map.remove();
            mapRef.current = null;
        };
    }, []);

    // Load / update choropleth when stats or map ready
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !stats) return;

        let cancelled = false;
        const fetchLevel: OverviewLevel =
            stats.region.level === "county" ? "city" : stats.region.level;
        const fetchAdcode =
            fetchLevel === "country"
                ? "100000"
                : stats.region.level === "county"
                  ? padAdcode(
                        "city",
                        stats.region.path.find((n) => n.level === "city")?.code ?? null,
                    )
                  : stats.region.adcode ||
                    padAdcode(stats.region.level, stats.region.code);
        const fetchUrl = geoJsonUrl(fetchLevel, fetchAdcode);

        (async () => {
            try {
                setMapError(null);
                const res = await fetch(fetchUrl);
                if (!res.ok) throw new Error(`geojson ${res.status}`);
                const gj = await res.json();
                if (cancelled || !mapRef.current) return;

                const childByName = new Map(stats.children.map((c) => [c.name, c]));
                const childByCode = new Map(
                    stats.children
                        .filter((c) => c.code)
                        .flatMap((c) => {
                            const entries: [string, OverviewChild][] = [[String(c.code), c]];
                            const padded = padAdcode(c.level, c.code);
                            if (padded) entries.push([padded, c]);
                            return entries;
                        }),
                );

                const features = (gj.features || []).map((f: GeoJSON.Feature) => {
                    const props = f.properties || {};
                    const name = String(props.name ?? props.adname ?? "");
                    const ac = props.adcode != null ? String(props.adcode) : "";
                    const child = childByCode.get(ac) || childByName.get(name) || null;
                    return {
                        ...f,
                        properties: {
                            ...props,
                            name,
                            parcel_count: child?.parcel_count ?? 0,
                            drought_severe: child?.drought_severe ?? 0,
                            drought_alert: child?.drought_alert ?? 0,
                            flood: child?.flood ?? 0,
                            weak_growth: child?.weak_growth ?? 0,
                            has_data: child ? 1 : 0,
                        },
                    };
                });

                const fc: GeoJSON.FeatureCollection = { type: "FeatureCollection", features };
                const mProp = metricProp(metricRef.current);
                const high = metricHighColor(metricRef.current);

                const apply = () => {
                    const m = mapRef.current;
                    if (!m || cancelled) return;
                    try {
                        m.resize();
                    } catch {
                        /* ignore */
                    }
                    if (m.getSource("overview")) {
                        (m.getSource("overview") as maplibregl.GeoJSONSource).setData(fc);
                        if (m.getLayer("overview-fill")) {
                            m.setPaintProperty("overview-fill", "fill-color", fillColorExpr(mProp, high) as never);
                        }
                    } else {
                        m.addSource("overview", { type: "geojson", data: fc });
                        m.addLayer({
                            id: "overview-fill",
                            type: "fill",
                            source: "overview",
                            paint: {
                                "fill-color": fillColorExpr(mProp, high) as never,
                                "fill-opacity": 0.65,
                            },
                        });
                        m.addLayer({
                            id: "overview-line",
                            type: "line",
                            source: "overview",
                            paint: {
                                "line-color": "#e2e8f0",
                                "line-width": 0.9,
                            },
                        });
                    }

                    try {
                        const bounds = new maplibregl.LngLatBounds();
                        for (const f of features) {
                            const geom = f.geometry;
                            if (!geom) continue;
                            const ringCoords = (coords: number[][]) => {
                                for (const c of coords) {
                                    if (c.length >= 2) bounds.extend([c[0], c[1]]);
                                }
                            };
                            if (geom.type === "Polygon") {
                                ringCoords(geom.coordinates[0] as number[][]);
                            } else if (geom.type === "MultiPolygon") {
                                for (const poly of geom.coordinates) {
                                    ringCoords(poly[0] as number[][]);
                                }
                            }
                        }
                        if (!bounds.isEmpty()) {
                            m.fitBounds(bounds, { padding: 40, duration: 600, maxZoom: 9 });
                        }
                    } catch {
                        /* ignore */
                    }
                };

                if (map.isStyleLoaded()) apply();
                else map.once("load", apply);
            } catch (err) {
                console.warn("overview geojson load failed", err);
                if (!cancelled) setMapError(t("mapLoadFailed"));
            }
        })();

        return () => {
            cancelled = true;
        };
    }, [stats, mapReady, t]);

    // Metric toggle: recolor without reloading geojson
    useEffect(() => {
        const map = mapRef.current;
        if (!map || !mapReady || !map.getLayer("overview-fill")) return;
        const prop = metricProp(metric);
        const high = metricHighColor(metric);
        try {
            map.setPaintProperty("overview-fill", "fill-color", fillColorExpr(prop, high) as never);
        } catch {
            /* ignore */
        }
    }, [metric, mapReady, stats]);

    const path = stats?.region.path ?? [{ level: "country" as const, code: null, name: t("breadcrumbCountry") }];
    const total = stats?.totals.parcel_count ?? 0;

    const cropLabel = (c: CropOption) =>
        locale.startsWith("zh") ? `${c.name_zh}（${c.name}）` : `${c.name} (${c.name_zh})`;

    const metricButtons: { key: MapMetric; label: string }[] = [
        { key: "drought", label: t("drought") },
        { key: "flood", label: t("flood") },
        { key: "weak_growth", label: t("weakGrowth") },
        { key: "parcel_count", label: t("parcels") },
    ];

    const legendColor = metricHighColor(metric);
    const weakPage = Math.floor(weakOffset / WEAK_PAGE_SIZE) + 1;
    const weakPages = Math.max(1, Math.ceil(weakTotal / WEAK_PAGE_SIZE));

    return (
        <div className="flex h-[calc(100vh-0px)] min-h-0 flex-1 flex-col gap-3 p-4 lg:p-6">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-bold tracking-tight">{t("title")}</h1>
                    <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
                </div>
            </div>

            {/* Toolbar: date + crop */}
            <div className="flex flex-wrap items-end gap-3 rounded-lg border bg-card/40 p-3">
                <div className="flex flex-wrap items-end gap-2">
                    <div className="space-y-1">
                        <Label htmlFor="overview-from" className="text-xs text-muted-foreground">
                            {t("dateFrom")}
                        </Label>
                        <Input
                            id="overview-from"
                            type="date"
                            className="h-9 w-[140px]"
                            value={fromDate}
                            onChange={(e) => {
                                setPreset("custom");
                                setFromDate(e.target.value);
                            }}
                        />
                    </div>
                    <div className="space-y-1">
                        <Label htmlFor="overview-to" className="text-xs text-muted-foreground">
                            {t("dateTo")}
                        </Label>
                        <Input
                            id="overview-to"
                            type="date"
                            className="h-9 w-[140px]"
                            value={toDate}
                            onChange={(e) => {
                                setPreset("custom");
                                setToDate(e.target.value);
                            }}
                        />
                    </div>
                    <div className="flex flex-wrap gap-1 pb-0.5">
                        {(
                            [
                                ["30d", t("preset30d")],
                                ["60d", t("preset60d")],
                                ["season", t("presetSeason")],
                            ] as const
                        ).map(([key, label]) => (
                            <Button
                                key={key}
                                type="button"
                                size="sm"
                                variant={preset === key ? "default" : "outline"}
                                className="h-8"
                                onClick={() => applyPreset(key)}
                            >
                                {label}
                            </Button>
                        ))}
                    </div>
                </div>
                <div className="space-y-1 min-w-[200px]">
                    <Label htmlFor="overview-crop" className="text-xs text-muted-foreground">
                        {t("cropPhenology")}
                    </Label>
                    <select
                        id="overview-crop"
                        className={cn(
                            "flex h-9 w-full min-w-[200px] rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm",
                            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                        )}
                        value={crop}
                        onChange={(e) => setCrop(e.target.value)}
                    >
                        <option value="">{t("cropDefault")}</option>
                        {crops.map((c) => (
                            <option key={c.key} value={c.key}>
                                {cropLabel(c)}
                            </option>
                        ))}
                    </select>
                    <p className="text-[11px] text-muted-foreground">{t("cropHint")}</p>
                </div>
            </div>

            {/* Breadcrumb */}
            <nav className="flex flex-wrap items-center gap-1 text-sm">
                {path.map((node, i) => {
                    const isLast = i === path.length - 1;
                    return (
                        <React.Fragment key={`${node.level}-${node.code ?? node.name}`}>
                            {i > 0 && <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />}
                            <button
                                type="button"
                                disabled={isLast}
                                className={cn(
                                    "rounded px-1.5 py-0.5",
                                    isLast
                                        ? "font-semibold text-foreground"
                                        : "text-primary hover:bg-primary-subtle",
                                )}
                                onClick={() => {
                                    if (node.level === "country") {
                                        setDrill({ level: "country" });
                                    } else {
                                        setDrill({
                                            level: node.level,
                                            code: node.code ?? undefined,
                                            name: node.name,
                                        });
                                    }
                                }}
                            >
                                {node.level === "country" ? t("breadcrumbCountry") : node.name}
                            </button>
                        </React.Fragment>
                    );
                })}
            </nav>

            <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[1fr_340px]">
                {/* Map + children list + weak table */}
                <div className="flex min-h-0 flex-col gap-3 overflow-y-auto">
                    {/* Metric toggle */}
                    <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs text-muted-foreground">{t("mapMetric")}:</span>
                        <div className="flex flex-wrap gap-1">
                            {metricButtons.map((b) => (
                                <Button
                                    key={b.key}
                                    type="button"
                                    size="sm"
                                    variant={metric === b.key ? "default" : "outline"}
                                    className="h-8"
                                    onClick={() => setMetric(b.key)}
                                >
                                    {b.label}
                                </Button>
                            ))}
                        </div>
                        <div className="ml-auto flex items-center gap-2 text-[11px] text-muted-foreground">
                            <span>{t("legendLow")}</span>
                            <span
                                className="inline-block h-2.5 w-24 rounded-sm"
                                style={{
                                    background: `linear-gradient(90deg, #1e293b, ${legendColor})`,
                                }}
                            />
                            <span>{t("legendHigh")}</span>
                        </div>
                    </div>

                    <div className="relative h-[min(52vh,560px)] min-h-[360px] w-full overflow-hidden rounded-lg border bg-[#0b1220]">
                        <div ref={mapContainerRef} className="absolute inset-0 h-full w-full" />
                        {loading && (
                            <div className="absolute inset-0 z-10 flex items-center justify-center bg-background/40">
                                <Loader2 className="h-6 w-6 animate-spin text-primary" />
                            </div>
                        )}
                        {mapError && (
                            <div className="absolute inset-x-0 top-0 z-10 bg-destructive/90 px-3 py-2 text-center text-xs text-destructive-foreground">
                                {mapError}
                            </div>
                        )}
                        <div className="pointer-events-none absolute bottom-2 left-2 z-10 rounded bg-background/80 px-2 py-1 text-[11px] text-muted-foreground">
                            {t("clickMapHint")}
                        </div>
                    </div>

                    <Card className="max-h-48 overflow-hidden">
                        <CardHeader className="py-3 px-4">
                            <CardTitle className="text-sm font-medium">{t("children")}</CardTitle>
                        </CardHeader>
                        <CardContent className="overflow-y-auto px-2 pb-2 pt-0 max-h-32">
                            {!stats?.children?.length ? (
                                <p className="px-2 text-xs text-muted-foreground">{t("noChildren")}</p>
                            ) : (
                                <ul className="space-y-0.5">
                                    {stats.children.map((c) => (
                                        <li key={`${c.level}-${c.code ?? c.name}`}>
                                            <Button
                                                variant="ghost"
                                                className="h-auto w-full justify-between px-2 py-1.5 text-left"
                                                onClick={() => drillToChild(c)}
                                            >
                                                <span className="truncate font-medium">{c.name}</span>
                                                <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                                                    {c.parcel_count} · 旱{c.drought_alert ?? c.drought_severe} · 涝
                                                    {c.flood} · 弱{c.weak_growth}
                                                </span>
                                            </Button>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </CardContent>
                    </Card>

                    {/* Weak-growth parcels table */}
                    <Card>
                        <CardHeader className="flex flex-row items-center justify-between space-y-0 py-3 px-4">
                            <CardTitle className="text-sm font-medium">{t("weakParcelsTitle")}</CardTitle>
                            <span className="text-xs text-muted-foreground tabular-nums">
                                {t("weakParcelsTotal", { total: weakTotal })}
                            </span>
                        </CardHeader>
                        <CardContent className="px-2 pb-3 pt-0">
                            {weakLoading ? (
                                <div className="flex items-center justify-center py-6">
                                    <Loader2 className="h-5 w-5 animate-spin text-primary" />
                                </div>
                            ) : !weakItems.length ? (
                                <p className="px-2 py-4 text-center text-xs text-muted-foreground">
                                    {t("weakParcelsEmpty")}
                                </p>
                            ) : (
                                <>
                                    <div className="overflow-x-auto">
                                        <table className="w-full text-xs">
                                            <thead>
                                                <tr className="border-b text-left text-muted-foreground">
                                                    <th className="px-2 py-1.5 font-medium">{t("colName")}</th>
                                                    <th className="px-2 py-1.5 font-medium">{t("colArea")}</th>
                                                    <th className="px-2 py-1.5 font-medium">{t("colNdvi")}</th>
                                                    <th className="px-2 py-1.5 font-medium">{t("colDate")}</th>
                                                    <th className="px-2 py-1.5 font-medium">{t("colAdmin")}</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {weakItems.map((row) => (
                                                    <tr key={row.land_id} className="border-b border-border/40">
                                                        <td className="max-w-[140px] truncate px-2 py-1.5 font-medium">
                                                            {row.land_name || row.land_id}
                                                        </td>
                                                        <td className="px-2 py-1.5 tabular-nums">
                                                            {Math.round(row.land_area_mu).toLocaleString()}
                                                        </td>
                                                        <td className="px-2 py-1.5 tabular-nums">
                                                            {row.ndvi_avg.toFixed(3)}
                                                        </td>
                                                        <td className="px-2 py-1.5 tabular-nums">
                                                            {row.scene_date ?? "—"}
                                                        </td>
                                                        <td className="max-w-[160px] truncate px-2 py-1.5 text-muted-foreground">
                                                            {[row.province_name, row.city_name, row.county_name]
                                                                .filter(Boolean)
                                                                .join(" / ") || "—"}
                                                        </td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                    {weakTotal > WEAK_PAGE_SIZE && (
                                        <div className="mt-2 flex items-center justify-between px-2">
                                            <Button
                                                type="button"
                                                size="sm"
                                                variant="outline"
                                                className="h-7"
                                                disabled={weakOffset <= 0 || weakLoading}
                                                onClick={() =>
                                                    setWeakOffset((o) => Math.max(0, o - WEAK_PAGE_SIZE))
                                                }
                                            >
                                                <ChevronLeft className="h-3.5 w-3.5" />
                                                {t("prevPage")}
                                            </Button>
                                            <span className="text-[11px] text-muted-foreground tabular-nums">
                                                {weakPage} / {weakPages}
                                            </span>
                                            <Button
                                                type="button"
                                                size="sm"
                                                variant="outline"
                                                className="h-7"
                                                disabled={weakOffset + WEAK_PAGE_SIZE >= weakTotal || weakLoading}
                                                onClick={() => setWeakOffset((o) => o + WEAK_PAGE_SIZE)}
                                            >
                                                {t("nextPage")}
                                                <ChevronRight className="h-3.5 w-3.5" />
                                            </Button>
                                        </div>
                                    )}
                                </>
                            )}
                        </CardContent>
                    </Card>
                </div>

                {/* Right panel cards */}
                <div className="flex flex-col gap-3 overflow-y-auto">
                    {error && (
                        <Card className="border-destructive/40">
                            <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
                        </Card>
                    )}

                    <Card>
                        <CardHeader className="py-3 px-4">
                            <CardTitle className="text-sm">{stats?.region.name ?? t("title")}</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-1 px-4 pb-4 text-sm">
                            <div className="flex justify-between">
                                <span className="text-muted-foreground">{t("parcels")}</span>
                                <span className="font-semibold tabular-nums">{total}</span>
                            </div>
                            <div className="flex justify-between">
                                <span className="text-muted-foreground">{t("areaMu")}</span>
                                <span className="font-semibold tabular-nums">
                                    {stats ? Math.round(stats.totals.area_mu).toLocaleString() : "—"}
                                </span>
                            </div>
                            {stats?.filters && (
                                <div className="pt-1 text-[11px] text-muted-foreground">
                                    {t("dateWindow")}: {stats.filters.from} → {stats.filters.to}
                                    {stats.filters.crop ? ` · ${stats.filters.crop}` : ""}
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader className="py-3 px-4">
                            <CardTitle className="text-sm">{t("drought")}</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-2.5 px-4 pb-4">
                            <StatBar
                                label={t("severe")}
                                color={DROUGHT_CLASS_STYLE.severe.color}
                                count={stats?.drought.severe ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("moderate")}
                                color={DROUGHT_CLASS_STYLE.moderate.color}
                                count={stats?.drought.moderate ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("mild")}
                                color={DROUGHT_CLASS_STYLE.mild.color}
                                count={stats?.drought.mild ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("normal")}
                                color={DROUGHT_CLASS_STYLE.normal.color}
                                count={stats?.drought.normal ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("unknown")}
                                color="#9ca3af"
                                count={stats?.drought.unknown ?? 0}
                                total={total}
                            />
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader className="py-3 px-4">
                            <CardTitle className="text-sm">{t("flood")}</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-2.5 px-4 pb-4">
                            <StatBar
                                label={t("floodClass")}
                                color={FLOOD_CLASS_STYLE.flood.color}
                                count={stats?.flood.flood ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("wet")}
                                color={FLOOD_CLASS_STYLE.wet.color}
                                count={stats?.flood.wet ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("dry")}
                                color="#86efac"
                                count={stats?.flood.dry ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("unknown")}
                                color="#9ca3af"
                                count={stats?.flood.unknown ?? 0}
                                total={total}
                            />
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader className="py-3 px-4">
                            <CardTitle className="text-sm">{t("weakGrowth")}</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-2 px-4 pb-4 text-sm">
                            <div className="flex justify-between">
                                <span className="text-muted-foreground">{t("parcels")}</span>
                                <span className="font-semibold tabular-nums">
                                    {stats?.weak_growth.parcel_count ?? 0}
                                    <span className="ml-1 text-xs font-normal text-muted-foreground">
                                        ({pct(stats?.weak_growth.parcel_count ?? 0, total)}%)
                                    </span>
                                </span>
                            </div>
                            <div className="flex justify-between">
                                <span className="text-muted-foreground">{t("areaMu")}</span>
                                <span className="font-semibold tabular-nums">
                                    {stats ? Math.round(stats.weak_growth.area_mu).toLocaleString() : "—"}
                                </span>
                            </div>
                            <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                                <div
                                    className="h-full rounded-full bg-amber-500 transition-all"
                                    style={{ width: `${pct(stats?.weak_growth.parcel_count ?? 0, total)}%` }}
                                />
                            </div>
                        </CardContent>
                    </Card>
                </div>
            </div>
        </div>
    );
}

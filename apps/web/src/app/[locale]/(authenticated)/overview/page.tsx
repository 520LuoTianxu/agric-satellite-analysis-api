"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useTranslations } from "next-intl";
import { ChevronRight, Loader2 } from "lucide-react";
import { agriApi, type OverviewChild, type OverviewLevel, type OverviewStats } from "@/lib/api";
import { registerPMTilesProtocol } from "@/lib/pmtiles";
import { createTransformRequest, refreshMapToken } from "@/lib/map-auth";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { DROUGHT_CLASS_STYLE, FLOOD_CLASS_STYLE } from "@/lib/agri-heatmap";

/** China approximate bounds [west, south, east, north]. */
const CHINA_BOUNDS: [[number, number], [number, number]] = [
    [73, 18],
    [135, 54],
];

const CHINA_CENTER: [number, number] = [104.5, 35.5];

type DrillState = {
    level: OverviewLevel;
    code?: string;
    name?: string;
};

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
    const [drill, setDrill] = useState<DrillState>({ level: "country" });
    const [stats, setStats] = useState<OverviewStats | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [mapReady, setMapReady] = useState(false);
    const [mapError, setMapError] = useState<string | null>(null);

    const mapContainerRef = useRef<HTMLDivElement>(null);
    const mapRef = useRef<maplibregl.Map | null>(null);
    const statsRef = useRef<OverviewStats | null>(null);
    const onFeatureClickRef = useRef<(child: OverviewChild) => void>(() => {});

    statsRef.current = stats;

    const loadStats = useCallback(async (d: DrillState) => {
        setLoading(true);
        setError(null);
        try {
            const res = await agriApi.overviewStats({
                level: d.level,
                code: d.code,
                name: d.name,
            });
            setStats(res);
        } catch (e: unknown) {
            const msg = e && typeof e === "object" && "detail" in e ? String((e as { detail: unknown }).detail) : t("loadFailed");
            setError(msg || t("loadFailed"));
            setStats(null);
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        void loadStats(drill);
    }, [drill, loadStats]);

    const drillToChild = useCallback((child: OverviewChild) => {
        if (child.level === "county") {
            // County is leaf for P0 — still set so breadcrumb/stats update
            setDrill({ level: child.level, code: child.code ?? undefined, name: child.name });
            return;
        }
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
                            flood: child?.flood ?? 0,
                            weak_growth: child?.weak_growth ?? 0,
                            has_data: child ? 1 : 0,
                        },
                    };
                });

                const fc: GeoJSON.FeatureCollection = { type: "FeatureCollection", features };

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
                    } else {
                        m.addSource("overview", { type: "geojson", data: fc });
                        m.addLayer({
                            id: "overview-fill",
                            type: "fill",
                            source: "overview",
                            paint: {
                                "fill-color": [
                                    "case",
                                    [">", ["get", "drought_severe"], 0],
                                    DROUGHT_CLASS_STYLE.severe.color,
                                    [">", ["get", "flood"], 0],
                                    FLOOD_CLASS_STYLE.flood.color,
                                    [">", ["get", "weak_growth"], 0],
                                    "#ca8a04",
                                    [">", ["get", "parcel_count"], 0],
                                    "#22c55e",
                                    "#64748b",
                                ],
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

    const path = stats?.region.path ?? [{ level: "country" as const, code: null, name: t("breadcrumbCountry") }];
    const total = stats?.totals.parcel_count ?? 0;

    const dateLabel = useMemo(() => {
        if (!stats) return "";
        return `${stats.filters.from} → ${stats.filters.to}`;
    }, [stats]);

    return (
        <div className="flex h-[calc(100vh-0px)] min-h-0 flex-1 flex-col gap-3 p-4 lg:p-6">
            <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                    <h1 className="text-2xl font-bold tracking-tight">{t("title")}</h1>
                    <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
                </div>
                {dateLabel && (
                    <div className="text-xs text-muted-foreground">
                        {t("dateWindow")}: <span className="font-medium text-foreground">{dateLabel}</span>
                    </div>
                )}
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
                {/* Map + children list */}
                <div className="flex min-h-0 flex-col gap-3">
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
                                                    {c.parcel_count} · 旱{c.drought_severe} · 涝{c.flood} · 弱{c.weak_growth}
                                                </span>
                                            </Button>
                                        </li>
                                    ))}
                                </ul>
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

"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useLocale, useTranslations } from "next-intl";
import { ChevronRight, Download, Loader2 } from "lucide-react";
import {
    agriApi,
    cropsApi,
    type CropOption,
    type OverviewChild,
    type OverviewLevel,
    type OverviewStats,
} from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { DROUGHT_CLASS_STYLE, FLOOD_CLASS_STYLE } from "@/lib/agri-heatmap";

/** China approximate bounds [west, south, east, north]. */
const CHINA_BOUNDS: [[number, number], [number, number]] = [
    [73, 18],
    [135, 54],
];

const CHINA_CENTER: [number, number] = [104.5, 35.5];

const DEFAULT_PHENOLOGY_MONTHS = [6, 7, 8, 9];

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
    // Aliyun DataV: https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json
    // country→100000 (provinces), province/city adcode→cities/counties. Served via local cache/proxy.
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
    if (metric === "flood") return "flood_alert";
    return metric;
}

/** Minimal choropleth: soft neutrals → gentle accent (no heavy dark fills). */
function fillColorExpr(prop: string, highColor: string): unknown {
    return [
        "case",
        ["==", ["get", "has_data"], 0],
        "#e8eaed",
        [
            "interpolate",
            ["linear"],
            ["get", prop],
            0,
            "#f3f4f6",
            1,
            "#e5e7eb",
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
            return "#fca5a5"; // soft rose
        case "flood":
            return "#93c5fd"; // soft blue
        case "weak_growth":
            return "#fcd34d"; // soft amber
        case "parcel_count":
            return "#a7f3d0"; // soft mint
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
        <div className="space-y-0.5">
            <div className="flex justify-between text-[11px] leading-tight">
                <span className="flex items-center gap-1 truncate">
                    <span className="inline-block h-2 w-2 shrink-0 rounded-sm" style={{ background: color }} />
                    <span className="truncate">{label}</span>
                </span>
                <span className="shrink-0 pl-1 text-muted-foreground tabular-nums">
                    {count}
                </span>
            </div>
            <div className="h-1 w-full overflow-hidden rounded-full bg-muted">
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


    const mapContainerRef = useRef<HTMLDivElement>(null);
    const mapRef = useRef<maplibregl.Map | null>(null);
    const statsRef = useRef<OverviewStats | null>(null);
    const metricRef = useRef<MapMetric>(metric);
    const onFeatureClickRef = useRef<(child: OverviewChild) => void>(() => {});
    const popupLabelsRef = useRef({
        parcels: "地块数",
        areaMu: "面积(亩)",
        drought: "干旱",
        flood: "洪涝",
        weakGrowth: "弱长势",
        noData: "暂无统计",
    });

    statsRef.current = stats;
    metricRef.current = metric;
    popupLabelsRef.current = {
        parcels: t("parcels"),
        areaMu: t("areaMu"),
        drought: t("drought"),
        flood: t("flood"),
        weakGrowth: t("weakGrowth"),
        noData: t("noChildren"),
    };

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

    const [exporting, setExporting] = useState(false);
    const runExport = useCallback(
        async (kind: "stats" | "weak") => {
            setExporting(true);
            try {
                const opts = {
                    level: drill.level,
                    code: drill.code,
                    name: drill.name,
                    from: fromDate,
                    to: toDate,
                    crop: crop || undefined,
                };
                if (kind === "stats") await agriApi.overviewExportStatsCsv(opts);
                else await agriApi.overviewExportWeakParcelsCsv(opts);
            } catch (e: unknown) {
                const msg =
                    e && typeof e === "object" && "detail" in e
                        ? String((e as { detail: unknown }).detail)
                        : t("exportFailed");
                setError(msg || t("exportFailed"));
            } finally {
                setExporting(false);
            }
        },
        [drill, fromDate, toDate, crop, t],
    );


    const drillToChild = useCallback((child: OverviewChild) => {
        setDrill({ level: child.level, code: child.code ?? undefined, name: child.name });
    }, []);

    onFeatureClickRef.current = drillToChild;

    // Init map once
    useEffect(() => {
        if (!mapContainerRef.current || mapRef.current) return;
        // No commercial basemap — only Aliyun DataV admin GeoJSON choropleth (no CARTO watermark).
        const blankStyle = {
            version: 8 as const,
            glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
            sources: {},
            layers: [
                {
                    id: "background",
                    type: "background" as const,
                    paint: { "background-color": "rgba(0,0,0,0)" },
                },
            ],
        };

        const map = new maplibregl.Map({
            container: mapContainerRef.current,
            style: blankStyle,
            center: CHINA_CENTER,
            zoom: 3.4,
            maxBounds: [
                [CHINA_BOUNDS[0][0] - 5, CHINA_BOUNDS[0][1] - 5],
                [CHINA_BOUNDS[1][0] + 5, CHINA_BOUNDS[1][1] + 5],
            ],
            attributionControl: {
                compact: true,
                customAttribution:
                    '<a href="https://datav.aliyun.com/portal/school/atlas/area_selector" target="_blank" rel="noreferrer">阿里云 DataV</a>',
            },
        });
        map.addControl(new maplibregl.NavigationControl(), "top-left");
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

        const popup = new maplibregl.Popup({
            closeButton: false,
            closeOnClick: false,
            offset: 12,
            className: "overview-hover-popup",
            maxWidth: "240px",
        });

        const formatPopupHtml = (props: Record<string, unknown>) => {
            const L = popupLabelsRef.current;
            const name = String(props.name ?? props.adname ?? "—");
            const has = Number(props.has_data) === 1;
            const row = (label: string, value: string) =>
                `<div style="display:flex;justify-content:space-between;gap:12px;line-height:1.45">` +
                `<span style="color:#111827">${label}</span>` +
                `<span style="color:#2563eb;font-variant-numeric:tabular-nums">${value}</span></div>`;
            if (!has) {
                return `<div style="font-weight:600;margin-bottom:4px;color:#111827">${name}</div>` +
                    `<div style="color:#6b7280;font-size:12px">${L.noData}</div>`;
            }
            const parcels = Number(props.parcel_count ?? 0);
            const area = Math.round(Number(props.area_mu ?? 0)).toLocaleString();
            const drought = Number(props.drought_alert ?? props.drought_severe ?? 0);
            const flood = Number(props.flood_alert ?? props.flood ?? 0);
            const weak = Number(props.weak_growth ?? 0);
            return (
                `<div style="font-weight:600;margin-bottom:6px;color:#111827">${name}</div>` +
                row(L.parcels, String(parcels)) +
                row(L.areaMu, area) +
                row(L.drought, String(drought)) +
                row(L.flood, String(flood)) +
                row(L.weakGrowth, String(weak))
            );
        };

        map.on("mousemove", "overview-fill", (e) => {
            map.getCanvas().style.cursor = "pointer";
            const f = e.features?.[0];
            if (!f || !e.lngLat) return;
            const props = (f.properties || {}) as Record<string, unknown>;
            popup.setLngLat(e.lngLat).setHTML(formatPopupHtml(props)).addTo(map);
        });
        map.on("mouseleave", "overview-fill", () => {
            map.getCanvas().style.cursor = "";
            popup.remove();
        });

        return () => {
            popup.remove();
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
                            area_mu: child?.area_mu ?? 0,
                            drought_severe: child?.drought_severe ?? 0,
                            drought_alert: child?.drought_alert ?? 0,
                            flood: child?.flood ?? 0,
                            flood_alert: child?.flood_alert ?? child?.flood ?? 0,
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
                            m.setPaintProperty("overview-fill", "fill-opacity", 0.55);
                        }
                    } else {
                        m.addSource("overview", { type: "geojson", data: fc });
                        m.addLayer({
                            id: "overview-fill",
                            type: "fill",
                            source: "overview",
                            paint: {
                                "fill-color": fillColorExpr(mProp, high) as never,
                                "fill-opacity": 0.55,
                            },
                        });
                        m.addLayer({
                            id: "overview-line",
                            type: "line",
                            source: "overview",
                            paint: {
                                "line-color": "#94a3b8",
                                "line-width": 0.6,
                                "line-opacity": 0.85,
                            },
                        });
                        if (!m.getLayer("overview-label")) {
                            m.addLayer({
                                id: "overview-label",
                                type: "symbol",
                                source: "overview",
                                layout: {
                                    "text-field": ["get", "name"],
                                    "text-size": 11,
                                    "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
                                    "text-max-width": 8,
                                },
                                paint: {
                                    "text-color": "#64748b",
                                    "text-halo-color": "rgba(255,255,255,0.9)",
                                    "text-halo-width": 1.4,
                                },
                            });
                        }
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
            map.setPaintProperty("overview-fill", "fill-opacity", 0.55);
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

    return (
        <div className="flex h-[calc(100vh-0px)] min-h-0 flex-1 flex-col gap-1.5 p-2 lg:p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
                <h1 className="text-base font-semibold tracking-tight lg:text-lg">{t("title")}</h1>
            </div>

            {/* Toolbar: date + crop (compact) */}
            <div className="flex flex-wrap items-center gap-1.5 rounded-md border bg-card/40 px-2 py-1">
                <Input
                    id="overview-from"
                    type="date"
                    aria-label={t("dateFrom")}
                    className="h-7 w-[118px] px-1.5 text-xs"
                    value={fromDate}
                    onChange={(e) => {
                        setPreset("custom");
                        setFromDate(e.target.value);
                    }}
                />
                <span className="text-[11px] text-muted-foreground">→</span>
                <Input
                    id="overview-to"
                    type="date"
                    aria-label={t("dateTo")}
                    className="h-7 w-[118px] px-1.5 text-xs"
                    value={toDate}
                    onChange={(e) => {
                        setPreset("custom");
                        setToDate(e.target.value);
                    }}
                />
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
                        className="h-7 px-2 text-xs"
                        onClick={() => applyPreset(key)}
                    >
                        {label}
                    </Button>
                ))}
                <select
                    id="overview-crop"
                    aria-label={t("cropPhenology")}
                    title={t("cropHint")}
                    className={cn(
                        "flex h-7 min-w-[120px] max-w-[160px] rounded-md border border-input bg-background px-1.5 text-xs shadow-sm",
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
            </div>

            {/* Breadcrumb */}
            <nav className="flex flex-wrap items-center gap-0.5 text-xs">
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

            <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 lg:grid-cols-[minmax(0,1fr)_200px]">
                {/* Map */}
                <div className="flex min-h-0 flex-col gap-1.5 overflow-hidden">
                    {/* Metric toggle */}
                    <div className="flex flex-wrap items-center gap-1">
                        <div className="flex flex-wrap gap-0.5">
                            {metricButtons.map((b) => (
                                <Button
                                    key={b.key}
                                    type="button"
                                    size="sm"
                                    variant={metric === b.key ? "default" : "outline"}
                                    className="h-6 px-2 text-[11px]"
                                    onClick={() => setMetric(b.key)}
                                >
                                    {b.label}
                                </Button>
                            ))}
                        </div>
                        <div className="ml-auto flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                            <span
                                className="inline-block h-1.5 w-14 rounded-sm"
                                style={{
                                    background: `linear-gradient(90deg, #f3f4f6, ${legendColor})`,
                                }}
                                title={`${t("legendLow")} → ${t("legendHigh")}`}
                            />
                            <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                className="h-6 px-1.5 text-[11px]"
                                disabled={exporting || loading}
                                onClick={() => void runExport("stats")}
                            >
                                <Download className="h-3 w-3" />
                            </Button>
                            <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                className="h-6 px-1.5 text-[11px]"
                                disabled={exporting || loading}
                                onClick={() => void runExport("weak")}
                            >
                                <Download className="mr-0.5 h-3 w-3" />
                                <span className="sr-only sm:not-sr-only">{t("exportWeak")}</span>
                            </Button>
                        </div>
                    </div>

                    <div className="relative min-h-0 w-full flex-1 overflow-hidden rounded-md border border-border/50 bg-transparent">
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
                </div>

                {/* Right panel cards */}
                <div className="flex flex-col gap-1.5 overflow-y-auto text-xs">
                    {error && (
                        <Card className="border-destructive/40">
                            <CardContent className="px-2 py-1.5 text-xs text-destructive">{error}</CardContent>
                        </Card>
                    )}

                    <Card>
                        <CardHeader className="px-2 py-1.5">
                            <CardTitle className="text-xs font-medium">{stats?.region.name ?? t("title")}</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-0.5 px-2 pb-2 text-xs">
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
                        <CardHeader className="px-2 py-1.5">
                            <CardTitle className="text-xs font-medium">{t("drought")}</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-1.5 px-2 pb-2">
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
                            {stats?.filters?.drought_source ? (
                                <p className="pt-1 text-[11px] text-muted-foreground">
                                    {t("droughtSource")}:{" "}
                                    {stats.filters.drought_source === "pixels"
                                        ? t("droughtSourcePixels")
                                        : stats.filters.drought_source === "cache"
                                          ? t("droughtSourceCache")
                                          : t("droughtSourceAvg")}
                                </p>
                            ) : null}
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader className="px-2 py-1.5">
                            <CardTitle className="text-xs font-medium">{t("flood")}</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-1.5 px-2 pb-2">
                            <StatBar
                                label={t("floodSevere")}
                                color={FLOOD_CLASS_STYLE.flood_severe.color}
                                count={stats?.flood.flood_severe ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("floodModerate")}
                                color={FLOOD_CLASS_STYLE.flood_moderate.color}
                                count={stats?.flood.flood_moderate ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("floodMild")}
                                color={FLOOD_CLASS_STYLE.flood_mild.color}
                                count={stats?.flood.flood_mild ?? stats?.flood.wet ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("dry")}
                                color="#bbf7d0"
                                count={stats?.flood.dry ?? 0}
                                total={total}
                            />
                            <StatBar
                                label={t("unknown")}
                                color="#d1d5db"
                                count={stats?.flood.unknown ?? 0}
                                total={total}
                            />
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader className="px-2 py-1.5">
                            <CardTitle className="text-xs font-medium">{t("weakGrowth")}</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-1 px-2 pb-2 text-xs">
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

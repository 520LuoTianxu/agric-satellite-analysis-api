"use client";

import React, { useEffect, useState, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { Link, useRouter } from "@/i18n/navigation";
import dynamic from "next/dynamic";
import maplibregl from "maplibre-gl";
import { fieldsApi, alertsApi, INDEX_CONFIG, ALL_INDEX_TYPES, monitoringApi, parseAgriLandId } from "@/lib/api";
import CropSelect from "@/components/field/crop-select";
import type { Field, RasterLayer, IndexType } from "@/lib/api";
import type { AgriHeatIndex, AgriHeatmapImage } from "@/lib/agri-heatmap";
import { AGRI_MODE_LABELS, AGRI_PRIMARY_MODES, canvasToObjectUrl, clipHeatmapImageToField, dataUrlToObjectUrl, heatmapImageHasContent, revokeHeatmapObjectUrl } from "@/lib/agri-heatmap";
import { cn } from "@/lib/utils";
import { formatAreaMu } from "@/lib/area";
import { toast } from "sonner";
import {
    ArrowLeft,
    CloudSun,
    Edit2,
    Layers,
    Loader2,
    Save,
    Trash2,
    X,
    Bell,
    ClipboardList,
    Share2,
    PanelRight,
    PanelRightClose,
    Satellite,
} from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useConfirm } from "@/components/confirm-dialog";
import { useTranslations } from "next-intl";
import { MAP_STYLES, type MapStyleId } from "@/lib/pmtiles";
import { tokenColor, MAP_CHROME } from "@/lib/design-tokens";
import { Skeleton } from "@/components/ui/skeleton";

const DrawMap = dynamic(() => import("@/components/map/draw-map"), {
    ssr: false,
    loading: () => (
        <Skeleton className="h-full w-full rounded-none" />
    ),
});

const BaseMap = dynamic(() => import("@/components/map/base-map"), {
    ssr: false,
    loading: () => (
        <Skeleton className="h-full w-full rounded-none" />
    ),
});

const NdviTab = dynamic(() => import("@/components/field/ndvi-tab"), {
    ssr: false,
    loading: () => (
        <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
    ),
});

const LocationSearch = dynamic(() => import("@/components/map/location-search"), {
    ssr: false,
});

const MapStyleSwitcher = dynamic(() => import("@/components/map/map-style-switcher"), {
    ssr: false,
});

/** Satellite codes as stored by the pipeline, expanded for the reader. */
const SATELLITE_LABELS: Record<string, string> = { S2: "Sentinel-2 L2A" };

const NdviLegend = dynamic(() => import("@/components/field/ndvi-legend"), {
    ssr: false,
});

const AgriHeatmapLegend = dynamic(() => import("@/components/field/agri-heatmap-legend"), {
    ssr: false,
});

const AlertsTab = dynamic(() => import("@/components/field/alerts-tab"), {
    ssr: false,
    loading: () => (
        <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
    ),
});

const LandReportTab = dynamic(() => import("@/components/field/land-report-tab"), {
    ssr: false,
    loading: () => (
        <div className="p-4 space-y-3">
            <Skeleton className="h-8 w-40" />
            <Skeleton className="h-24 w-full" />
        </div>
    ),
});

const ScoutingTab = dynamic(() => import("@/components/field/scouting-tab"), {
    ssr: false,
    loading: () => (
        <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
    ),
});

const ShareTab = dynamic(() => import("@/components/field/share-tab"), {
    ssr: false,
    loading: () => (
        <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
    ),
});

const WeatherTab = dynamic(() => import("@/components/field/weather-tab"), {
    ssr: false,
    loading: () => (
        <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
    ),
});

const SoilTab = dynamic(() => import("@/components/field/soil-tab"), {
    ssr: false,
    loading: () => (
        <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
    ),
});

/* ── Index overlay helpers ───────────────────────────────────── */

function indexSourceId(indexType: IndexType) {
    return `${indexType.toLowerCase()}-tiles`;
}
function indexLayerId(indexType: IndexType) {
    return `${indexType.toLowerCase()}-raster`;
}

function removeIndexOverlay(map: maplibregl.Map, indexType: IndexType) {
    const layerId = indexLayerId(indexType);
    const sourceId = indexSourceId(indexType);
    if (map.getLayer(layerId)) map.removeLayer(layerId);
    if (map.getSource(sourceId)) map.removeSource(sourceId);
}

function removeAllIndexOverlays(map: maplibregl.Map) {
    const types: IndexType[] = ["NDVI", "EVI", "SAVI", "NDWI"];
    for (const t of types) removeIndexOverlay(map, t);
}

function addIndexOverlay(map: maplibregl.Map, layer: RasterLayer, field: Field, indexType: IndexType) {
    // Fake agri:// placeholder COGs 500 on TiTiler — never add as raster tiles.
    if (!layer.tile_url || !field.geom) return;
    if ((layer.cog_uri || "").startsWith("agri://") || layer.tile_url.includes("agri://")) return;
    const sourceId = indexSourceId(indexType);
    const layerId = indexLayerId(indexType);
    const bounds = computeGeomBounds(field.geom);
    if (!map.getSource(sourceId)) {
        map.addSource(sourceId, {
            type: "raster",
            tiles: [layer.tile_url],
            tileSize: 256,
            ...(bounds ? { bounds } : {}),
        });
    }
    if (!map.getLayer(layerId)) {
        map.addLayer(
            {
                id: layerId,
                type: "raster",
                source: sourceId,
                paint: { "raster-opacity": 0.75 },
                minzoom: 10,
                maxzoom: 18,
            },
            map.getLayer("field-outline") ? "field-outline" : undefined,
        );
    }
}

function computeGeomBounds(geom: GeoJSON.Geometry): [number, number, number, number] | undefined {
    const coords: number[][] = [];
    const extract = (g: any) => {
        if (g.type === "Polygon") g.coordinates[0].forEach((c: number[]) => coords.push(c));
        else if (g.type === "MultiPolygon")
            g.coordinates.forEach((poly: number[][][]) =>
                poly[0].forEach((c: number[]) => coords.push(c)),
            );
    };
    extract(geom);
    if (coords.length === 0) return undefined;
    let minLng = Infinity,
        minLat = Infinity,
        maxLng = -Infinity,
        maxLat = -Infinity;
    for (const [lng, lat] of coords) {
        if (lng < minLng) minLng = lng;
        if (lat < minLat) minLat = lat;
        if (lng > maxLng) maxLng = lng;
        if (lat > maxLat) maxLat = lat;
    }
    return [minLng, minLat, maxLng, maxLat];
}

const PANEL_WIDTH_STORAGE_KEY = "openfarm.fieldPanelWidthPx";
const PANEL_WIDTH_DEFAULT_PX = 400; // 为图表和双列信息保留阅读宽度
const PANEL_WIDTH_MIN_PX = 288; // 18rem
const PANEL_WIDTH_MAX_PX = 640; // 40rem

function clampPanelWidthPx(px: number): number {
    const vwCap =
        typeof window !== "undefined" ? Math.floor(window.innerWidth * 0.5) : PANEL_WIDTH_MAX_PX;
    const max = Math.max(PANEL_WIDTH_MIN_PX, Math.min(PANEL_WIDTH_MAX_PX, vwCap));
    return Math.round(Math.min(max, Math.max(PANEL_WIDTH_MIN_PX, px)));
}

/* ── Page ──────────────────────────────────────────────────── */

export default function FieldDetailPage() {
    const t = useTranslations("fieldDetail");
    const confirm = useConfirm();
    const params = useParams();
    const router = useRouter();
    const farmId = params.id as string;
    const fieldId = params.fieldId as string;

    const [field, setField] = useState<Field | null>(null);
    const [loading, setLoading] = useState(true);

    // Edit mode
    const [editing, setEditing] = useState(false);
    const [editName, setEditName] = useState("");
    const [editCropType, setEditCropType] = useState("");
    const [editSeason, setEditSeason] = useState("");
    const [editGeom, setEditGeom] = useState<GeoJSON.Geometry | null>(null);
    const [saving, setSaving] = useState(false);

    // Map
    const [mapInstance, setMapInstance] = useState<maplibregl.Map | null>(null);
    const [mapStyle, setMapStyle] = useState<MapStyleId>("satellite");
    const [sidebarOpen, setSidebarOpen] = useState(false);
    const [activeTab, setActiveTab] = useState("info");
    const [panelWidthPx, setPanelWidthPx] = useState(PANEL_WIDTH_DEFAULT_PX);
    const [isResizingPanel, setIsResizingPanel] = useState(false);
    const panelWidthRef = useRef(PANEL_WIDTH_DEFAULT_PX);

    // Index overlay
    const [indexLayer, setIndexLayer] = useState<RasterLayer | null>(null);
    const [agriHeatmap, setAgriHeatmap] = useState<AgriHeatmapImage | null>(null);
    const [agriHeatMode, setAgriHeatMode] = useState<AgriHeatIndex>("ndvi");
    const [activeIndexType, setActiveIndexType] = useState<IndexType>("NDVI");
    const indexLayerRef = useRef<RasterLayer | null>(null);
    const activeIndexTypeRef = useRef<IndexType>("NDVI");
    const fieldRef = useRef<Field | null>(null);
    const agriHeatmapRef = useRef<AgriHeatmapImage | null>(null);
    /** blob: URL currently fed to MapLibre ImageSource — revoke on clear/replace. */
    const agriHeatmapBlobUrlRef = useRef<string | null>(null);
    /** Bump to cancel in-flight canvas.toBlob apply. */
    const agriHeatmapApplyGenRef = useRef(0);

    // Available index types (only indices with computed layers)
    const [availableTypes, setAvailableTypes] = useState<IndexType[]>([]);

    // Keyboard shortcut: toggle sidebar with Cmd/Ctrl + .
    useEffect(() => {
        const handleKey = (e: KeyboardEvent) => {
            if ((e.metaKey || e.ctrlKey) && e.key === ".") {
                e.preventDefault();
                setSidebarOpen((prev) => !prev);
            }
        };
        window.addEventListener("keydown", handleKey);
        return () => window.removeEventListener("keydown", handleKey);
    }, []);

    // Restore persisted field panel width (px). Invalid/missing → 22rem default.
    useEffect(() => {
        try {
            const raw = localStorage.getItem(PANEL_WIDTH_STORAGE_KEY);
            if (raw == null || raw === "") return;
            const n = Number(raw);
            if (!Number.isFinite(n) || n <= 0) return;
            const clamped = clampPanelWidthPx(n);
            panelWidthRef.current = clamped;
            setPanelWidthPx(clamped);
        } catch {
            /* ignore quota / private mode */
        }
    }, []);

    useEffect(() => {
        panelWidthRef.current = panelWidthPx;
    }, [panelWidthPx]);

    const persistPanelWidth = useCallback((px: number) => {
        try {
            localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(px));
        } catch {
            /* ignore */
        }
    }, []);

    const handlePanelResizeStart = useCallback(
        (e: React.MouseEvent) => {
            e.preventDefault();
            e.stopPropagation();
            setIsResizingPanel(true);
            const startX = e.clientX;
            const startW = panelWidthRef.current;

            const onMove = (ev: MouseEvent) => {
                // Dragging the left edge: move left → wider panel
                const next = clampPanelWidthPx(startW + (startX - ev.clientX));
                panelWidthRef.current = next;
                setPanelWidthPx(next);
            };
            const onUp = () => {
                setIsResizingPanel(false);
                persistPanelWidth(panelWidthRef.current);
                document.body.style.removeProperty("cursor");
                document.body.style.removeProperty("user-select");
                window.removeEventListener("mousemove", onMove);
                window.removeEventListener("mouseup", onUp);
            };

            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
            window.addEventListener("mousemove", onMove);
            window.addEventListener("mouseup", onUp);
        },
        [persistPanelWidth],
    );

    const handlePanelResizeReset = useCallback(() => {
        panelWidthRef.current = PANEL_WIDTH_DEFAULT_PX;
        setPanelWidthPx(PANEL_WIDTH_DEFAULT_PX);
        persistPanelWidth(PANEL_WIDTH_DEFAULT_PX);
    }, [persistPanelWidth]);

    // Alerts
    const [openAlertCount, setOpenAlertCount] = useState(0);

    // Fetch alert count independently (so badge shows without opening alerts tab)
    useEffect(() => {
        if (!fieldId) return;
        let cancelled = false;
        alertsApi.listForField(fieldId, 200).then((res) => {
            if (!cancelled) {
                setOpenAlertCount(res.items.filter((a) => a.status === "open").length);
            }
        }).catch(() => { });
        return () => { cancelled = true; };
    }, [fieldId]);

    // Fetch which index types have computed layers
    const refreshAvailableTypes = useCallback(() => {
        if (!fieldId) return;
        monitoringApi.layerTypes(fieldId).then((types) => {
            const upper = new Set(types.map((t) => t.toUpperCase() as IndexType));
            const sorted = ALL_INDEX_TYPES.filter((t) => upper.has(t));
            setAvailableTypes(sorted);
        }).catch(() => { });
    }, [fieldId]);

    useEffect(() => {
        refreshAvailableTypes();
    }, [refreshAvailableTypes]);

    // Keep refs in sync for style.load handler
    useEffect(() => {
        indexLayerRef.current = indexLayer;
    }, [indexLayer]);
    useEffect(() => {
        activeIndexTypeRef.current = activeIndexType;
    }, [activeIndexType]);
    useEffect(() => {
        fieldRef.current = field;
    }, [field]);
    useEffect(() => {
        agriHeatmapRef.current = agriHeatmap;
    }, [agriHeatmap]);


    const loadField = useCallback(async () => {
        try {
            const f = await fieldsApi.get(fieldId);
            setField(f);
            setEditName(f.name);
            setEditCropType(f.crop_type || "");
            setEditSeason(f.season || "");
            setEditGeom(f.geom);
        } catch {
            toast.error(t("fieldNotFound"));
            router.push(`/farms/${farmId}`);
        } finally {
            setLoading(false);
        }
    }, [fieldId, farmId, router, t]);

    useEffect(() => {
        loadField();
    }, [loadField]);

    const clearAgriHeatmapLayers = useCallback((map: maplibregl.Map) => {
        // Cancel any in-flight canvas.toBlob → ImageSource apply
        agriHeatmapApplyGenRef.current += 1;
        revokeHeatmapObjectUrl(agriHeatmapBlobUrlRef.current);
        agriHeatmapBlobUrlRef.current = null;
        const ids: [string, string][] = [
            ["agri-heatmap-raster", "agri-heatmap"],
            ["agri-heatmap-fill", "agri-heatmap-geojson"],
            ["agri-heatmap-circles", "agri-heatmap-points"],
        ];
        for (const [layerId, srcId] of ids) {
            try {
                if (map.getLayer(layerId)) map.removeLayer(layerId);
            } catch { /* ignore */ }
            try {
                if (map.getSource(srcId)) map.removeSource(srcId);
            } catch { /* ignore */ }
        }
    }, []);

    /**
     * Draw agri 色膜: MapLibre image raster ONLY (canvas-filled cells → blob:/data: film).
     * Do NOT co-add GeoJSON fill — abutting polygons + outlines create white grid seams.
     * Image uses canvas.toBlob → createObjectURL when possible (CSP connect-src has blob:/data:).
     */
    const applyAgriHeatmapToMap = useCallback(
        (
            map: maplibregl.Map,
            hm: AgriHeatmapImage,
            fieldGeom: GeoJSON.Polygon | GeoJSON.MultiPolygon | null | undefined,
            beforeId?: string,
        ) => {
            const imgSrcId = "agri-heatmap";
            const imgLayerId = "agri-heatmap-raster";

            // Strip any leftover GeoJSON fill from older builds (white seams).
            try {
                if (map.getLayer("agri-heatmap-fill")) map.removeLayer("agri-heatmap-fill");
            } catch { /* ignore */ }
            try {
                if (map.getSource("agri-heatmap-geojson")) map.removeSource("agri-heatmap-geojson");
            } catch { /* ignore */ }

            // Continuous canvas color film (nearest). Prefer blob: object URL.
            const clippedImg = clipHeatmapImageToField(hm, fieldGeom);
            const addImageLayer = (url: string) => {
                try {
                    if (map.getLayer(imgLayerId)) map.removeLayer(imgLayerId);
                } catch { /* ignore */ }
                try {
                    if (map.getSource(imgSrcId)) map.removeSource(imgSrcId);
                } catch { /* ignore */ }
                map.addSource(imgSrcId, {
                    type: "image",
                    url,
                    coordinates: clippedImg.coordinates,
                });
                const before =
                    beforeId && map.getLayer(beforeId) ? beforeId : undefined;
                map.addLayer(
                    {
                        id: imgLayerId,
                        type: "raster",
                        source: imgSrcId,
                        paint: {
                            "raster-opacity": 0.92,
                            "raster-resampling": "nearest",
                        },
                    },
                    before,
                );
            };

            if (heatmapImageHasContent(clippedImg) && clippedImg.dataUrl) {
                const dataUrl = clippedImg.dataUrl;
                const gen = ++agriHeatmapApplyGenRef.current;

                const commitUrl = (url: string) => {
                    if (gen !== agriHeatmapApplyGenRef.current) {
                        revokeHeatmapObjectUrl(url !== dataUrl ? url : null);
                        return;
                    }
                    try {
                        revokeHeatmapObjectUrl(agriHeatmapBlobUrlRef.current);
                        agriHeatmapBlobUrlRef.current = url.startsWith("blob:") ? url : null;
                        addImageLayer(url);
                    } catch (e) {
                        console.warn("[agri-heatmap] image overlay failed", e);
                        if (url !== dataUrl) {
                            try {
                                addImageLayer(dataUrl);
                            } catch (e2) {
                                console.warn("[agri-heatmap] image overlay failed", e2);
                            }
                        }
                    }
                };

                // Prefer canvas.toBlob → createObjectURL (MapLibre fetches via connect-src).
                const img = new Image();
                img.onload = () => {
                    if (gen !== agriHeatmapApplyGenRef.current) return;
                    const canvas = document.createElement("canvas");
                    canvas.width = img.naturalWidth || img.width;
                    canvas.height = img.naturalHeight || img.height;
                    const ctx = canvas.getContext("2d", { willReadFrequently: true });
                    if (!ctx || !canvas.width || !canvas.height) {
                        try {
                            commitUrl(dataUrlToObjectUrl(dataUrl));
                        } catch {
                            commitUrl(dataUrl);
                        }
                        return;
                    }
                    ctx.drawImage(img, 0, 0);
                    void canvasToObjectUrl(canvas)
                        .then((blobUrl) => commitUrl(blobUrl))
                        .catch(() => {
                            try {
                                commitUrl(dataUrlToObjectUrl(dataUrl));
                            } catch {
                                commitUrl(dataUrl);
                            }
                        });
                };
                img.onerror = () => {
                    if (gen !== agriHeatmapApplyGenRef.current) return;
                    try {
                        commitUrl(dataUrlToObjectUrl(dataUrl));
                    } catch (e) {
                        console.warn("[agri-heatmap] image overlay failed", e);
                        try {
                            commitUrl(dataUrl);
                        } catch (e2) {
                            console.warn("[agri-heatmap] image overlay failed", e2);
                        }
                    }
                };
                img.src = dataUrl;
            } else if (hm.pixelCount > 0) {
                const msg = "色斑有像素但色膜未生成（canvas/clip 失败）";
                console.warn("[agri-heatmap]", msg, {
                    index: hm.index,
                    pixelCount: hm.pixelCount,
                    fromLonLat: !!hm.fromLonLat,
                    geojsonN: hm.geojson?.features?.length ?? 0,
                    hasDataUrl: !!hm.dataUrl,
                    clippedHasContent: heatmapImageHasContent(clippedImg),
                });
                toast.message(msg, {
                    description: `${AGRI_MODE_LABELS[hm.index]} · ${hm.pixelCount} px`,
                });
            }
        },
        [],
    );

    // Add field polygon (and NDVI if active) to map.
    // When agri 色斑图 is active: field fill opacity 0; continuous image film sits above fill, below outline.
    const setupMapLayers = useCallback((map: maplibregl.Map) => {
        const f = fieldRef.current;
        if (!f?.geom) return;

        const hm = agriHeatmapRef.current;
        // Remove existing layers/source first to avoid stale state / wrong z-order
        try {
            clearAgriHeatmapLayers(map);
        } catch { /* ignore */ }
        if (map.getLayer("field-outline")) map.removeLayer("field-outline");
        if (map.getLayer("field-fill")) map.removeLayer("field-fill");
        if (map.getSource("field-polygon")) map.removeSource("field-polygon");

        map.addSource("field-polygon", {
            type: "geojson",
            data: { type: "Feature", properties: {}, geometry: f.geom },
        });
        // Transparent fill while heatmap is on — outline stays for boundary.
        map.addLayer({
            id: "field-fill",
            type: "fill",
            source: "field-polygon",
            paint: {
                "fill-color": tokenColor("--map-field-stroke"),
                "fill-opacity": hm ? 0 : 0.2,
            },
        });
        map.addLayer({
            id: "field-outline",
            type: "line",
            source: "field-polygon",
            paint: {
                "line-color": tokenColor("--map-field-stroke"),
                "line-width": 3.5,
                "line-opacity": 1,
            },
        });

        // Re-add index overlay if active
        const layer = indexLayerRef.current;
        if (layer?.tile_url) {
            addIndexOverlay(map, layer, f, activeIndexTypeRef.current);
        }

        if (hm) {
            const fieldGeom = f.geom as GeoJSON.Polygon | GeoJSON.MultiPolygon;
            applyAgriHeatmapToMap(map, hm, fieldGeom, "field-outline");
        }
    }, [applyAgriHeatmapToMap, clearAgriHeatmapLayers]);

    // 地块应位于可见地图中，右侧分析面板占用的区域不参与居中计算。
    const fitFieldInView = useCallback((map: maplibregl.Map) => {
        if (!field?.geom) return;
        const bounds = new maplibregl.LngLatBounds();
        getAllCoords(field.geom).forEach(([lng, lat]) => bounds.extend([lng, lat]));
        if (bounds.isEmpty()) return;
        const width = map.getContainer().clientWidth;
        const height = map.getContainer().clientHeight;
        const mobile = window.matchMedia("(max-width: 639px)").matches;
        map.fitBounds(bounds, {
            padding: {
                top: 80,
                bottom: sidebarOpen && mobile ? Math.round(height * 0.52) + 28 : 48,
                left: 56,
                right: sidebarOpen && !mobile ? Math.min(panelWidthPx + 48, Math.round(width * 0.65)) : 40,
            },
            maxZoom: 17,
            duration: 0,
        });
    }, [field, sidebarOpen, panelWidthPx]);

    // Callback from BaseMap when ready
    const handleMapReady = useCallback(
        (map: maplibregl.Map) => {
            setMapInstance(map);
            setupMapLayers(map);

            fitFieldInView(map);
        },
        [setupMapLayers, fitFieldInView],
    );

    // When field arrives after the map is already ready, draw/fit the boundary
    // (handleMapReady often runs with field still null — race with loadField).
    useEffect(() => {
        if (!mapInstance || !field?.geom) return;
        if (!mapInstance.isStyleLoaded()) return;
        setupMapLayers(mapInstance);
        fitFieldInView(mapInstance);
        // 观察实际地图容器，导航侧栏切换与窗口缩放均能保持地块可见。
        const observer = new ResizeObserver(() => {
            mapInstance.resize();
            fitFieldInView(mapInstance);
        });
        observer.observe(mapInstance.getContainer());
        return () => observer.disconnect();
    }, [field, mapInstance, setupMapLayers, fitFieldInView]);

    // Location search
    const handleLocationSelect = useCallback(
        (lngLat: [number, number]) => {
            mapInstance?.flyTo({ center: lngLat, zoom: 16, duration: 1500 });
        },
        [mapInstance],
    );

    // Map style change - poll isStyleLoaded() to re-add layers reliably
    const handleStyleChange = useCallback(
        (styleId: MapStyleId) => {
            if (!mapInstance) return;
            const styleDef = MAP_STYLES.find((s) => s.id === styleId);
            if (!styleDef) return;

            mapInstance.setStyle(styleDef.style);
            setMapStyle(styleId);

            // Poll until the new style is loaded, then re-add our layers
            const poll = setInterval(() => {
                try {
                    if (mapInstance.isStyleLoaded()) {
                        clearInterval(poll);
                        setupMapLayers(mapInstance);
                    }
                } catch {
                    clearInterval(poll);
                }
            }, 50);

            // Safety: stop polling after 5s
            setTimeout(() => clearInterval(poll), 5000);
        },
        [mapInstance, setupMapLayers],
    );

    // Index tile overlay management
    useEffect(() => {
        const map = mapInstance;
        if (!map || !map.isStyleLoaded()) return;
        removeAllIndexOverlays(map);
        if (indexLayer?.tile_url && field) {
            addIndexOverlay(map, indexLayer, field, activeIndexType);
        }
    }, [indexLayer, field, mapInstance, activeIndexType]);

    // Agri 色斑图 — canvas image film ONLY (blob:/data:); never GeoJSON fill (white seams)
    useEffect(() => {
        const map = mapInstance;
        if (!map || !map.isStyleLoaded()) return;
        try {
            clearAgriHeatmapLayers(map);
        } catch { /* ignore */ }

        // Hide solid green fill whenever 色斑 is active so sparse pixels stay visible
        try {
            if (map.getLayer("field-fill")) {
                map.setPaintProperty("field-fill", "fill-opacity", agriHeatmap ? 0 : 0.2);
            }
        } catch { /* ignore */ }

        if (!agriHeatmap) return;

        const fieldGeom = fieldRef.current?.geom as
            | GeoJSON.Polygon
            | GeoJSON.MultiPolygon
            | undefined;
        const beforeId = map.getLayer("field-outline") ? "field-outline" : undefined;
        applyAgriHeatmapToMap(map, agriHeatmap, fieldGeom, beforeId);

        return () => {
            try {
                clearAgriHeatmapLayers(map);
                if (map.getLayer("field-fill")) {
                    map.setPaintProperty("field-fill", "fill-opacity", 0.2);
                }
            } catch { /* ignore */ }
        };
    }, [agriHeatmap, mapInstance, field, applyAgriHeatmapToMap, clearAgriHeatmapLayers]);

    const handleShowLayer = useCallback((layer: RasterLayer | null, indexType: IndexType) => {
        setIndexLayer(layer);
        setActiveIndexType(indexType);
    }, []);

    const handleSave = async () => {
        setSaving(true);
        try {
            const data: any = {};
            if (editName.trim() !== field?.name) data.name = editName.trim();
            if (editCropType.trim() !== (field?.crop_type || ""))
                data.crop_type = editCropType.trim() || null;
            if (editSeason.trim() !== (field?.season || ""))
                data.season = editSeason.trim() || null;
            if (editGeom && JSON.stringify(editGeom) !== JSON.stringify(field?.geom))
                data.geom = editGeom;

            const updated = await fieldsApi.update(fieldId, data);
            setField(updated);
            setEditing(false);
            toast.success(t("fieldUpdated"));
        } catch (err: any) {
            toast.error(err.detail || t("failedUpdate"));
        } finally {
            setSaving(false);
        }
    };

    const handleDelete = async () => {
        const ok = await confirm({
            title: t("delete"),
            description: t("confirmDelete"),
            confirmLabel: t("delete"),
            variant: "destructive",
        });
        if (!ok) return;
        try {
            await fieldsApi.delete(fieldId);
            toast.success(t("fieldDeleted"));
            router.push(`/farms/${farmId}`);
        } catch (err: any) {
            toast.error(err.detail || t("failedDelete"));
        }
    };

    if (loading) {
        return (
            <div className="relative h-full w-full overflow-hidden">
                <Skeleton className="absolute inset-0 rounded-none" />
                <div className="absolute top-4 left-4 flex gap-2">
                    <Skeleton className="h-10 w-32 rounded-lg" />
                    <Skeleton className="h-10 w-28 rounded-lg" />
                </div>
                <Skeleton className="absolute top-4 right-4 bottom-4 w-[var(--panel-w)] rounded-xl" />
            </div>
        );
    }

    if (!field) return null;

    return (
        <div
            className="relative h-full w-full overflow-hidden"
            style={{ "--panel-w": `${panelWidthPx}px` } as React.CSSProperties}
        >
            {/* Full-screen map */}
            <div className="absolute inset-0">
                {editing ? (
                    <DrawMap
                        initialGeometry={field.geom}
                        onGeometryChange={(g) => setEditGeom(g)}
                        onMapReady={(m) => setMapInstance(m)}
                    />
                ) : (
                    <BaseMap onMapReady={handleMapReady} />
                )}
            </div>

            {/* Top-left: Back + Style Switcher + Search */}
            <div className="absolute top-4 left-4 z-20 flex items-center gap-2">
                <Link
                    href={`/farms/${farmId}`}
                    className={cn("inline-flex h-10 items-center gap-1.5 rounded-lg px-3.5 text-[13px] font-medium text-foreground transition-colors hover:bg-surface-3", MAP_CHROME)}
                >
                    <ArrowLeft className="h-4 w-4" />
                    {t("backToFarm")}
                </Link>
                <MapStyleSwitcher currentStyle={mapStyle} onStyleChange={handleStyleChange} />
                <LocationSearch onSelect={handleLocationSelect} />
            </div>

            {/* Index / agri 色斑 Legend - bottom-left */}
            {(agriHeatmap || indexLayer) && (
                <div className="absolute bottom-6 left-4 z-20 transition-opacity duration-200">
                    {agriHeatmap ? (
                        <AgriHeatmapLegend heatmap={agriHeatmap} />
                    ) : indexLayer ? (
                        <NdviLegend layer={indexLayer} indexType={activeIndexType} />
                    ) : null}
                </div>
            )}

            {/* Bottom-centre stack: index selector above, provenance below.
                One container so the two can never overlap, and both stay
                centred on the visible map rather than on the viewport. */}
            <div
                className="absolute bottom-6 z-20 flex max-w-[calc(100%-2rem)] flex-col items-center gap-2"
                style={{
                    left: sidebarOpen ? "calc(50% - var(--panel-w) / 2)" : "50%",
                    transform: "translateX(-50%)",
                }}
            >
                {activeTab === "ndvi" && parseAgriLandId(field.tags) && (
                    <div className={cn("flex gap-1 rounded-lg p-1", MAP_CHROME)}>
                        {AGRI_PRIMARY_MODES.map((mode) => (
                            <Button
                                key={mode}
                                variant={mode === agriHeatMode ? "default" : "ghost"}
                                size="sm"
                                onClick={() => setAgriHeatMode(mode)}
                                className="px-3 text-xs font-medium"
                                title={
                                    mode === "drought"
                                        ? "干旱 NDDI（Gu et al. 2007）"
                                        : mode === "flood"
                                          ? "洪涝 S1 VV/VH 阈值"
                                          : AGRI_MODE_LABELS[mode]
                                }
                            >
                                {AGRI_MODE_LABELS[mode]}
                            </Button>
                        ))}
                    </div>
                )}

                {activeTab === "ndvi" && !parseAgriLandId(field.tags) && availableTypes.length > 0 && (
                    <div className={cn("flex gap-1 rounded-lg p-1", MAP_CHROME)}>
                        {availableTypes.map((idx) => (
                            <Button
                                key={idx}
                                variant={idx === activeIndexType ? "default" : "ghost"}
                                size="sm"
                                onClick={() => { if (idx === activeIndexType) return; setIndexLayer(null); setActiveIndexType(idx); }}
                                className="px-3 text-xs font-medium"
                            >
                                {INDEX_CONFIG[idx].label}
                            </Button>
                        ))}
                    </div>
                )}

                {/* Satellite, date, cloud cover and colormap in one mono
                    line: the reproducibility claim, made visible where the
                    user is actually looking. */}
                {agriHeatmap && (
                    <div
                        className="max-w-full rounded-lg border border-border px-3.5 py-2"
                        style={{ background: "hsl(var(--map-scrim) / 0.82)" }}
                    >
                        <p className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground [overflow-wrap:anywhere]">
                            <Satellite className="h-3 w-3 shrink-0" aria-hidden="true" />
                            {[
                                agriHeatmap.index === "flood" ? "Sentinel-1" : "Sentinel-2",
                                AGRI_MODE_LABELS[agriHeatmap.index],
                                agriHeatmap.mean != null ? `均≈${agriHeatmap.mean.toFixed(2)}` : null,
                                `${agriHeatmap.pixelCount} px`,
                            ]
                                .filter(Boolean)
                                .join(" · ")}
                        </p>
                    </div>
                )}

                {!agriHeatmap && indexLayer && (
                    <div
                        className="max-w-full rounded-lg border border-border px-3.5 py-2"
                        style={{ background: "hsl(var(--map-scrim) / 0.82)" }}
                    >
                        <p className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground [overflow-wrap:anywhere]">
                            <Satellite className="h-3 w-3 shrink-0" aria-hidden="true" />
                            {[
                                SATELLITE_LABELS[indexLayer.satellite] ?? indexLayer.satellite,
                                indexLayer.date,
                                indexLayer.params_json?.cloud_cover != null
                                    ? t("cloudCover", { percent: Math.round(indexLayer.params_json.cloud_cover) })
                                    : null,
                                INDEX_CONFIG[activeIndexType].colormap,
                            ]
                                .filter(Boolean)
                                .join(" · ")}
                        </p>
                    </div>
                )}
            </div>

            {/* Sidebar toggle button - always visible */}
            <button
                type="button"
                onClick={() => setSidebarOpen(!sidebarOpen)}
                className={cn("absolute top-4 right-4 sm:right-[var(--toggle-right)] z-20 rounded-xl p-2.5 transition-all hover:bg-surface-3", MAP_CHROME)}
                style={{ "--toggle-right": sidebarOpen ? "calc(var(--panel-w) + 2rem)" : "1rem" } as React.CSSProperties}
                title={sidebarOpen ? "Hide panel (⌘.)" : "Show panel (⌘.)"}
            >
                {sidebarOpen ? (
                    <PanelRightClose className="h-4 w-4 text-foreground" />
                ) : (
                    <PanelRight className="h-4 w-4 text-foreground" />
                )}
            </button>

            {/* Floating tabbed sidebar - right */}
            <div
                className={cn(
                    "absolute right-4 bottom-4 z-10 h-[52%] w-[calc(100%-2rem)] sm:top-4 sm:h-auto sm:w-[var(--panel-w)] max-w-[calc(100%-2rem)]",
                    !isResizingPanel && "transition-transform duration-300 ease-in-out",
                    sidebarOpen ? "translate-x-0" : "translate-x-[calc(100%+1rem)]",
                )}
            >
                {/* Left-edge drag handle — always-visible short gray grip (centered) */}
                <div
                    role="separator"
                    aria-orientation="vertical"
                    aria-label="Resize panel"
                    title="Drag to resize · double-click resets to default"
                    onMouseDown={handlePanelResizeStart}
                    onDoubleClick={handlePanelResizeReset}
                    className="group/resize absolute left-0 top-0 bottom-0 z-20 hidden sm:flex w-3 -translate-x-1/2 cursor-col-resize touch-none items-center justify-center"
                >
                    <div
                        className={cn(
                            "pointer-events-none h-14 w-1 rounded-full bg-muted-foreground/50 shadow-sm",
                            "transition-colors group-hover/resize:bg-muted-foreground/75",
                            isResizingPanel && "bg-muted-foreground/85",
                        )}
                    />
                </div>
                <div className={cn("flex h-full flex-col overflow-hidden rounded-xl", MAP_CHROME)}>
                    <Tabs defaultValue="info" value={activeTab} onValueChange={setActiveTab} className="flex flex-col flex-1 overflow-hidden">
                        {/* 页签保持紧凑单行，窄侧栏允许横向滚动，不挤占内容高度。 */}
                        <TabsList variant="underline" className="flex shrink-0 flex-nowrap gap-3 overflow-x-auto bg-background/95 px-3 py-1">
                            <TabsTrigger
                                value="info"
                                variant="underline"
                                className="shrink-0 pb-2 pt-2 text-[11px]"
                            >
                                {t("tabInfo")}
                            </TabsTrigger>
                            <TabsTrigger
                                value="ndvi"
                                variant="underline"
                                className="shrink-0 pb-2 pt-2 text-[11px]"
                            >
                                {t("tabNdvi")}
                            </TabsTrigger>
                            <TabsTrigger
                                value="weather"
                                variant="underline"
                                className="shrink-0 pb-2 pt-2 text-[11px]"
                            >
                                {t("tabWeather")}
                            </TabsTrigger>
                            <TabsTrigger
                                value="soil"
                                variant="underline"
                                className="shrink-0 pb-2 pt-2 text-[11px]"
                            >
                                {t("tabSoil")}
                            </TabsTrigger>
                            <TabsTrigger
                                value="alerts"
                                variant="underline"
                                className="shrink-0 pb-2 pt-2 text-[11px]"
                            >
                                <span className="relative">
                                    {t("tabAlerts")}
                                    {openAlertCount > 0 && (
                                        <span className="absolute -top-1 -right-2 h-2 w-2 rounded-full bg-destructive" />
                                    )}
                                </span>
                            </TabsTrigger>
                            <TabsTrigger
                                value="land-report"
                                variant="underline"
                                className="shrink-0 pb-2 pt-2 text-[11px]"
                            >
                                {t("tabLandReport")}
                            </TabsTrigger>
                            <TabsTrigger
                                value="scouting"
                                variant="underline"
                                className="shrink-0 pb-2 pt-2 text-[11px]"
                            >
                                {t("tabScouting")}
                            </TabsTrigger>
                            <TabsTrigger
                                value="share"
                                variant="underline"
                                className="shrink-0 pb-2 pt-2 text-[11px]"
                            >
                                {t("tabShare")}
                            </TabsTrigger>
                        </TabsList>

                        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain [scrollbar-gutter:stable]">
                            {editing ? (
                                <div className="p-4">
                                    <h3 className="text-sm font-semibold mb-3">{t("editField")}</h3>
                                    <div className="space-y-3">
                                        <div className="space-y-1.5">
                                            <Label htmlFor="edit-name" className="text-xs">
                                                {t("name")}
                                            </Label>
                                            <Input
                                                id="edit-name"
                                                value={editName}
                                                onChange={(e) => setEditName(e.target.value)}
                                                className="h-9"
                                            />
                                        </div>
                                        <div className="space-y-1.5">
                                            <Label htmlFor="edit-crop" className="text-xs">
                                                {t("cropType")}
                                            </Label>
                                            <CropSelect
                                                id="edit-crop"
                                                value={editCropType}
                                                onChange={setEditCropType}
                                                placeholder={t("cropType")}
                                            />
                                        </div>
                                        <div className="space-y-1.5">
                                            <Label htmlFor="edit-season" className="text-xs">
                                                {t("season")}
                                            </Label>
                                            <Input
                                                id="edit-season"
                                                value={editSeason}
                                                onChange={(e) => setEditSeason(e.target.value)}
                                                className="h-9"
                                            />
                                        </div>
                                        <p className="text-xs text-muted-foreground">
                                            {t("editHint")}
                                        </p>
                                        <div className="flex gap-2">
                                            <Button
                                                className="flex-1 h-9"
                                                onClick={handleSave}
                                                disabled={saving}
                                            >
                                                {saving ? (
                                                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                                ) : (
                                                    <Save className="h-4 w-4 mr-2" />
                                                )}
                                                {t("save")}
                                            </Button>
                                            <Button
                                                variant="outline"
                                                className="flex-1 h-9"
                                                onClick={() => setEditing(false)}
                                            >
                                                <X className="h-4 w-4 mr-2" /> {t("cancel")}
                                            </Button>
                                        </div>
                                    </div>
                                </div>
                            ) : (
                                <>
                                    <TabsContent value="info" className="mt-0 p-3 space-y-3">
                                        {/* Field header card */}
                                        <div className="rounded-lg bg-card p-3">
                                            <h2 className="text-sm font-semibold">{field.name}</h2>
                                            <p className="text-xs text-muted-foreground mt-0.5">
                                                {field.area_ha != null ? formatAreaMu(field.area_ha) : ""}
                                                {field.crop_type && field.crop_type !== "unknown" && ` · ${field.crop_type}`}
                                                {field.season && ` · ${field.season}`}
                                            </p>
                                        </div>

                                        {/* Field details card */}
                                        <div className="rounded-lg bg-card p-3">
                                            <dl className="grid grid-cols-[repeat(auto-fit,minmax(120px,1fr))] gap-3">
                                                <InfoRow label={t("name")} value={field.name} />
                                                <InfoRow
                                                    label={t("area")}
                                                    value={
                                                        formatAreaMu(field.area_ha)
                                                    }
                                                />
                                                <InfoRow
                                                    label={t("cropType")}
                                                    value={field.crop_type && field.crop_type !== "unknown" ? field.crop_type : t("cropUnset")}
                                                />
                                                <InfoRow
                                                    label={t("season")}
                                                    value={field.season || "-"}
                                                />
                                                <InfoRow
                                                    label={t("tags")}
                                                    value={field.tags?.join(", ") || "-"}
                                                />
                                                <InfoRow
                                                    label={t("created")}
                                                    value={new Date(
                                                        field.created_at,
                                                    ).toLocaleDateString()}
                                                />
                                                <InfoRow
                                                    label={t("updated")}
                                                    value={new Date(
                                                        field.updated_at,
                                                    ).toLocaleDateString()}
                                                />
                                            </dl>
                                        </div>

                                        {/* Actions */}
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            className="w-full"
                                            onClick={() => setEditing(true)}
                                        >
                                            <Edit2 className="h-4 w-4 mr-2" />
                                            {t("edit")}
                                        </Button>

                                        <div className="rounded-lg border border-destructive/20 bg-destructive/5 shadow-sm p-3">
                                            <div className="flex items-center justify-between">
                                                <div>
                                                    <p className="text-xs font-medium text-destructive">
                                                        {t("delete")}
                                                    </p>
                                                    <p className="text-[11px] text-muted-foreground mt-0.5">
                                                        {t("confirmDelete")}
                                                    </p>
                                                </div>
                                                <Button
                                                    variant="destructive"
                                                    size="sm"
                                                    className="shrink-0 px-3 text-xs"
                                                    onClick={handleDelete}
                                                >
                                                    <Trash2 className="h-3 w-3 mr-2" />
                                                    {t("delete")}
                                                </Button>
                                            </div>
                                        </div>
                                    </TabsContent>

                                    <TabsContent
                                        value="ndvi"
                                        forceMount
                                        className="mt-0 p-3 data-[state=inactive]:hidden data-[state=inactive]:!hidden"
                                    >
                                        <NdviTab
                                            fieldId={fieldId}
                                            fieldTags={field?.tags ?? null}
                                            cropType={field?.crop_type}
                                            areaHa={field?.area_ha}
                                            onShowLayer={handleShowLayer}
                                            activeIndexOverride={activeIndexType}
                                            onActiveIndexChange={setActiveIndexType}
                                            onDataLoaded={refreshAvailableTypes}
                                            onAgriHeatmapChange={setAgriHeatmap}
                                            agriHeatMode={agriHeatMode}
                                            onAgriHeatModeChange={setAgriHeatMode}
                                            agriHeatmapEnabled={activeTab === "ndvi"}
                                        />
                                    </TabsContent>

                                    <TabsContent value="alerts" className="mt-0 p-3">
                                        <AlertsTab fieldId={fieldId} onOpenCountChange={setOpenAlertCount} />
                                    </TabsContent>

                                    <TabsContent value="land-report" className="mt-0">
                                        <LandReportTab
                                            fieldId={fieldId}
                                            cropType={field?.crop_type}
                                            onCropBound={(key) => {
                                                setField((prev) => (prev ? { ...prev, crop_type: key } : prev));
                                            }}
                                        />
                                    </TabsContent>

                                    <TabsContent value="scouting" className="mt-0">
                                        <ScoutingTab fieldId={fieldId} mapInstance={mapInstance} activeTab={activeTab} />
                                    </TabsContent>

                                    <TabsContent value="weather" className="mt-0 p-3">
                                        <WeatherTab fieldId={fieldId} />
                                    </TabsContent>

                                    <TabsContent value="soil" className="mt-0 p-3">
                                        <SoilTab fieldId={fieldId} mapInstance={mapInstance} activeTab={activeTab} />
                                    </TabsContent>

                                    <TabsContent value="share" className="mt-0">
                                        <ShareTab fieldId={fieldId} />
                                    </TabsContent>
                                </>
                            )}
                        </div>
                    </Tabs>
                </div>
            </div>
        </div>
    );
}

function InfoRow({ label, value }: { label: string; value: string }) {
    return (
        <div className="min-w-0 rounded-lg bg-muted/35 px-3 py-2.5">
            <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
            <dd className="mt-1 break-words text-xs font-medium leading-relaxed">{value}</dd>
        </div>
    );
}

function getAllCoords(geom: GeoJSON.Geometry): number[][] {
    if (geom.type === "Point") return [geom.coordinates as number[]];
    if (geom.type === "Polygon") return (geom.coordinates as number[][][]).flat();
    if (geom.type === "MultiPolygon") return (geom.coordinates as number[][][][]).flat(2);
    return [];
}

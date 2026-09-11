/**
 * OpenFarm API client.
 *
 * JWT / X-Org-Id are optional after OpenFarm auth removal (API AUTH_DISABLED).
 * All methods return typed responses; throws on HTTP errors.
 */

/**
 * Resolve API base URL.
 * In the browser, prefer same-origin `/v1` (Next rewrite → INTERNAL_API_URL) when
 * NEXT_PUBLIC_API_URL points at localhost:8000 — avoids ERR_CONNECTION_REFUSED
 * when the API port is not published on the host.
 */
export function getApiBase(): string {
    const raw = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/v1";
    if (typeof window !== "undefined") {
        if (raw.startsWith("/")) return raw.replace(/\/$/, "") || "/v1";
        try {
            const u = new URL(raw);
            if (
                (u.hostname === "localhost" || u.hostname === "127.0.0.1") &&
                (raw.includes(":8000"))
            ) {
                return "/v1";
            }
        } catch {
            /* keep raw */
        }
    }
    return raw.replace(/\/$/, "");
}

/** Auth/org headers removed — API AUTH_DISABLED; no JWT / X-Org-Id. */
export function getOrgId(): string | null {
    return null;
}

export function setOrgId(_orgId: string) {
    /* no-op: org localStorage removed */
}

export class ApiError extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
        super(detail);
        this.status = status;
        this.detail = detail;
    }
}

/** Core fetch wrapper - adds Authorization + X-Org-Id headers. */
async function apiFetch<T>(
    path: string,
    opts: RequestInit & { orgId?: string | null; skipOrg?: boolean } = {},
): Promise<T> {
    const { orgId: _orgId, skipOrg: _skipOrg, ...fetchOpts } = opts;
    const headers: Record<string, string> = {
        ...(fetchOpts.headers as Record<string, string>),
    };

    // Don't set Content-Type for FormData
    if (!(fetchOpts.body instanceof FormData) && !headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
    }

    const res = await fetch(`${getApiBase()}${path}`, { ...fetchOpts, headers });

    if (!res.ok) {
        let detail = res.statusText;
        try {
            const body = await res.json();
            detail = body.detail || JSON.stringify(body);
        } catch { }
        throw new ApiError(res.status, detail);
    }

    if (res.status === 204) return undefined as T;
    return res.json();
}

/** Authenticated binary/CSV download → triggers browser save. */
async function apiDownload(path: string, fallbackName: string): Promise<void> {
    const res = await fetch(`${getApiBase()}${path}`);
    if (!res.ok) {
        let detail = res.statusText;
        try {
            const body = await res.json();
            detail = body.detail || JSON.stringify(body);
        } catch { /* ignore */ }
        throw new ApiError(res.status, detail);
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename="([^"]+)"/.exec(cd);
    const filename = m?.[1] || fallbackName;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

// ── Types ────────────────────────────────────────────────────────────

export interface Paginated<T> {
    items: T[];
    total: number;
    limit: number;
    offset: number;
}

export interface Org {
    id: string;
    name: string;
    created_by: string;
    created_at: string;
}

export interface OrgDetail extends Org {
    member_count: number;
    farm_count: number;
    field_count: number;
}

/** Blast radius of a workspace deletion, shown before the owner confirms. */
export interface OrgDeletionImpact {
    farm_count: number;
    field_count: number;
    history_months: number;
    scouting_count: number;
}

export interface OrgBrief {
    id: string;
    name: string;
    role: string;
}

export interface UserMe {
    id: string;
    email: string;
    name: string;
    avatar_url: string | null;
    created_at: string;
    orgs: OrgBrief[];
}

export interface Member {
    id: string;
    user_id: string;
    email: string;
    name: string;
    role: string;
    created_at: string;
}

export interface Invite {
    id: string;
    org_id: string;
    email: string;
    role: string;
    status: string;
    invited_by_name: string | null;
    created_at: string;
}

export interface Farm {
    id: string;
    org_id: string;
    name: string;
    country: string | null;
    region: string | null;
    timezone: string | null;
    created_at: string;
    updated_at: string;
}

export interface Field {
    id: string;
    org_id: string;
    farm_id: string;
    name: string;
    geom: GeoJSON.Geometry | null;
    area_ha: number | null;
    crop_type: string | null;
    season: string | null;
    tags: string[] | null;
    created_by: string;
    created_at: string;
    updated_at: string;
}

export interface FieldImportResult {
    imported: number;
    errors: string[];
}

// ── Index Configuration ──────────────────────────────────────────────

export type IndexType = "NDVI" | "EVI" | "SAVI" | "NDWI" | "NDMI" | "NDRE" | "CIRE" | "MNDWI" | "VV" | "VH";

export interface IndexConfig {
    label: string;
    colormap: string;
    rescaleMin: number;
    rescaleMax: number;
    /** CSS background for legends - references the ramp tokens so the
        legend and the TiTiler colormap can never drift apart. */
    gradient: string;
    threshold: number;
}

export const INDEX_CONFIG: Record<IndexType, IndexConfig> = {
    NDVI: {
        label: "NDVI",
        colormap: "rdylgn",
        rescaleMin: -0.2,
        rescaleMax: 0.9,
        gradient: "var(--ramp-vegetation)",
        threshold: 0.3,
    },
    EVI: {
        label: "EVI",
        colormap: "rdylgn",
        // Wider than historical 0.8 — EVI often exceeds 1.0; avoid chart maxing
        rescaleMin: -0.2,
        rescaleMax: 1.2,
        gradient: "var(--ramp-vegetation)",
        threshold: 0.2,
    },
    SAVI: {
        label: "SAVI",
        colormap: "rdylgn",
        rescaleMin: -0.2,
        rescaleMax: 0.8,
        gradient: "var(--ramp-vegetation)",
        threshold: 0.25,
    },
    NDWI: {
        label: "NDWI",
        colormap: "rdbu",
        rescaleMin: -0.5,
        rescaleMax: 0.5,
        gradient: "var(--ramp-water)",
        threshold: 0.0,
    },
    NDMI: {
        label: "NDMI",
        colormap: "rdylgn",
        rescaleMin: -0.5,
        rescaleMax: 0.5,
        gradient: "var(--ramp-vegetation)",
        threshold: 0.0,
    },
    NDRE: {
        label: "NDRE",
        colormap: "rdylgn",
        rescaleMin: -0.2,
        rescaleMax: 0.8,
        gradient: "var(--ramp-vegetation)",
        threshold: 0.2,
    },
    CIRE: {
        label: "CIRE",
        colormap: "rdylgn",
        rescaleMin: 0,
        rescaleMax: 1.5,
        gradient: "var(--ramp-vegetation)",
        threshold: 0.2,
    },
    MNDWI: {
        label: "MNDWI",
        colormap: "rdbu",
        rescaleMin: -0.5,
        rescaleMax: 0.5,
        gradient: "var(--ramp-water)",
        threshold: 0.0,
    },
    VV: {
        label: "VV",
        colormap: "viridis",
        // Sentinel-1 σ⁰ approx in dB (typical agri range)
        rescaleMin: -25,
        rescaleMax: 0,
        gradient: "var(--ramp-water)",
        threshold: -18,
    },
    VH: {
        label: "VH",
        colormap: "viridis",
        rescaleMin: -30,
        rescaleMax: -5,
        gradient: "var(--ramp-water)",
        threshold: -22,
    },
};

export const ALL_INDEX_TYPES: IndexType[] = [
    "NDVI",
    "EVI",
    "SAVI",
    "NDWI",
    "NDMI",
    "NDRE",
    "CIRE",
    "MNDWI",
    "VV",
    "VH",
];

// ── Monitoring Types ─────────────────────────────────────────────────

export interface RasterLayer {
    id: string;
    field_id: string;
    layer_type: string;
    satellite: string;
    date: string;
    cog_uri: string;
    tile_url: string | null;
    min: number | null;
    max: number | null;
    params_json: Record<string, any> | null;
    provenance_json: Record<string, any> | null;
    created_at: string;
}

export interface FieldStat {
    id: string;
    field_id: string;
    date: string;
    mean: number | null;
    median: number | null;
    min: number | null;
    max: number | null;
    p10: number | null;
    p90: number | null;
    stddev: number | null;
    quality_score: number | null;
    created_at: string;
    /** Parcel or STAC cloud cover % when this point comes from agri scenes. */
    cloud_cover?: number | null;
    /** good | fair | bad; omitted for raw clear scenes. */
    decloud_quality?: string | null;
    decloud_reasons?: string[] | null;
    /** stac_direct or uncrtaints_decloud */
    product_source?: string | null;
    scene_id?: string | null;
    /** Unused decloud product for the same date (marked overlay, not the official line). */
    decloud_alt?: boolean;
    /** Fair/bad decloud: stored but may be unreliable. */
    may_be_unreliable?: boolean;
}

export interface NdviJob {
    id: string;
    field_id: string;
    type: string;
    status: string;
    progress_json: Record<string, any> | null;
    error: string | null;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
}

/** Open alert counts across the workspace, independent of paging. */
export interface AlertSummary {
    open_total: number;
    high: number;
    medium: number;
    low: number;
}

export interface Alert {
    id: string;
    field_id: string;
    date: string;
    severity: string;
    rule_name: string;
    rule_params_json: Record<string, any> | null;
    message: string;
    status: string;
    index_type: string | null;
    weather_context: Record<string, any> | null;
    soil_context: Record<string, any> | null;
    created_at: string;
    /** Resolved by the API at query time, not stored on the alert. Null
     *  when the field or farm has been deleted. */
    field_name: string | null;
    farm_id: string | null;
    farm_name: string | null;
}

// ── Scouting Types ───────────────────────────────────────────────

export interface ScoutingObservation {
    id: string;
    field_id: string;
    alert_id: string | null;
    geom_point: GeoJSON.Point | null;
    title: string;
    note: string | null;
    tags: string[] | null;
    photo_uri: string | null;
    weather_snapshot: Record<string, any> | null;
    created_by: string;
    created_at: string;
}

export interface ScoutingCreate {
    geom_point: { type: "Point"; coordinates: [number, number] };
    title: string;
    note?: string;
    tags?: string[];
    photo_uri?: string;
    alert_id?: string;
}

export interface ScoutingUpdate {
    title?: string;
    note?: string;
    tags?: string[];
}

// ── Weather Types ────────────────────────────────────────────────

export interface WeatherDaily {
    date: string;
    temperature_2m_min: number | null;
    temperature_2m_max: number | null;
    temperature_2m_mean: number | null;
    precipitation_sum: number | null;
    et0_fao_mm: number | null;
    soil_temperature_0cm: number | null;
    soil_temperature_6cm: number | null;
    soil_temperature_18cm: number | null;
    soil_temperature_54cm: number | null;
    soil_moisture_0_1cm: number | null;
    soil_moisture_1_3cm: number | null;
    soil_moisture_3_9cm: number | null;
    soil_moisture_9_27cm: number | null;
    soil_moisture_27_81cm: number | null;
    vapor_pressure_deficit: number | null;
    shortwave_radiation_sum: number | null;
    wind_speed_10m_max: number | null;
    cloud_cover_mean: number | null;
    gdd_daily: number | null;
    gdd_cumulative: number | null;
    water_balance_30d_mm: number | null;
    drought_index: number | null;
    heat_stress_flag: boolean | null;
}

export interface WeatherForecastDay {
    date: string;
    temperature_2m_min: number | null;
    temperature_2m_max: number | null;
    temperature_2m_mean: number | null;
    precipitation_sum: number | null;
    et0_fao_mm: number | null;
    wind_speed_10m_max: number | null;
    cloud_cover_mean: number | null;
}

export interface WeatherSummary {
    field_id: string;
    period_start: string;
    period_end: string;
    avg_temperature: number | null;
    min_temperature: number | null;
    max_temperature: number | null;
    total_precipitation: number | null;
    total_et0: number | null;
    water_deficit_mm: number | null;
    gdd_cumulative: number | null;
    frost_days: number;
    heat_stress_days: number;
    avg_soil_moisture_top: number | null;
    drought_index: number | null;
    data_source: string;
    last_updated: string | null;
}

export interface WeatherResponse {
    field_id: string;
    location: { latitude: number; longitude: number };
    data: WeatherDaily[];
    forecast: WeatherForecastDay[];
    summary: WeatherSummary;
}


// ── Upload Types ─────────────────────────────────────────────────

export interface PresignedUpload {
    upload_url: string;
    object_key: string;
}

// usersApi / orgsApi removed (auth/orgs dropped)

// ── Farms ────────────────────────────────────────────────────────────

export const farmsApi = {
    list: (limit = 50, offset = 0, q?: string) => {
        const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
        if (q && q.trim()) params.set("q", q.trim());
        return apiFetch<Paginated<Farm>>(`/farms?${params.toString()}`);
    },
    get: (farmId: string) => apiFetch<Farm>(`/farms/${farmId}`),
    create: (data: { name: string; country?: string; region?: string; timezone?: string }) =>
        apiFetch<Farm>("/farms", { method: "POST", body: JSON.stringify(data) }),
    update: (farmId: string, data: { name?: string; country?: string; region?: string; timezone?: string }) =>
        apiFetch<Farm>(`/farms/${farmId}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (farmId: string) =>
        apiFetch(`/farms/${farmId}`, { method: "DELETE" }),
    fields: (farmId: string, limit = 200, offset = 0) =>
        apiFetch<Paginated<Field>>(`/farms/${farmId}/fields?limit=${limit}&offset=${offset}`),
};

// ── Fields ───────────────────────────────────────────────────────────

export type BackfillPhase = "idle" | "stac" | "bridge" | "done";

export interface BackfillStatusResponse {
    field_id: string;
    has_active_backfill: boolean;
    pending_jobs: number;
    running_jobs: number;
    completed_jobs: number;
    failed_jobs: number;
    total_jobs: number;
    percent: number;
    phase: BackfillPhase | string;
    message: string;
}

export const fieldsApi = {
    get: (fieldId: string) => apiFetch<Field>(`/fields/${fieldId}`),
    create: (data: { farm_id: string; name: string; geom: any; crop_type: string; season?: string; tags?: string[] }) =>
        apiFetch<Field>("/fields", { method: "POST", body: JSON.stringify(data) }),
    update: (fieldId: string, data: { name?: string; geom?: any; crop_type?: string; season?: string; tags?: string[] }) =>
        apiFetch<Field>(`/fields/${fieldId}`, { method: "PUT", body: JSON.stringify(data) }),
    delete: (fieldId: string) =>
        apiFetch(`/fields/${fieldId}`, { method: "DELETE" }),
    import: (farmId: string, file: File) => {
        const formData = new FormData();
        formData.append("file", file);
        return apiFetch<FieldImportResult>(`/fields/import?farm_id=${farmId}`, { method: "POST", body: formData });
    },
    backfillIndices: (
        fieldId: string,
        opts: {
            months?: number;
            force?: boolean;
            date_from?: string;
            date_to?: string;
            growing_seasons?: GrowingSeasonWindow[];
            season_months?: number[];
        } = {},
    ) => {
        const months = opts.months ?? 24;
        const force = opts.force ?? true;
        const body: Record<string, unknown> = { months, force };
        if (opts.date_from) body.date_from = opts.date_from;
        if (opts.date_to) body.date_to = opts.date_to;
        if (opts.growing_seasons?.length) body.growing_seasons = opts.growing_seasons;
        if (opts.season_months?.length) body.season_months = opts.season_months;
        return apiFetch<{ field_id: string; status: string; message: string }>(
            `/fields/${fieldId}/backfill-indices`,
            { method: "POST", body: JSON.stringify(body) },
        );
    },
    backfillStatus: (fieldId: string) =>
        apiFetch<BackfillStatusResponse>(
            `/fields/${fieldId}/backfill-status`,
        ),
};

// ── Monitoring ───────────────────────────────────────────────────────

export const monitoringApi = {
    layers: (fieldId: string, type: IndexType = "NDVI", limit = 50) =>
        apiFetch<Paginated<RasterLayer>>(`/fields/${fieldId}/layers?type=${type}&limit=${limit}`),
    stats: (fieldId: string, type: IndexType = "NDVI", limit = 200) =>
        apiFetch<Paginated<FieldStat>>(`/fields/${fieldId}/stats?type=${type}&limit=${limit}`),
    layerTypes: (fieldId: string) =>
        apiFetch<string[]>(`/fields/${fieldId}/layers/types`),
};

// ── Jobs ─────────────────────────────────────────────────────────────

export const jobsApi = {
    createNdvi: (fieldId: string, dateFrom: string, dateTo: string) =>
        apiFetch<NdviJob>(`/fields/${fieldId}/jobs/ndvi`, {
            method: "POST",
            body: JSON.stringify({ date_from: dateFrom, date_to: dateTo }),
        }),
    createIndex: (fieldId: string, indexType: IndexType, dateFrom: string, dateTo: string, params?: { savi_l?: number }) =>
        apiFetch<NdviJob>(`/fields/${fieldId}/jobs/index`, {
            method: "POST",
            body: JSON.stringify({ index_type: indexType.toLowerCase(), date_from: dateFrom, date_to: dateTo, ...params }),
        }),
    get: (jobId: string) => apiFetch<NdviJob>(`/jobs/${jobId}`),
};

// ── Alerts ───────────────────────────────────────────────────────────


export interface GrowingSeasonWindow {
    label?: string;
    crop?: string;
    months?: number[];
    start_month?: number;
    end_month?: number;
}

export interface CropOption {
    key: string;
    name: string;
    name_zh: string;
    season_months: number[];
    peak_months: number[];
    season_label_zh: string;
}

export const cropsApi = {
    list: () => apiFetch<CropOption[]>("/crops"),
};

export type AssessmentDimensionKey =
    | "crop"
    | "soil"
    | "vigor"
    | "weather"
    | "wet_safety"
    | "drought_safety";

export interface AssessmentScorecardDimension {
    key: AssessmentDimensionKey;
    score: number;
    light: string | null;
    weight: string | null;
}

export interface AssessmentScorecard {
    job_id: string;
    overall: {
        score: number;
        grade: string | null;
        light: string | null;
        one_liner: string | null;
    };
    dimensions: AssessmentScorecardDimension[];
    confidence: { score: number } | null;
    generated_at: string | null;
}

export const assessmentApi = {
    generate: (fieldId: string, body?: { crop_type?: string }) =>
        apiFetch<NdviJob>(`/fields/${fieldId}/assessment-report`, {
            method: "POST",
            body: JSON.stringify(body || {}),
        }),
    latestMeta: (fieldId: string) =>
        apiFetch<NdviJob>(`/fields/${fieldId}/assessment-report/latest/meta`),
    latestScorecard: (fieldId: string) =>
        apiFetch<AssessmentScorecard>(
            `/fields/${fieldId}/assessment-report/latest/scorecard`,
        ),
    downloadLatest: async (fieldId: string) => {
        const res = await fetch(
            `${getApiBase()}/fields/${fieldId}/assessment-report/latest`,
        );
        if (!res.ok) {
            const detail = await res.text();
            throw new Error(detail || `Download failed (${res.status})`);
        }
        const blob = await res.blob();
        const cd = res.headers.get("Content-Disposition") || "";
        let filename = "选地分析报告.pdf";
        const m = /filename\*=UTF-8''([^;]+)|filename="([^"]+)"/i.exec(cd);
        if (m) {
            filename = decodeURIComponent(m[1] || m[2]);
        }
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    },
};


export const alertsApi = {
    list: (opts: { status?: string; severity?: string; limit?: number; offset?: number } = {}) => {
        const params = new URLSearchParams();
        if (opts.status) params.set("status", opts.status);
        if (opts.severity) params.set("severity", opts.severity);
        params.set("limit", String(opts.limit ?? 50));
        params.set("offset", String(opts.offset ?? 0));
        return apiFetch<Paginated<Alert>>(`/alerts?${params}`);
    },
    /** Open counts by severity across the workspace, for the summary cards. */
    summary: () => apiFetch<AlertSummary>("/alerts/summary"),
    listForField: (fieldId: string, limit = 50, indexType?: string) => {
        const params = new URLSearchParams({ field_id: fieldId, limit: String(limit) });
        if (indexType) params.set("index_type", indexType);
        return apiFetch<Paginated<Alert>>(`/alerts?${params}`);
    },
    listForFarm: (farmId: string, limit = 50) =>
        apiFetch<Paginated<Alert>>(`/alerts?farm_id=${farmId}&limit=${limit}`),
    update: (alertId: string, data: { status: string }) =>
        apiFetch<Alert>(`/alerts/${alertId}`, { method: "PATCH", body: JSON.stringify(data) }),
};

// ── Scouting ─────────────────────────────────────────────────────

export const scoutingApi = {
    list: (fieldId: string, limit = 50, offset = 0) =>
        apiFetch<Paginated<ScoutingObservation>>(
            `/fields/${fieldId}/scouting?limit=${limit}&offset=${offset}`,
        ),
    create: (fieldId: string, data: ScoutingCreate) =>
        apiFetch<ScoutingObservation>(`/fields/${fieldId}/scouting`, {
            method: "POST",
            body: JSON.stringify(data),
        }),
    update: (fieldId: string, obsId: string, data: ScoutingUpdate) =>
        apiFetch<ScoutingObservation>(`/fields/${fieldId}/scouting/${obsId}`, {
            method: "PATCH",
            body: JSON.stringify(data),
        }),
    delete: (fieldId: string, obsId: string) =>
        apiFetch(`/fields/${fieldId}/scouting/${obsId}`, { method: "DELETE" }),
};


// ── Weather ──────────────────────────────────────────────────────

export const weatherApi = {
    get: (fieldId: string, startDate: string, endDate: string, includeForecast = true) =>
        apiFetch<WeatherResponse>(
            `/fields/${fieldId}/weather?start_date=${startDate}&end_date=${endDate}&include_forecast=${includeForecast}`,
        ),
    summary: (fieldId: string, days = 30) =>
        apiFetch<WeatherSummary>(`/fields/${fieldId}/weather/summary?days=${days}`),
    backfill: (fieldId: string, days = 90) =>
        apiFetch<{ field_id: string; status: string; message: string }>(
            `/fields/${fieldId}/weather/backfill`,
            { method: "POST", body: JSON.stringify({ days }) },
        ),
};

// ── Share Types ──────────────────────────────────────────────────

export interface ShareLink {
    id: string;
    field_id: string;
    token: string;
    scope: string;
    expires_at: string | null;
    created_at: string;
}

export interface ShareStatPoint {
    date: string;
    mean: number | null;
    median?: number | null;
    min?: number | null;
    max?: number | null;
    p10?: number | null;
    p90?: number | null;
    stddev?: number | null;
    quality_score?: number | null;
    id?: string | null;
    field_id?: string | null;
    created_at?: string | null;
    cloud_cover?: number | null;
    decloud_quality?: string | null;
    decloud_reasons?: string[] | null;
    product_source?: string | null;
    scene_id?: string | null;
}

export interface ShareReport {
    field: {
        id: string;
        name: string;
        area_ha: number | null;
        crop_type: string | null;
        geom: GeoJSON.Geometry | null;
    };
    latest_layer: RasterLayer | null;
    layers_by_type: Record<string, RasterLayer>;
    available_index_types: string[];
    stats: ShareStatPoint[];
    stats_by_type: Record<string, ShareStatPoint[]>;
    alerts: Alert[];
    scouting: ScoutingObservation[];
    weather_summary: Record<string, any> | null;
    weather_data: WeatherDaily[];
    soil_summary: Record<string, any> | null;
    /** classic FieldStat/RasterLayer, agri parcel_scene_products, or both */
    rs_source?: "classic" | "agri" | "mixed" | null;
    agri_land_id?: string | null;
    agri_heatmap_available?: boolean;
}

export interface ShareAgriPixels {
    land_id: string;
    date: string;
    sensor: "S1" | "S2" | string;
    index_type: string;
    mean: number | null;
    pixel_count: number;
    pixels_lonlat: Array<{
        lon: number;
        lat: number;
        clear?: number;
        NDVI?: number;
        EVI?: number;
        NDMI?: number;
        NDRE?: number;
        CIre?: number;
        MNDWI?: number;
        VV_db?: number;
        VH_db?: number;
        [key: string]: number | undefined;
    }>;
    pixels_source: "db_lonlat" | null;
}

// ── Uploads ──────────────────────────────────────────────────────

const MINIO_URL = process.env.NEXT_PUBLIC_MINIO_URL || "http://localhost:9000/openfarm";

export const uploadsApi = {
    presign: (filename: string, contentType = "image/jpeg") =>
        apiFetch<PresignedUpload>("/uploads/presign", {
            method: "POST",
            body: JSON.stringify({ filename, content_type: contentType }),
        }),
    /** Upload file to MinIO via presigned URL, returns the object key. */
    async upload(file: File): Promise<string> {
        const ext = file.name.split(".").pop() || "jpg";
        const ct = file.type || "image/jpeg";
        const { upload_url, object_key } = await this.presign(file.name, ct);
        // PUT directly to MinIO (presigned URL)
        // Do NOT send Content-Type header - it's not part of the signed
        // headers so MinIO would reject the request with 403.
        const res = await fetch(upload_url, {
            method: "PUT",
            body: file,
        });
        if (!res.ok) throw new Error("Upload failed");
        return object_key;
    },
};

/** Construct a public URL for a photo stored in MinIO. */
export function getPhotoUrl(objectKey: string): string {
    return `${MINIO_URL}/${objectKey}`;
}

// ── Soil Types ───────────────────────────────────────────────────

export interface SoilLayer {
    depth_top_cm: number;
    depth_bottom_cm: number;
    sand_pct: number | null;
    silt_pct: number | null;
    clay_pct: number | null;
    ph: number | null;
    soc_g_kg: number | null;
    bd_kg_dm3: number | null;
    cec_cmol_kg: number | null;
    nitrogen_g_kg: number | null;
    cfvo_pct: number | null;
    fc_vol_pct: number | null;
    wp_vol_pct: number | null;
    awc_mm: number | null;
    ksat_cm_day: number | null;
    texture_class: string | null;
    sand_q05: number | null;
    sand_q95: number | null;
    clay_q05: number | null;
    clay_q95: number | null;
    ph_q05: number | null;
    ph_q95: number | null;
    soc_q05: number | null;
    soc_q95: number | null;
    ksat_q05: number | null;
    ksat_q95: number | null;
}

export interface SoilProfile {
    id: string;
    field_id: string;
    source: string;
    source_resolution_m: number | null;
    fetched_at: string;
    layers: SoilLayer[];
}

export interface SoilFieldSummary {
    id: string;
    field_id: string;
    dominant_texture: string | null;
    avg_ph: number | null;
    total_soc_stock_t_ha: number | null;
    rootzone_awc_mm: number | null;
    drainage_class: string | null;
    acidification_risk: number | null;
    compaction_risk: number | null;
    leaching_risk: number | null;
    rooting_constraint: number | null;
    waterlogging_risk: number | null;
    topsoil_soc_stock_t_ha: number | null;
    data_quality_score: number | null;
    computed_at: string;
}

export interface SoilRefreshResponse {
    field_id: string;
    job_id: string;
    status: string;
    message: string;
}

// Intelligence response types

export interface SamplingZoneFeature {
    type: "Feature";
    geometry: GeoJSON.Geometry;
    properties: Record<string, any>;
}

export interface SamplingZonesResponse {
    type: "FeatureCollection";
    features: SamplingZoneFeature[];
}

export interface CropSuitabilityItem {
    crop: string;
    name: string;
    score: number;
    rating: string;
    limiting_factors: string[];
}

export interface CropSuitabilityResponse {
    crops: CropSuitabilityItem[];
    field_crop_type: string | null;
    field_crop_suitability: CropSuitabilityItem | null;
    weather_available: boolean;
    message: string | null;
}

export interface NutrientContextResponse {
    zone_class: string;
    confidence: number;
    factors: string[];
    interpretation: string;
    disclaimer: string;
}

export interface CarbonEstimateResponse {
    current_soc_t_ha: number | null;
    topsoil_soc_t_ha: number | null;
    saturation_t_ha: number | null;
    saturation_pct: number | null;
    seq_potential_low_t_ha_yr: number | null;
    seq_potential_high_t_ha_yr: number | null;
    climate_zone: string | null;
    disclaimer: string;
}

export interface SoilWeatherStressResponse {
    status: string;
    severity: number;
    moisture_status: string;
    awc_rootzone_mm: number | null;
    water_balance_30d_mm: number | null;
    factors: string[];
}

export const soilApi = {
    get: (fieldId: string) =>
        apiFetch<SoilProfile>(`/fields/${fieldId}/soil`),
    getSummary: (fieldId: string) =>
        apiFetch<SoilFieldSummary>(`/fields/${fieldId}/soil/summary`),
    refresh: (fieldId: string) =>
        apiFetch<SoilRefreshResponse>(`/fields/${fieldId}/soil/refresh`, {
            method: "POST",
        }),
    getSamplingZones: (fieldId: string) =>
        apiFetch<SamplingZonesResponse>(`/fields/${fieldId}/soil/sampling-zones`),
    getCropSuitability: (fieldId: string) =>
        apiFetch<CropSuitabilityResponse>(`/fields/${fieldId}/soil/crop-suitability`),
    getNutrientContext: (fieldId: string) =>
        apiFetch<NutrientContextResponse>(`/fields/${fieldId}/soil/nutrient-context`),
    getCarbon: (fieldId: string) =>
        apiFetch<CarbonEstimateResponse>(`/fields/${fieldId}/soil/carbon`),
    getWeatherStress: (fieldId: string) =>
        apiFetch<SoilWeatherStressResponse>(`/fields/${fieldId}/soil/weather-stress`),
};

// ── Agri (地块 S1/S2 场景产品) ────────────────────────────────────

export type AgriSensor = "S1" | "S2";

export interface AgriSceneProduct {
    land_id: string;
    tile_id: string;
    date: string;
    sensor: AgriSensor;
    scene_id: string;
    land_name: string | null;
    cloud_cover: number | null;
    cloud_cover_over_30: boolean | null;
    parcel_cloud_cover_pct: number | null;
    /** scl | lonlat_clear when the parcel cloud is in-polygon; missing = legacy */
    parcel_cloud_source?: string | null;
    /** stac_direct (raw) or uncrtaints_decloud (additive) */
    source?: string | null;
    /** good | fair | bad; only good enters official drought metrics */
    decloud_quality?: string | null;
    decloud_score?: number | null;
    /** Quality-gate reason codes when the row is a decloud product */
    decloud_reasons?: string[] | null;
    /** Sentinel-1 relative orbit when known (parsed from scene_id if missing) */
    relative_orbit?: number | null;
    ndvi_avg: number | null;
    ndvi_min: number | null;
    ndvi_max: number | null;
    evi_avg: number | null;
    evi_min: number | null;
    evi_max: number | null;
    ndmi_avg: number | null;
    ndre_avg: number | null;
    mndwi_avg: number | null;
    cire_avg: number | null;
    vv_avg: number | null;
    vv_min: number | null;
    vv_max: number | null;
    vh_avg: number | null;
    vh_min: number | null;
    vh_max: number | null;
    /** Present only when include_pixels=1 — legacy grid fallback */
    pixel_data?: {
        grid: {
            epsg: number;
            width: number;
            height: number;
            origin_x: number;
            origin_y: number;
            resolution: number;
        };
        pixels: number[][];
    } | null;
    /** Preferred DB lonlat_v1 (or OSS) lon/lat pixels when include_pixels=1 */
    pixels_lonlat?: Array<{
        lon: number;
        lat: number;
        clear?: number;
        NDVI?: number;
        EVI?: number;
        NDMI?: number;
        NDRE?: number;
        CIre?: number;
        MNDWI?: number;
        VV_db?: number;
        VH_db?: number;
        [key: string]: number | undefined;
    }> | null;
    rgb_url?: string | null;
    large_rgb_url?: string | null;
    heatmap_url?: string | null;
    s2_heatmap_url?: string | null;
    pixels_source?: "db_lonlat" | "oss" | "db_grid" | null;
}

export interface AgriSensorSceneSummary {
    sensor: AgriSensor;
    count: number;
    date_min: string | null;
    date_max: string | null;
    latest_ndvi_avg: number | null;
    latest_evi_avg: number | null;
    latest_vv_avg: number | null;
    latest_vh_avg: number | null;
    latest_date: string | null;
}

export interface AgriLandScenesSummary {
    land_id: string;
    total: number;
    sensors: AgriSensorSceneSummary[];
}

/** Parse agri:<land_id> tag from field.tags */
export function parseAgriLandId(tags: string[] | null | undefined): string | null {
    if (!tags?.length) return null;
    for (const tag of tags) {
        if (typeof tag === "string" && tag.startsWith("agri:")) {
            const id = tag.slice(5).trim();
            if (id) return id;
        }
    }
    return null;
}

export const agriApi = {
    overviewStats: (opts: {
        level?: OverviewLevel;
        code?: string;
        name?: string;
        from?: string;
        to?: string;
        /** Crop key — phenology months for weak-growth only (does not filter parcels). */
        crop?: string;
    } = {}) => {
        const params = new URLSearchParams();
        if (opts.level) params.set("level", opts.level);
        if (opts.code) params.set("code", opts.code);
        if (opts.name) params.set("name", opts.name);
        if (opts.from) params.set("from", opts.from);
        if (opts.to) params.set("to", opts.to);
        if (opts.crop) params.set("crop", opts.crop);
        return apiFetch<OverviewStats>(`/agri/overview/stats?${params}`);
    },
    overviewWeakParcels: (opts: {
        level?: OverviewLevel;
        code?: string;
        name?: string;
        from?: string;
        to?: string;
        crop?: string;
        limit?: number;
        offset?: number;
    } = {}) => {
        const params = new URLSearchParams();
        if (opts.level) params.set("level", opts.level);
        if (opts.code) params.set("code", opts.code);
        if (opts.name) params.set("name", opts.name);
        if (opts.from) params.set("from", opts.from);
        if (opts.to) params.set("to", opts.to);
        if (opts.crop) params.set("crop", opts.crop);
        params.set("limit", String(opts.limit ?? 50));
        params.set("offset", String(opts.offset ?? 0));
        return apiFetch<OverviewWeakParcels>(`/agri/overview/weak-parcels?${params}`);
    },
    overviewRegions: (opts: {
        parentLevel?: OverviewLevel;
        parentCode?: string;
        parentName?: string;
    } = {}) => {
        const params = new URLSearchParams();
        if (opts.parentLevel) params.set("parent_level", opts.parentLevel);
        if (opts.parentCode) params.set("parent_code", opts.parentCode);
        if (opts.parentName) params.set("parent_name", opts.parentName);
        return apiFetch<OverviewRegions>(`/agri/overview/regions?${params}`);
    },
    overviewExportStatsCsv: async (opts: {
        level?: OverviewLevel;
        code?: string;
        name?: string;
        from?: string;
        to?: string;
        crop?: string;
    } = {}) => {
        const params = new URLSearchParams();
        if (opts.level) params.set("level", opts.level);
        if (opts.code) params.set("code", opts.code);
        if (opts.name) params.set("name", opts.name);
        if (opts.from) params.set("from", opts.from);
        if (opts.to) params.set("to", opts.to);
        if (opts.crop) params.set("crop", opts.crop);
        return apiDownload(`/agri/overview/export/stats.csv?${params}`, "overview-stats.csv");
    },
    overviewExportWeakParcelsCsv: async (opts: {
        level?: OverviewLevel;
        code?: string;
        name?: string;
        from?: string;
        to?: string;
        crop?: string;
        limit?: number;
    } = {}) => {
        const params = new URLSearchParams();
        if (opts.level) params.set("level", opts.level);
        if (opts.code) params.set("code", opts.code);
        if (opts.name) params.set("name", opts.name);
        if (opts.from) params.set("from", opts.from);
        if (opts.to) params.set("to", opts.to);
        if (opts.crop) params.set("crop", opts.crop);
        params.set("limit", String(opts.limit ?? 5000));
        return apiDownload(`/agri/overview/export/weak-parcels.csv?${params}`, "overview-weak-parcels.csv");
    },
    scenes: (
        landId: string,
        opts: {
            sensor?: AgriSensor;
            from?: string;
            to?: string;
            limit?: number;
            offset?: number;
            /** If 1, prefer DB lonlat_v1 pixels (pixels_lonlat); grid pixel_data is fallback. */
            includePixels?: 0 | 1;
        } = {},
    ) => {
        const params = new URLSearchParams();
        if (opts.sensor) params.set("sensor", opts.sensor);
        if (opts.from) params.set("from", opts.from);
        if (opts.to) params.set("to", opts.to);
        if (opts.includePixels != null) params.set("include_pixels", String(opts.includePixels));
        params.set("limit", String(opts.limit ?? 200));
        params.set("offset", String(opts.offset ?? 0));
        return apiFetch<Paginated<AgriSceneProduct>>(
            `/agri/lands/${encodeURIComponent(landId)}/scenes?${params}`,
        );
    },
    scenesSummary: (landId: string) =>
        apiFetch<AgriLandScenesSummary>(
            `/agri/lands/${encodeURIComponent(landId)}/scenes/summary`,
        ),
};


// ── China overview (全国态势) ──────────────────────────────────────

export type OverviewLevel = "country" | "province" | "city" | "county";

export interface OverviewRegionPathNode {
    level: OverviewLevel;
    code: string | null;
    name: string;
}

export interface OverviewChild {
    level: OverviewLevel;
    code: string | null;
    name: string;
    parcel_count: number;
    drought_severe: number;
    /** severe + moderate + mild */
    drought_alert: number;
    /** open water: flood_severe + flood_moderate */
    flood: number;
    /** confirmed flood only (watch / former mild is not included) */
    flood_alert?: number;
    weak_growth: number;
    area_mu: number;
}

export interface OverviewStats {
    region: {
        level: OverviewLevel;
        code: string | null;
        name: string;
        path: OverviewRegionPathNode[];
        adcode?: string | null;
    };
    filters: {
        from: string;
        to: string;
        /** Crop key used for phenology months; null when default Jun–Sep. */
        crop: string | null;
        cloud_max_pct: number;
        phenology_months: number[];
        weak_ndvi_lt: number;
        drought_source?: "pixels" | "scene_avg" | "cache";
        cache_hit?: boolean;
        pixels_parcels?: number;
        pixels_classified?: number;
    };
    totals: { parcel_count: number; area_mu: number };
    drought: {
        severe: number;
        moderate: number;
        mild: number;
        normal: number;
        unknown: number;
        area_mu: Record<string, number>;
    };
    flood: {
        flood_severe?: number;
        flood_moderate?: number;
        flood_mild?: number;
        /** open water = severe+moderate (compat) */
        flood: number;
        /** alias of flood_mild */
        wet: number;
        dry: number;
        unknown: number;
        area_mu: Record<string, number>;
    };
    weak_growth: { parcel_count: number; area_mu: number };
    children: OverviewChild[];
}

export interface OverviewRegions {
    parent_level: OverviewLevel | null;
    parent_code: string | null;
    parent_name: string | null;
    children: {
        level: OverviewLevel;
        code: string | null;
        name: string;
        parcel_count: number;
        area_mu: number;
    }[];
}

export interface OverviewWeakParcel {
    land_id: string;
    land_name: string | null;
    province_name: string | null;
    city_name: string | null;
    county_name: string | null;
    land_area_mu: number;
    ndvi_avg: number;
    scene_date: string | null;
    cloud_pct: number | null;
}

export interface OverviewWeakParcels {
    total: number;
    items: OverviewWeakParcel[];
}

// ── Share Links ──────────────────────────────────────────────────

export const shareApi = {
    list: (fieldId: string) =>
        apiFetch<ShareLink[]>(`/fields/${fieldId}/share`),
    create: (fieldId: string, expiresInDays: number | null) =>
        apiFetch<ShareLink>(`/fields/${fieldId}/share`, {
            method: "POST",
            body: JSON.stringify({ expires_in_days: expiresInDays }),
        }),
    revoke: (fieldId: string, token: string) =>
        apiFetch(`/fields/${fieldId}/share/${token}`, { method: "DELETE" }),
    /** Public endpoint - no auth required. Uses plain fetch. */
    async getReport(token: string): Promise<ShareReport> {
        const res = await fetch(`${getApiBase()}/share/${token}`);
        if (res.status === 410) throw new Error("expired");
        if (res.status === 404) throw new Error("not_found");
        if (!res.ok) throw new Error(`Report fetch failed: ${res.status}`);
        return res.json();
    },
    /** Public agri lonlat pixels for share map 色斑 (gated by share token). */
    async getAgriPixels(
        token: string,
        opts: { indexType?: string; sceneDate?: string } = {},
    ): Promise<ShareAgriPixels> {
        const params = new URLSearchParams();
        if (opts.indexType) params.set("index_type", opts.indexType);
        if (opts.sceneDate) params.set("scene_date", opts.sceneDate);
        const qs = params.toString();
        const res = await fetch(
            `${getApiBase()}/share/${token}/agri-pixels${qs ? `?${qs}` : ""}`,
        );
        if (res.status === 410) throw new Error("expired");
        if (res.status === 404) throw new Error("not_found");
        if (!res.ok) throw new Error(`Agri pixels fetch failed: ${res.status}`);
        return res.json();
    },
};

export default apiFetch;

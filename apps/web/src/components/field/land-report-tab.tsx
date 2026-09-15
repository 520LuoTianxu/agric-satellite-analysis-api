"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
    assessmentApi,
    fieldsApi,
    jobsApi,
    seasonGrowthApi,
    soilApi,
    ApiError,
    type AssessmentDimensionKey,
    type AssessmentScorecard,
    type NdviJob,
} from "@/lib/api";
import CropSelect from "@/components/field/crop-select";
import LandScorecardRadar from "@/components/charts/land-scorecard-radar";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { DatePicker } from "@/components/ui/date-picker";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
    CheckCircle2,
    Download,
    FileText,
    Hexagon,
    Loader2,
    Paperclip,
    RefreshCw,
    AlertTriangle,
    Sprout,
    X,
} from "lucide-react";
import { toast } from "sonner";
import { useTranslations } from "next-intl";

interface LandReportTabProps {
    fieldId: string;
    cropType?: string | null;
    onCropBound?: (cropKey: string) => void;
}

type ScorecardState = "loading" | "empty" | "legacy" | "ready";

const AXIS_I18N: Record<AssessmentDimensionKey, string> = {
    crop: "axisCrop",
    soil: "axisSoil",
    vigor: "axisVigor",
    weather: "axisWeather",
    wet_safety: "axisWetSafety",
    drought_safety: "axisDroughtSafety",
};

function trafficKind(light?: string | null): "green" | "yellow" | "red" | null {
    if (light === "绿" || light === "green") return "green";
    if (light === "黄" || light === "yellow") return "yellow";
    if (light === "红" || light === "red") return "red";
    return null;
}

function lightBadgeClass(light?: string | null) {
    const kind = trafficKind(light);
    if (kind === "green") return "bg-success-subtle text-success";
    if (kind === "yellow") return "bg-warning-subtle text-warning";
    if (kind === "red") return "bg-danger-subtle text-danger";
    return "bg-surface-2 text-muted-foreground";
}

function errorCode(err: unknown): string | undefined {
    if (!(err instanceof ApiError)) return undefined;
    const d = err.detail as unknown;
    if (d && typeof d === "object" && "code" in d) {
        return String((d as { code: unknown }).code);
    }
    return undefined;
}

function lightLabel(
    t: (key: "lightGreen" | "lightYellow" | "lightRed") => string,
    light?: string | null,
    fallback?: string | null,
) {
    const kind = trafficKind(light);
    if (kind === "green") return t("lightGreen");
    if (kind === "yellow") return t("lightYellow");
    if (kind === "red") return t("lightRed");
    return fallback || "";
}


function isUsableCrop(raw: string | null | undefined): boolean {
    if (raw == null) return false;
    const s = String(raw).trim();
    if (!s) return false;
    const lower = s.toLowerCase();
    if (lower === "unknown" || lower === "-" || lower === "null" || lower === "undefined") {
        return false;
    }
    return true;
}

function isCropRequiredError(err: any): boolean {
    const d = err?.detail ?? err?.message;
    if (d && typeof d === "object" && d.code === "crop_required") return true;
    if (typeof d === "string" && d.includes("crop_required")) return true;
    return false;
}

export default function LandReportTab({ fieldId, cropType, onCropBound }: LandReportTabProps) {
    const t = useTranslations("landReportTab");
    const [latest, setLatest] = useState<NdviJob | null>(null);
    const [loading, setLoading] = useState(true);
    const [generating, setGenerating] = useState(false);
    const [downloading, setDownloading] = useState(false);
    const [boundCrop, setBoundCrop] = useState(() => (isUsableCrop(cropType) ? String(cropType).trim() : ""));
    const [pickCrop, setPickCrop] = useState(() => (isUsableCrop(cropType) ? String(cropType).trim() : ""));
    const [years, setYears] = useState<number>(3);
    const [dateFrom, setDateFrom] = useState<string>("");
    const [scorecard, setScorecard] = useState<AssessmentScorecard | null>(null);
    const [scorecardState, setScorecardState] = useState<ScorecardState>("loading");
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    // ── Season growth report ──────────────────────────────────────────
    const ts = useTranslations("seasonGrowthReport");
    const [sgLatest, setSgLatest] = useState<NdviJob | null>(null);
    const [sgLoading, setSgLoading] = useState(true);
    const [sgGenerating, setSgGenerating] = useState(false);
    const [sgDownloading, setSgDownloading] = useState(false);
    const [sgStart, setSgStart] = useState(() => {
        const y = new Date().getFullYear();
        return `${y}-06-01`;
    });
    const [sgEnd, setSgEnd] = useState(() => {
        const y = new Date().getFullYear();
        return `${y}-09-30`;
    });
    const [sgCrop, setSgCrop] = useState("");
    const [sgLabel, setSgLabel] = useState("");
    const [sgFiles, setSgFiles] = useState<File[]>([]);
    const [sgUploading, setSgUploading] = useState(false);
    const sgPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const sgFileRef = useRef<HTMLInputElement>(null);

    // Shared cdfinance credentials for assessment + season-growth generate
    const [cdfinanceToken, setCdfinanceToken] = useState("");
    const [cdfinanceGroupId, setCdfinanceGroupId] = useState("");
    const [cdfinanceHrBaseId, setCdfinanceHrBaseId] = useState("");

    useEffect(() => {
        const next = isUsableCrop(cropType) ? String(cropType).trim() : "";
        setBoundCrop(next);
        if (next) setPickCrop(next);
    }, [cropType]);

    const refreshMeta = useCallback(async () => {
        try {
            const job = await assessmentApi.latestMeta(fieldId);
            setLatest(job);
            return job;
        } catch {
            setLatest(null);
            return null;
        } finally {
            setLoading(false);
        }
    }, [fieldId]);

    const refreshScorecard = useCallback(async () => {
        try {
            const next = await assessmentApi.latestScorecard(fieldId);
            setScorecard(next);
            setScorecardState("ready");
        } catch (err) {
            setScorecard(null);
            setScorecardState(errorCode(err) === "scorecard_unavailable" ? "legacy" : "empty");
        }
    }, [fieldId]);

    const refreshSgMeta = useCallback(async () => {
        try {
            const job = await seasonGrowthApi.latestMeta(fieldId);
            setSgLatest(job);
            return job;
        } catch {
            setSgLatest(null);
            return null;
        } finally {
            setSgLoading(false);
        }
    }, [fieldId]);

    useEffect(() => {
        refreshMeta();
        refreshScorecard();
        refreshSgMeta();
        return () => {
            if (pollRef.current) clearInterval(pollRef.current);
            if (sgPollRef.current) clearInterval(sgPollRef.current);
        };
    }, [refreshMeta, refreshScorecard, refreshSgMeta]);

    const stopSgPoll = () => {
        if (sgPollRef.current) {
            clearInterval(sgPollRef.current);
            sgPollRef.current = null;
        }
    };

    const startSgPoll = (jobId: string) => {
        stopSgPoll();
        sgPollRef.current = setInterval(async () => {
            try {
                const job = await jobsApi.get(jobId);
                setSgLatest(job);
                if (job.status === "succeeded") {
                    stopSgPoll();
                    setSgGenerating(false);
                    toast.success(ts("generateDone"));
                } else if (job.status === "failed") {
                    stopSgPoll();
                    setSgGenerating(false);
                    toast.error(job.error || ts("generateFailed"));
                }
            } catch {
                /* keep polling */
            }
        }, 2000);
    };

    const handleSgGenerate = async () => {
        if (!sgStart || !sgEnd) {
            toast.error(ts("datesRequired"));
            return;
        }
        setSgGenerating(true);
        try {
            let material_keys: string[] = [];
            if (sgFiles.length) {
                setSgUploading(true);
                for (const f of sgFiles) {
                    const up = await seasonGrowthApi.uploadMaterial(fieldId, f);
                    if (up.key) material_keys.push(up.key);
                }
                setSgUploading(false);
            }
            const crops = sgCrop.trim() ? [sgCrop.trim()] : [];
            const cdfinanceBody: {
                cdfinance_token?: string;
                group_id?: string;
                hr_base_id?: string;
            } = {};
            if (cdfinanceToken.trim()) {
                cdfinanceBody.cdfinance_token = cdfinanceToken.trim();
            }
            if (cdfinanceGroupId.trim()) {
                cdfinanceBody.group_id = cdfinanceGroupId.trim();
            }
            if (cdfinanceHrBaseId.trim()) {
                cdfinanceBody.hr_base_id = cdfinanceHrBaseId.trim();
            }
            const job = await seasonGrowthApi.generate(fieldId, {
                start_date: sgStart,
                end_date: sgEnd,
                crops,
                label: sgLabel.trim() || undefined,
                material_keys,
                pull_data: true,
                ...cdfinanceBody,
            });
            setSgLatest(job);
            if (job.status === "succeeded") {
                setSgGenerating(false);
                toast.success(ts("generateDone"));
                return;
            }
            if (job.status === "failed") {
                setSgGenerating(false);
                toast.error(job.error || ts("generateFailed"));
                return;
            }
            toast.message(ts("generateStarted"));
            startSgPoll(job.id);
        } catch (e: any) {
            setSgGenerating(false);
            setSgUploading(false);
            toast.error(e?.detail?.message || e?.message || ts("generateFailed"));
        }
    };

    const handleSgDownload = async () => {
        setSgDownloading(true);
        try {
            await seasonGrowthApi.downloadLatest(fieldId);
        } catch (e: any) {
            toast.error(e?.message || ts("downloadFailed"));
        } finally {
            setSgDownloading(false);
        }
    };

    const sgProgress = sgLatest?.progress_json || {};
    const sgOneLiner = sgProgress.one_liner as string | undefined;
    const sgHasPdf = sgLatest?.status === "succeeded" && Boolean(sgProgress.object_key);
    const sgInFlight =
        sgGenerating || sgLatest?.status === "pending" || sgLatest?.status === "running";

    const stopPoll = () => {
        if (pollRef.current) {
            clearInterval(pollRef.current);
            pollRef.current = null;
        }
    };

    const startPoll = (jobId: string) => {
        stopPoll();
        pollRef.current = setInterval(async () => {
            try {
                const job = await jobsApi.get(jobId);
                setLatest(job);
                if (job.status === "succeeded") {
                    stopPoll();
                    setGenerating(false);
                    toast.success(t("generateDone"));
                    void refreshScorecard();
                } else if (job.status === "failed") {
                    stopPoll();
                    setGenerating(false);
                    toast.error(job.error || t("generateFailed"));
                }
            } catch {
                /* keep polling briefly */
            }
        }, 2000);
    };

    const runGenerate = async (cropKey?: string) => {
        setGenerating(true);
        try {
            const body: {
                crop_type?: string;
                date_from?: string;
                years?: number;
                pull_data: boolean;
                cdfinance_token?: string;
                group_id?: string;
                hr_base_id?: string;
            } = { pull_data: true };
            if (cropKey) body.crop_type = cropKey;
            if (dateFrom.trim()) {
                body.date_from = dateFrom.trim();
            } else {
                body.years = years;
            }
            if (cdfinanceToken.trim()) {
                body.cdfinance_token = cdfinanceToken.trim();
            }
            if (cdfinanceGroupId.trim()) {
                body.group_id = cdfinanceGroupId.trim();
            }
            if (cdfinanceHrBaseId.trim()) {
                body.hr_base_id = cdfinanceHrBaseId.trim();
            }
            // Soft prompt when no token and no cached site admission
            if (!body.cdfinance_token) {
                try {
                    await soilApi.getSiteAdmission(fieldId);
                } catch {
                    toast.message(t("siteAdmissionMissingHint"));
                }
            }
            const job = await assessmentApi.generate(fieldId, body);
            if (cropKey) {
                setBoundCrop(cropKey);
                onCropBound?.(cropKey);
            }
            setLatest(job);
            if (job.status === "succeeded") {
                setGenerating(false);
                toast.success(t("generateDone"));
                void refreshScorecard();
                return;
            }
            if (job.status === "failed") {
                setGenerating(false);
                toast.error(job.error || t("generateFailed"));
                return;
            }
            toast.message(t("pullAndGenerateStarted"));
            startPoll(job.id);
        } catch (e: any) {
            setGenerating(false);
            if (isCropRequiredError(e)) {
                toast.error(t("cropRequired"));
                return;
            }
            toast.error(e?.detail?.message || e?.message || t("generateFailed"));
        }
    };

    const handleGenerate = async () => {
        const cropKey = isUsableCrop(pickCrop)
            ? pickCrop.trim()
            : isUsableCrop(boundCrop)
              ? boundCrop.trim()
              : "";
        if (!cropKey) {
            toast.error(t("cropRequired"));
            return;
        }
        await runGenerate(cropKey);
    };

    const handleBindOnly = async () => {
        if (!isUsableCrop(pickCrop)) {
            toast.error(t("cropRequired"));
            return;
        }
        try {
            const key = pickCrop.trim();
            await fieldsApi.update(fieldId, { crop_type: key });
            setBoundCrop(key);
            onCropBound?.(key);
            toast.success(t("cropBound"));
        } catch (e: any) {
            toast.error(e?.message || t("cropBindFailed"));
        }
    };

    const handleDownload = async () => {
        setDownloading(true);
        try {
            await assessmentApi.downloadLatest(fieldId);
        } catch (e: any) {
            toast.error(e?.message || t("downloadFailed"));
        } finally {
            setDownloading(false);
        }
    };

    const progress = latest?.progress_json || {};
    const score =
        scorecard?.overall.score ??
        (typeof progress.score === "number" ? (progress.score as number) : undefined);
    const grade = scorecard?.overall.grade ?? (progress.grade as string | undefined);
    const light = scorecard?.overall.light ?? (progress.light as string | undefined);
    const oneLiner =
        scorecard?.overall.one_liner ?? (progress.one_liner as string | undefined);
    const hasPdf = latest?.status === "succeeded" && Boolean(progress.object_key);
    const inFlight =
        generating || latest?.status === "pending" || latest?.status === "running";
    const needsCrop = !isUsableCrop(boundCrop);

    return (
        <div className="p-4 space-y-4">
            <div className="space-y-1">
                <h3 className="text-sm font-semibold flex items-center gap-2">
                    <FileText className="h-4 w-4" />
                    {t("title")}
                </h3>
                <p className="text-xs text-muted-foreground leading-relaxed">
                    {t("description")}
                </p>
            </div>

            {loading ? (
                <div className="space-y-2">
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-16 w-full" />
                    <Skeleton className="h-64 w-full" />
                </div>
            ) : (
                <>
                    <div
                        className={`rounded-lg border p-3 space-y-3 ${
                            needsCrop
                                ? "border-warning/40 bg-warning-subtle"
                                : "border-border bg-card"
                        }`}
                    >
                        {needsCrop ? (
                            <p className="text-xs text-warning leading-relaxed">
                                {t("cropGateHint")}
                            </p>
                        ) : (
                            <p className="text-[11px] text-muted-foreground">
                                {t("boundCrop")}:{" "}
                                <span className="font-medium text-foreground">{boundCrop}</span>
                                {" · "}
                                {t("cropReselectHint")}
                            </p>
                        )}
                        <div className="space-y-1">
                            <span className="text-xs text-muted-foreground">{t("selectCrop")}</span>
                            <CropSelect
                                value={pickCrop}
                                onChange={setPickCrop}
                                placeholder={t("selectCrop")}
                                required
                            />
                        </div>
                        <div className="space-y-1.5">
                            <span className="text-xs text-muted-foreground">{t("historyWindow")}</span>
                            <div className="flex flex-wrap gap-1.5">
                                {[1, 2, 3, 5].map((y) => (
                                    <Button
                                        key={y}
                                        type="button"
                                        size="sm"
                                        variant={
                                            !dateFrom && years === y ? "default" : "outline"
                                        }
                                        className="h-7 px-2.5 text-xs"
                                        onClick={() => {
                                            setYears(y);
                                            setDateFrom("");
                                        }}
                                    >
                                        {t("yearsPreset", { years: y })}
                                    </Button>
                                ))}
                            </div>
                            <label className="text-xs space-y-1 block">
                                <span className="text-muted-foreground">{t("dateFromOptional")}</span>
                                <DatePicker
                                    value={dateFrom}
                                    max={new Date().toISOString().slice(0, 10)}
                                    onChange={setDateFrom}
                                />
                            </label>
                            <p className="text-[11px] text-muted-foreground leading-relaxed">
                                {dateFrom
                                    ? t("windowHintDateFrom", { dateFrom })
                                    : t("windowHintYears", { years })}
                            </p>
                        </div>
                        {needsCrop && (
                            <div className="flex flex-wrap gap-2">
                                <Button size="sm" variant="outline" onClick={handleBindOnly}>
                                    {t("bindCrop")}
                                </Button>
                            </div>
                        )}
                    </div>

                    <div className="rounded-lg border border-border bg-card p-3 space-y-2">
                        <p className="text-xs font-medium">{t("cdfinancePanelTitle")}</p>
                        <p className="text-[11px] text-muted-foreground leading-relaxed">
                            {t("cdfinancePanelHint")}
                        </p>
                        <label className="text-xs space-y-1 block">
                            <span className="text-muted-foreground">{t("cdfinanceGroupId")}</span>
                            <Input
                                className="h-9"
                                value={cdfinanceGroupId}
                                placeholder={t("cdfinanceGroupPlaceholder")}
                                onChange={(e) => setCdfinanceGroupId(e.target.value)}
                                autoComplete="off"
                            />
                        </label>
                        <label className="text-xs space-y-1 block">
                            <span className="text-muted-foreground">{t("cdfinanceToken")}</span>
                            <Input
                                className="h-9 font-mono text-[11px]"
                                type="password"
                                value={cdfinanceToken}
                                placeholder={t("cdfinanceTokenPlaceholder")}
                                onChange={(e) => setCdfinanceToken(e.target.value)}
                                autoComplete="off"
                            />
                        </label>
                        <label className="text-xs space-y-1 block">
                            <span className="text-muted-foreground">{t("cdfinanceHrBaseId")}</span>
                            <Input
                                className="h-9"
                                value={cdfinanceHrBaseId}
                                placeholder={t("cdfinanceHrBaseIdPlaceholder")}
                                onChange={(e) => setCdfinanceHrBaseId(e.target.value)}
                                autoComplete="off"
                            />
                        </label>
                    </div>

                    <div className="flex flex-wrap gap-2">
                        <Button
                            size="sm"
                            onClick={handleGenerate}
                            disabled={inFlight || (!isUsableCrop(pickCrop) && !isUsableCrop(boundCrop))}
                        >
                            {inFlight ? (
                                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                            ) : (
                                <RefreshCw className="h-4 w-4 mr-1.5" />
                            )}
                            {inFlight
                                ? t("generating")
                                : needsCrop
                                  ? t("bindAndGenerate")
                                  : t("pullAndGenerate")}
                        </Button>
                        <Button
                            size="sm"
                            variant="outline"
                            onClick={handleDownload}
                            disabled={!hasPdf || downloading}
                        >
                            {downloading ? (
                                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                            ) : (
                                <Download className="h-4 w-4 mr-1.5" />
                            )}
                            {t("download")}
                        </Button>
                    </div>

                    {scorecardState === "loading" ? (
                        <Skeleton className="h-64 w-full rounded-lg" />
                    ) : scorecard && scorecardState === "ready" ? (
                        <div className="rounded-lg border bg-card shadow-sm p-4 space-y-3">
                            <div className="space-y-1">
                                <h4 className="text-[15px] font-semibold flex items-center gap-2">
                                    <Hexagon className="h-4 w-4" />
                                    {t("chartTitle")}
                                </h4>
                                <p className="text-[11px] text-muted-foreground leading-relaxed">
                                    {t("chartCaption")}
                                </p>
                            </div>

                            <div className="flex items-end gap-3">
                                <div
                                    className={`rounded-md px-3 py-2 text-center min-w-[4.5rem] ${lightBadgeClass(light)}`}
                                >
                                    <div className="text-2xl font-bold tabular-nums leading-none font-mono">
                                        {score}
                                    </div>
                                    <div className="text-[10px] font-semibold uppercase tracking-wider mt-1">
                                        {lightLabel(t, light, grade)}
                                    </div>
                                </div>
                                <div className="flex-1 space-y-1">
                                    <p className="text-xs font-medium">{t("overallScore")}</p>
                                    {oneLiner && (
                                        <p className="text-xs text-muted-foreground leading-relaxed">
                                            {oneLiner}
                                        </p>
                                    )}
                                </div>
                            </div>

                            <LandScorecardRadar scorecard={scorecard} />

                            <ul className="grid grid-cols-1 gap-2">
                                {scorecard.dimensions.map((d) => (
                                    <li
                                        key={d.key}
                                        className="flex items-center justify-between gap-2 rounded-lg border p-3"
                                    >
                                        <span className="text-xs">
                                            {t(AXIS_I18N[d.key])}
                                            {d.weight ? (
                                                <span className="text-muted-foreground"> · {d.weight}</span>
                                            ) : null}
                                        </span>
                                        <span className="font-mono text-sm font-medium tabular-nums">
                                            {t("scoreOutOf", { score: d.score })}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                            <p className="text-[11px] text-muted-foreground leading-relaxed">
                                {t("chartWeights")}
                            </p>
                        </div>
                    ) : (
                        <div className="rounded-lg border-2 border-dashed p-12 text-center space-y-2">
                            <Hexagon className="h-12 w-12 mx-auto text-muted-foreground" />
                            <p className="text-sm font-medium">
                                {scorecardState === "legacy" ? t("legacyNoScorecard") : t("noScorecard")}
                            </p>
                            <p className="text-xs text-muted-foreground leading-relaxed">
                                {scorecardState === "legacy"
                                    ? t("legacyNoScorecardHint")
                                    : t("noScorecardHint")}
                            </p>
                        </div>
                    )}

                    {latest && (
                        <div className="rounded-lg border bg-card p-3 space-y-2">
                            <div className="flex items-center justify-between gap-2">
                                <span className="text-xs text-muted-foreground">
                                    {t("status")}
                                </span>
                                <Badge variant="secondary" className="text-xs">
                                    {latest.status === "succeeded" && (
                                        <CheckCircle2 className="h-3 w-3 mr-1 text-success" />
                                    )}
                                    {latest.status === "failed" && (
                                        <AlertTriangle className="h-3 w-3 mr-1 text-destructive" />
                                    )}
                                    {(latest.status === "pending" ||
                                        latest.status === "running") && (
                                        <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                                    )}
                                    {t(`status_${latest.status}` as any, {
                                        default: latest.status,
                                    })}
                                </Badge>
                            </div>

                            {typeof score === "number" && scorecardState !== "ready" && (
                                <div className="flex items-end gap-3 pt-1">
                                    <div
                                        className={`rounded-md px-3 py-2 text-center min-w-[4.5rem] ${lightBadgeClass(light)}`}
                                    >
                                        <div className="text-2xl font-bold tabular-nums leading-none font-mono">
                                            {score}
                                        </div>
                                        <div className="text-[10px] font-semibold uppercase tracking-wider mt-1">
                                            {grade || light || ""}
                                        </div>
                                    </div>
                                    {oneLiner && (
                                        <p className="text-xs text-muted-foreground leading-relaxed flex-1">
                                            {oneLiner}
                                        </p>
                                    )}
                                </div>
                            )}

                            {latest.status === "failed" && latest.error && (
                                <p className="text-xs text-destructive">{latest.error}</p>
                            )}

                            {latest.finished_at && latest.status === "succeeded" && (
                                <p className="text-[11px] text-muted-foreground">
                                    {t("generatedAt", {
                                        time: new Date(latest.finished_at).toLocaleString(),
                                    })}
                                </p>
                            )}
                        </div>
                    )}

                    {!latest && (
                        <p className="text-xs text-muted-foreground">{t("empty")}</p>
                    )}

                    <div className="rounded-md bg-surface-2 p-3 text-[11px] text-muted-foreground space-y-1 leading-relaxed">
                        <p>{t("noteVigor")}</p>
                        <p>{t("noteFlood")}</p>
                        <p>{t("noteArea")}</p>
                    </div>
                </>
            )}

            {/* ── 生育期长势报告 ─────────────────────────────────────── */}
            <div className="border-t pt-4 space-y-3">
                <div className="space-y-1">
                    <h3 className="text-sm font-semibold flex items-center gap-2">
                        <Sprout className="h-4 w-4" />
                        {ts("title")}
                    </h3>
                    <p className="text-xs text-muted-foreground leading-relaxed">
                        {ts("description")}
                    </p>
                </div>

                {sgLoading ? (
                    <Skeleton className="h-24 w-full" />
                ) : (
                    <>
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                            <label className="text-xs space-y-1">
                                <span className="text-muted-foreground">{ts("startDate")}</span>
                                <DatePicker value={sgStart} onChange={setSgStart} />
                            </label>
                            <label className="text-xs space-y-1">
                                <span className="text-muted-foreground">{ts("endDate")}</span>
                                <DatePicker value={sgEnd} onChange={setSgEnd} />
                            </label>
                        </div>
                        <div className="space-y-1">
                            <span className="text-xs text-muted-foreground">{ts("cropOptional")}</span>
                            <CropSelect
                                value={sgCrop}
                                onChange={setSgCrop}
                                placeholder={ts("selectCrop")}
                            />
                        </div>
                        <label className="text-xs space-y-1 block">
                            <span className="text-muted-foreground">{ts("labelOptional")}</span>
                            <Input
                                className="h-9"
                                value={sgLabel}
                                placeholder={ts("labelPlaceholder")}
                                onChange={(e) => setSgLabel(e.target.value)}
                            />
                        </label>
                        <div className="text-xs space-y-1.5">
                            <span className="text-muted-foreground">{ts("materials")}</span>
                            <input
                                ref={sgFileRef}
                                type="file"
                                multiple
                                className="hidden"
                                onChange={(e) =>
                                    setSgFiles(Array.from(e.target.files || []))
                                }
                            />
                            <div className="flex flex-wrap items-center gap-2">
                                <Button
                                    type="button"
                                    size="sm"
                                    variant="outline"
                                    className="h-9"
                                    onClick={() => sgFileRef.current?.click()}
                                >
                                    <Paperclip className="mr-1.5 h-4 w-4" />
                                    {ts("chooseFiles")}
                                </Button>
                                {sgFiles.length > 0 && (
                                    <Button
                                        type="button"
                                        size="sm"
                                        variant="ghost"
                                        className="h-9 px-2 text-muted-foreground"
                                        onClick={() => {
                                            setSgFiles([]);
                                            if (sgFileRef.current) sgFileRef.current.value = "";
                                        }}
                                    >
                                        <X className="mr-1 h-3.5 w-3.5" />
                                        {ts("clearFiles")}
                                    </Button>
                                )}
                            </div>
                            {sgFiles.length > 0 && (
                                <ul className="space-y-0.5 text-[11px] text-muted-foreground">
                                    <li>{ts("filesSelected", { count: sgFiles.length })}</li>
                                    {sgFiles.slice(0, 4).map((f) => (
                                        <li key={f.name} className="truncate">
                                            {f.name}
                                        </li>
                                    ))}
                                    {sgFiles.length > 4 && (
                                        <li>…</li>
                                    )}
                                </ul>
                            )}
                        </div>

                        <p className="text-[11px] text-muted-foreground leading-relaxed">
                            {ts("cdfinanceReuseHint")}
                        </p>

                        <div className="flex flex-wrap gap-2">
                            <Button
                                size="sm"
                                onClick={handleSgGenerate}
                                disabled={sgInFlight || sgUploading}
                            >
                                {sgInFlight || sgUploading ? (
                                    <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                                ) : (
                                    <RefreshCw className="h-4 w-4 mr-1.5" />
                                )}
                                {sgUploading
                                    ? ts("uploading")
                                    : sgInFlight
                                      ? ts("generating")
                                      : ts("generate")}
                            </Button>
                            <Button
                                size="sm"
                                variant="outline"
                                onClick={handleSgDownload}
                                disabled={!sgHasPdf || sgDownloading}
                            >
                                {sgDownloading ? (
                                    <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                                ) : (
                                    <Download className="h-4 w-4 mr-1.5" />
                                )}
                                {ts("download")}
                            </Button>
                        </div>

                        {sgLatest && (
                            <div className="rounded-lg border bg-card p-3 space-y-2">
                                <div className="flex items-center justify-between gap-2">
                                    <span className="text-xs text-muted-foreground">
                                        {ts("status")}
                                    </span>
                                    <Badge variant="secondary" className="text-xs">
                                        {sgLatest.status === "succeeded" && (
                                            <CheckCircle2 className="h-3 w-3 mr-1 text-success" />
                                        )}
                                        {sgLatest.status === "failed" && (
                                            <AlertTriangle className="h-3 w-3 mr-1 text-destructive" />
                                        )}
                                        {(sgLatest.status === "pending" ||
                                            sgLatest.status === "running") && (
                                            <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                                        )}
                                        {ts(`status_${sgLatest.status}` as any, {
                                            default: sgLatest.status,
                                        })}
                                    </Badge>
                                </div>
                                {sgOneLiner && (
                                    <p className="text-xs text-muted-foreground leading-relaxed">
                                        {sgOneLiner}
                                    </p>
                                )}
                                {sgLatest.status === "failed" && sgLatest.error && (
                                    <p className="text-xs text-destructive">{sgLatest.error}</p>
                                )}
                                {sgLatest.finished_at && sgLatest.status === "succeeded" && (
                                    <p className="text-[11px] text-muted-foreground">
                                        {ts("generatedAt", {
                                            time: new Date(sgLatest.finished_at).toLocaleString(),
                                        })}
                                    </p>
                                )}
                            </div>
                        )}

                        {!sgLatest && (
                            <p className="text-xs text-muted-foreground">{ts("empty")}</p>
                        )}

                        <div className="rounded-md bg-surface-2 p-3 text-[11px] text-muted-foreground leading-relaxed">
                            <p>{ts("noteFacts")}</p>
                            <p>{ts("noteLlm")}</p>
                        </div>
                    </>
                )}
            </div>

        </div>
    );
}

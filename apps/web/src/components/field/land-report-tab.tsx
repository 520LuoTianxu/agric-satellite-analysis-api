"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
    assessmentApi,
    fieldsApi,
    jobsApi,
    ApiError,
    type AssessmentDimensionKey,
    type AssessmentScorecard,
    type NdviJob,
} from "@/lib/api";
import CropSelect from "@/components/field/crop-select";
import LandScorecardRadar from "@/components/charts/land-scorecard-radar";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
    CheckCircle2,
    Download,
    FileText,
    Hexagon,
    Loader2,
    RefreshCw,
    AlertTriangle,
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
    const [pickCrop, setPickCrop] = useState("");
    const [scorecard, setScorecard] = useState<AssessmentScorecard | null>(null);
    const [scorecardState, setScorecardState] = useState<ScorecardState>("loading");
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    useEffect(() => {
        setBoundCrop(isUsableCrop(cropType) ? String(cropType).trim() : "");
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

    useEffect(() => {
        refreshMeta();
        refreshScorecard();
        return () => {
            if (pollRef.current) clearInterval(pollRef.current);
        };
    }, [refreshMeta, refreshScorecard]);

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
            const job = await assessmentApi.generate(
                fieldId,
                cropKey ? { crop_type: cropKey } : undefined,
            );
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
            toast.message(t("generateStarted"));
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
        // Treat unusable/invalid bound crop as unbound and force the gate.
        if (!isUsableCrop(boundCrop)) {
            if (!isUsableCrop(pickCrop)) {
                toast.error(t("cropRequired"));
                return;
            }
            await runGenerate(pickCrop.trim());
            return;
        }
        await runGenerate();
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
                    {needsCrop && (
                        <div className="rounded-lg border border-warning/40 bg-warning-subtle p-3 space-y-2">
                            <p className="text-xs text-warning leading-relaxed">
                                {t("cropGateHint")}
                            </p>
                            <CropSelect
                                value={pickCrop}
                                onChange={setPickCrop}
                                placeholder={t("selectCrop")}
                                required
                            />
                            <div className="flex flex-wrap gap-2">
                                <Button size="sm" variant="outline" onClick={handleBindOnly}>
                                    {t("bindCrop")}
                                </Button>
                            </div>
                        </div>
                    )}

                    {!needsCrop && (
                        <p className="text-[11px] text-muted-foreground">
                            {t("boundCrop")}: <span className="font-medium text-foreground">{boundCrop}</span>
                        </p>
                    )}

                    <div className="flex flex-wrap gap-2">
                        <Button
                            size="sm"
                            onClick={handleGenerate}
                            disabled={inFlight || (needsCrop && !pickCrop)}
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
                                  : t("generate")}
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
        </div>
    );
}

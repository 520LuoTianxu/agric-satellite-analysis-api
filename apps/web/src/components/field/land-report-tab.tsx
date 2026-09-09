"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { assessmentApi, fieldsApi, jobsApi, type NdviJob } from "@/lib/api";
import CropSelect from "@/components/field/crop-select";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
    CheckCircle2,
    Download,
    FileText,
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

function lightBadge(light?: string) {
    if (light === "绿" || light === "green") return "bg-emerald-100 text-emerald-800";
    if (light === "黄" || light === "yellow") return "bg-amber-100 text-amber-800";
    if (light === "红" || light === "red") return "bg-red-100 text-red-800";
    return "bg-muted text-muted-foreground";
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
    const [boundCrop, setBoundCrop] = useState(cropType || "");
    const [pickCrop, setPickCrop] = useState("");
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    useEffect(() => {
        setBoundCrop(cropType || "");
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

    useEffect(() => {
        refreshMeta();
        return () => {
            if (pollRef.current) clearInterval(pollRef.current);
        };
    }, [refreshMeta]);

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
        if (!boundCrop) {
            if (!pickCrop) {
                toast.error(t("cropRequired"));
                return;
            }
            await runGenerate(pickCrop);
            return;
        }
        await runGenerate();
    };

    const handleBindOnly = async () => {
        if (!pickCrop) {
            toast.error(t("cropRequired"));
            return;
        }
        try {
            await fieldsApi.update(fieldId, { crop_type: pickCrop });
            setBoundCrop(pickCrop);
            onCropBound?.(pickCrop);
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
    const score = progress.score as number | undefined;
    const grade = progress.grade as string | undefined;
    const light = progress.light as string | undefined;
    const oneLiner = progress.one_liner as string | undefined;
    const hasPdf = latest?.status === "succeeded" && Boolean(progress.object_key);
    const inFlight =
        generating || latest?.status === "pending" || latest?.status === "running";
    const needsCrop = !boundCrop;

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
                </div>
            ) : (
                <>
                    {needsCrop && (
                        <div className="rounded-lg border border-amber-300/60 bg-amber-50 dark:bg-amber-950/30 p-3 space-y-2">
                            <p className="text-xs text-amber-900 dark:text-amber-100 leading-relaxed">
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

                    {latest && (
                        <div className="rounded-lg border bg-card p-3 space-y-2">
                            <div className="flex items-center justify-between gap-2">
                                <span className="text-xs text-muted-foreground">
                                    {t("status")}
                                </span>
                                <Badge variant="secondary" className="text-xs">
                                    {latest.status === "succeeded" && (
                                        <CheckCircle2 className="h-3 w-3 mr-1 text-emerald-600" />
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

                            {typeof score === "number" && (
                                <div className="flex items-end gap-3 pt-1">
                                    <div
                                        className={`rounded-md px-3 py-2 text-center min-w-[4.5rem] ${lightBadge(light)}`}
                                    >
                                        <div className="text-2xl font-bold leading-none">
                                            {score}
                                        </div>
                                        <div className="text-[10px] mt-1">
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

                    <div className="rounded-md bg-muted/50 p-3 text-[11px] text-muted-foreground space-y-1 leading-relaxed">
                        <p>{t("noteVigor")}</p>
                        <p>{t("noteFlood")}</p>
                        <p>{t("noteArea")}</p>
                    </div>
                </>
            )}
        </div>
    );
}

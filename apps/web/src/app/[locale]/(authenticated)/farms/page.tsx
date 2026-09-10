"use client";

import React, { useEffect, useState } from "react";
import { Link } from "@/i18n/navigation";
import { farmsApi } from "@/lib/api";
import type { Farm } from "@/lib/api";
import {
    Tractor,
    Plus,
    ChevronRight,
    MapPin,
    Search,
    ChevronLeft,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useTranslations } from "next-intl";
import { CreateFarmModal } from "@/components/create-farm-modal";

const PAGE_SIZE = 20;

export default function FarmsListPage() {
    const t = useTranslations("farmsPage");
    const [farms, setFarms] = useState<Farm[]>([]);
    const [total, setTotal] = useState(0);
    const [loading, setLoading] = useState(true);
    const [q, setQ] = useState("");
    const [debouncedQ, setDebouncedQ] = useState("");
    const [offset, setOffset] = useState(0);

    useEffect(() => {
        const id = window.setTimeout(() => setDebouncedQ(q), 300);
        return () => window.clearTimeout(id);
    }, [q]);

    useEffect(() => {
        setOffset(0);
    }, [debouncedQ]);


    useEffect(() => {
        let cancelled = false;
        (async () => {
            setLoading(true);
            try {
                const res = await farmsApi.list(PAGE_SIZE, offset, debouncedQ || undefined);
                if (!cancelled) {
                    setFarms(res.items);
                    setTotal(res.total);
                }
            } catch (err) {
                console.error(err);
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => { cancelled = true; };
    }, [offset, debouncedQ]);

    const page = Math.floor(offset / PAGE_SIZE) + 1;
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const canPrev = offset > 0;
    const canNext = offset + PAGE_SIZE < total;

    return (
        <div className="p-6 lg:p-8 max-w-6xl mx-auto">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between mb-8">
                <div>
                    <h1 className="text-2xl font-bold tracking-tight">{t("title")}</h1>
                    <p className="mt-1 text-sm text-muted-foreground">{t("subtitle", { count: total })}</p>
                </div>
                <CreateFarmModal>
                    <Button>
                        <Plus className="h-4 w-4 mr-2" />
                        {t("newFarm")}
                    </Button>
                </CreateFarmModal>
            </div>

            <div className="mb-6 relative max-w-md">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <input
                    type="search"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="Search farms by name…"
                    className="w-full rounded-md border bg-background py-2 pl-9 pr-3 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                />
            </div>

            {loading ? (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {[1, 2, 3].map((i) => (
                        <Skeleton key={i} className="h-36 rounded-lg" />
                    ))}
                </div>
            ) : farms.length === 0 ? (
                <Card className="border-2 border-dashed p-16 text-center">
                    <CardContent className="p-0">
                        <Tractor className="mx-auto h-12 w-12 text-muted-foreground/40" />
                        <p className="mt-4 text-base font-medium">{t("noFarmsTitle")}</p>
                        <p className="mt-2 text-sm text-muted-foreground">
                            {t("noFarmsDesc")}
                        </p>
                        <CreateFarmModal>
                            <Button className="mt-6">
                                <Plus className="h-4 w-4 mr-2" />
                                {t("createFarm")}
                            </Button>
                        </CreateFarmModal>
                    </CardContent>
                </Card>
            ) : (
                <>
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {farms.map((farm) => (
                            <Link key={farm.id} href={`/farms/${farm.id}`} className="group">
                                <Card className="h-full hover:border-primary/30 hover:shadow-md transition-all">
                                    <CardContent className="p-5">
                                        <div className="flex items-start justify-between">
                                            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary-subtle group-hover:bg-primary-subtle transition-colors">
                                                <Tractor className="h-5 w-5 text-primary" />
                                            </div>
                                            <ChevronRight className="h-4 w-4 text-muted-foreground/40 group-hover:text-primary transition-colors mt-1" />
                                        </div>
                                        <h3 className="mt-3 text-base font-semibold">{farm.name}</h3>
                                        {(farm.region || farm.country) && (
                                            <p className="mt-1 flex items-center gap-1 text-sm text-muted-foreground">
                                                <MapPin className="h-4 w-4" />
                                                {[farm.region, farm.country].filter(Boolean).join(", ")}
                                            </p>
                                        )}
                                        <p className="mt-2 text-xs text-muted-foreground/60">
                                            {t("created", { date: new Date(farm.created_at).toLocaleDateString() })}
                                        </p>
                                    </CardContent>
                                </Card>
                            </Link>
                        ))}
                    </div>

                    <div className="mt-8 flex items-center justify-between gap-4">
                        <p className="text-sm text-muted-foreground tabular-nums">
                            Page {page} / {totalPages}
                            <span className="mx-2 text-muted-foreground/40">·</span>
                            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
                        </p>
                        <div className="flex gap-2">
                            <Button
                                variant="outline"
                                size="sm"
                                disabled={!canPrev}
                                onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
                            >
                                <ChevronLeft className="h-4 w-4 mr-1" />
                                Prev
                            </Button>
                            <Button
                                variant="outline"
                                size="sm"
                                disabled={!canNext}
                                onClick={() => setOffset((o) => o + PAGE_SIZE)}
                            >
                                Next
                                <ChevronRight className="h-4 w-4 ml-1" />
                            </Button>
                        </div>
                    </div>
                </>
            )}
        </div>
    );
}

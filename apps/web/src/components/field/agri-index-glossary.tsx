"use client";

import React, { useEffect, useMemo, useState } from "react";
import { CircleHelp } from "lucide-react";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
    DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

export type GlossaryKey =
    | "sentinel2"
    | "sentinel1"
    | "ndvi"
    | "evi"
    | "drought"
    | "flood"
    | "vv"
    | "vh"
    | "ndmi"
    | "ndre"
    | "cire"
    | "mndwi";

export interface GlossaryEntry {
    key: GlossaryKey;
    /** Short nav / title label */
    title: string;
    /** Optional subtitle under title */
    subtitle?: string;
    /** 2–4 plain-Chinese sentences */
    body: string;
    /** Optional one-line tip */
    tip?: string;
    imageSrc: string;
    imageAlt: string;
}

/** Map panel series keys → glossary entry (sentinel keys have no series). */
const SERIES_TO_GLOSSARY: Record<string, GlossaryKey> = {
    ndvi: "ndvi",
    evi: "evi",
    drought: "drought",
    flood: "flood",
    vv: "vv",
    vh: "vh",
    ndmi: "ndmi",
    ndre: "ndre",
    cire: "cire",
    mndwi: "mndwi",
};

export const GLOSSARY_ENTRIES: GlossaryEntry[] = [
    {
        key: "sentinel2",
        title: "哨兵二号（Sentinel-2）",
        subtitle: "光学卫星 · 看颜色与长势",
        body: "哨兵二号像天上的彩色相机，能拍到庄稼的绿黄变化，用来算 NDVI、EVI、NDMI、NDRE、CIRE、MNDWI 和干旱等指数。有云、雾或夜间时拍不清楚，图上常会缺数据或偏红。白天晴好时最适合看作物长势。",
        tip: "云多的日子光学图参考价值低，可改看雷达（哨兵一号）。",
        imageSrc: "/glossary/sentinel-s1-s2.png",
        imageAlt: "哨兵一号与哨兵二号示意",
    },
    {
        key: "sentinel1",
        title: "哨兵一号（Sentinel-1）",
        subtitle: "雷达卫星 · 穿云见水",
        body: "哨兵一号发雷达波再听回声，阴天、夜里也能看地。水面、湿土会把波“镜面反射”走，图上往往更暗；植株、建筑物散射更杂。本面板的 VV、VH 和洪涝主要来自它。",
        tip: "怀疑积水时优先看洪涝 / VV，不必等晴天。",
        imageSrc: "/glossary/sentinel-s1-s2.png",
        imageAlt: "哨兵一号与哨兵二号示意",
    },
    {
        key: "ndvi",
        title: "NDVI（植被指数）",
        subtitle: "长势好不好，一眼看绿",
        body: "NDVI 用红光和近红外估测植被活力。色斑图上越绿、数值越高，通常长势越好、覆盖越密；偏棕、数值偏低，多半是稀疏、未熟、受灾或裸地。旺季若整块地长期偏低，要怀疑未种植或严重胁迫。",
        tip: "看趋势比单日绝对值更稳：连降多日再排查。",
        imageSrc: "/glossary/ndvi.png",
        imageAlt: "NDVI 示意：绿高棕低",
    },
    {
        key: "evi",
        title: "EVI（增强植被指数）",
        subtitle: "密植时比 NDVI 更“扛饱和”",
        body: "EVI 和 NDVI 一样反映绿意与长势，但对大气和土壤背景做了校正。作物冠层很密时，NDVI 容易“顶满”分不出好坏，EVI 还能拉开差距。稀植或刚出苗时两者往往接近。",
        tip: "旺季密植地块，对比长势差异可优先看 EVI。",
        imageSrc: "/glossary/evi.png",
        imageAlt: "EVI 示意",
    },
    {
        key: "drought",
        title: "干旱",
        subtitle: "光学指数看缺水胁迫",
        body: "干旱模式结合植被与水分相关指数（类似 NDDI），突出“长势还在但叶子偏干”的区域。色斑偏红、数值偏高，一般表示更干、更紧缺水分；偏绿/偏低则相对湿润。受云影响，需挑低云日期看。",
        tip: "连旱多日再结合气象与田间墒情确认，勿单凭一天下结论。",
        imageSrc: "/glossary/drought.png",
        imageAlt: "干旱指数示意：越红越干",
    },
    {
        key: "flood",
        title: "洪涝",
        subtitle: "雷达看积水",
        body: "洪涝用哨兵一号雷达：平静积水像镜子，回波很弱，色斑图上往往发暗。大片持续偏暗、且与沟塘河道相连时，更可能是积水；刚耕耙的湿土有时也会偏暗，需结合地形与时间判断。",
        tip: "大雨后连续几天对照 VV/洪涝，比等光学晴空更快。",
        imageSrc: "/glossary/flood.png",
        imageAlt: "洪涝雷达暗回波示意",
    },
    {
        key: "vv",
        title: "VV（雷达同极化）",
        subtitle: "对平坦水面、裸土敏感",
        body: "VV 是雷达“竖发竖收”。平坦水面、湿润光滑土壤回波弱（数值低/偏暗）；粗糙地表或有结构时回波更强。看洪涝、地表湿度变化时常用 VV。",
        tip: "与 VH 对照：水面常是 VV、VH 都偏弱。",
        imageSrc: "/glossary/radar-vv-vh.png",
        imageAlt: "VV 与 VH 雷达示意",
    },
    {
        key: "vh",
        title: "VH（雷达交叉极化）",
        subtitle: "更多来自植株散射",
        body: "VH 是“竖发横收”，对作物茎叶等体散射更敏感。植株茂密时 VH 往往相对抬升；裸地或光滑水面则偏弱。可与 VV 一起判断“是水、是土，还是有庄稼”。",
        tip: "生长季 VH 走强通常说明冠层在发育。",
        imageSrc: "/glossary/radar-vv-vh.png",
        imageAlt: "VV 与 VH 雷达示意",
    },
    {
        key: "ndmi",
        title: "NDMI（归一化水分指数）",
        subtitle: "叶子和冠层含水量",
        body: "NDMI 用近红外与短波红外估测叶片/冠层水分。数值偏高、色调偏湿润，说明水分相对充足；偏低则可能缺水或叶片干枯。干旱模式也会参考它。",
        tip: "与 NDVI 同降时，更像整体受旱或衰老，而不只是缺绿。",
        imageSrc: "/glossary/ndmi.png",
        imageAlt: "NDMI 水分示意",
    },
    {
        key: "ndre",
        title: "NDRE（红边指数）",
        subtitle: "叶绿素与早期养分胁迫",
        body: "NDRE 盯住红边波段，对叶绿素变化比普通 NDVI 更敏感。旺季若 NDVI 还高但 NDRE 已掉，可能是早期缺肥或缺素信号。适合在封垄后排查局部黄化、长势不齐。",
        tip: "田间取样前，可先用 NDRE 圈出可疑斑块。",
        imageSrc: "/glossary/ndre-cire.png",
        imageAlt: "NDRE 与 CIRE 示意",
    },
    {
        key: "cire",
        title: "CIRE（叶绿素红边指数）",
        subtitle: "叶绿素密度与光合能力",
        body: "CIRE 侧重叶绿素含量和光合强弱。偏高通常叶片更绿、光合潜力更好；偏低可能缺肥、病害或衰老。与 NDRE 同源红边信息，可互相印证。",
        tip: "追肥前后各看一次，便于评估效果。",
        imageSrc: "/glossary/ndre-cire.png",
        imageAlt: "NDRE 与 CIRE 示意",
    },
    {
        key: "mndwi",
        title: "MNDWI（改进型水体指数）",
        subtitle: "区分开阔水面与旱地",
        body: "MNDWI 专门用来找开阔水面：高值/偏蓝区域更像河塘、淹水；低值多为旱地或植被。与雷达洪涝互补——光学晴空日可用 MNDWI 勾水面边界。",
        tip: "有云时改看哨兵一号洪涝；晴空时 MNDWI 边界更清晰。",
        imageSrc: "/glossary/mndwi.png",
        imageAlt: "MNDWI 水体示意",
    },
];

function resolveInitialKey(initialKey?: string | null): GlossaryKey {
    if (!initialKey) return "ndvi";
    if (SERIES_TO_GLOSSARY[initialKey]) return SERIES_TO_GLOSSARY[initialKey]!;
    if (GLOSSARY_ENTRIES.some((e) => e.key === initialKey)) {
        return initialKey as GlossaryKey;
    }
    return "ndvi";
}

export interface AgriIndexGlossaryProps {
    /** Current panel series — used as default selected topic when dialog opens */
    initialKey?: string | null;
    /** Optional class on the trigger button */
    triggerClassName?: string;
    /** Show text label「指数说明」next to icon (default: icon + short label on sm+) */
    showLabel?: boolean;
}

export function AgriIndexGlossary({
    initialKey = null,
    triggerClassName,
    showLabel = true,
}: AgriIndexGlossaryProps) {
    const [open, setOpen] = useState(false);
    const [selected, setSelected] = useState<GlossaryKey>(() =>
        resolveInitialKey(initialKey),
    );

    useEffect(() => {
        if (open) {
            setSelected(resolveInitialKey(initialKey));
        }
    }, [open, initialKey]);

    const entry = useMemo(
        () => GLOSSARY_ENTRIES.find((e) => e.key === selected) ?? GLOSSARY_ENTRIES[0]!,
        [selected],
    );

    return (
        <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
                <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className={cn(
                        "h-7 shrink-0 gap-1 px-2 text-xs",
                        showLabel ? "sm:px-2.5" : "w-7 p-0",
                        triggerClassName,
                    )}
                    title="指数说明"
                    aria-label="指数说明"
                >
                    <CircleHelp className="h-3.5 w-3.5" />
                    {showLabel ? <span>指数说明</span> : null}
                </Button>
            </DialogTrigger>
            <DialogContent
                className={cn(
                    "flex max-h-[min(88vh,40rem)] w-[min(92vw,48rem)] max-w-[48rem] flex-col gap-0 overflow-hidden p-0",
                    "sm:rounded-lg",
                )}
            >
                <DialogHeader className="shrink-0 space-y-1 border-b px-4 py-3 pr-12 text-left">
                    <DialogTitle className="text-base">指数与卫星说明</DialogTitle>
                    <DialogDescription className="text-xs leading-relaxed">
                        用人话解释本面板用到的卫星和指数：看什么、高低代表什么。点左侧条目切换。
                    </DialogDescription>
                </DialogHeader>

                {/* Mobile: topic select */}
                <div className="shrink-0 border-b px-3 py-2 md:hidden">
                    <Select
                        value={selected}
                        onValueChange={(v) => setSelected(v as GlossaryKey)}
                    >
                        <SelectTrigger className="h-8 w-full text-xs">
                            <SelectValue placeholder="选择说明条目" />
                        </SelectTrigger>
                        <SelectContent className="max-h-72">
                            {GLOSSARY_ENTRIES.map((e) => (
                                <SelectItem key={e.key} value={e.key} className="text-xs">
                                    {e.title}
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                </div>

                <div className="flex min-h-0 flex-1 flex-col md:flex-row">
                    {/* Desktop left nav */}
                    <nav
                        className="hidden w-44 shrink-0 overflow-y-auto border-r bg-muted/30 py-2 md:block lg:w-52"
                        aria-label="指数条目"
                    >
                        {GLOSSARY_ENTRIES.map((e) => {
                            const active = e.key === selected;
                            return (
                                <button
                                    key={e.key}
                                    type="button"
                                    onClick={() => setSelected(e.key)}
                                    className={cn(
                                        "flex w-full px-3 py-1.5 text-left text-[11px] leading-snug transition-colors",
                                        active
                                            ? "bg-primary/10 font-medium text-primary"
                                            : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                                    )}
                                >
                                    {e.title}
                                </button>
                            );
                        })}
                    </nav>

                    {/* Detail pane */}
                    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
                        <div className="space-y-3">
                            <div>
                                <h3 className="text-sm font-semibold tracking-tight">
                                    {entry.title}
                                </h3>
                                {entry.subtitle ? (
                                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                                        {entry.subtitle}
                                    </p>
                                ) : null}
                            </div>
                            <div className="overflow-hidden rounded-lg border bg-muted/20">
                                {/* eslint-disable-next-line @next/next/no-img-element */}
                                <img
                                    src={entry.imageSrc}
                                    alt={entry.imageAlt}
                                    className="mx-auto max-h-52 w-full object-contain p-2 sm:max-h-64"
                                />
                            </div>
                            <p className="text-xs leading-relaxed text-foreground/90 whitespace-pre-line">
                                {entry.body}
                            </p>
                            {entry.tip ? (
                                <p className="rounded-md border border-primary/20 bg-primary-subtle/40 px-2.5 py-1.5 text-[11px] leading-snug text-foreground/80">
                                    <span className="font-medium text-primary">小贴士：</span>
                                    {entry.tip}
                                </p>
                            ) : null}
                        </div>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}

export default AgriIndexGlossary;

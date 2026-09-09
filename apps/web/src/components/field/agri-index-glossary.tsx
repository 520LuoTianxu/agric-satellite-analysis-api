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

/** Public OSS base for glossary plates (uploaded via scripts/upload_to_oss.py). */
const GLOSSARY_ASSET_BASE = (
    process.env.NEXT_PUBLIC_GLOSSARY_ASSET_BASE ||
    "https://agric-dev.oss-cn-beijing.aliyuncs.com/web/glossary"
).replace(/\/$/, "");

function glossaryAsset(fileName: string): string {
    return `${GLOSSARY_ASSET_BASE}/${fileName}`;
}

export const GLOSSARY_ENTRIES: GlossaryEntry[] = [
    {
        key: "sentinel2",
        title: "哨兵二号（Sentinel-2）",
        subtitle: "光学多光谱 · 10/20/60 m · 约 5 天重访",
        body: "哨兵二号是欧空局 Copernicus 计划的光学多光谱卫星，搭载 MSI 传感器，约 13 个波段覆盖可见光、红边、近红外与短波红外。\n\n双星组网重访约 5 天，空间分辨率 10/20/60 m，适合农田、水体与地表覆盖监测。本面板的 NDVI、EVI、NDMI、NDRE、CIRE、MNDWI 与干旱等光学指数主要来自它。\n\n有云、雾或夜间时光学观测受限，图上可能缺数或质量偏差；晴空日最适合看长势与真彩色。",
        tip: "云多时改看哨兵一号雷达（VV/VH/洪涝）。",
        imageSrc: glossaryAsset("sentinel-s2.png"),
        imageAlt: "哨兵二号多光谱说明",
    },
    {
        key: "sentinel1",
        title: "哨兵一号（Sentinel-1）",
        subtitle: "C 波段 SAR · 全天时全天候 · 可穿云",
        body: "哨兵一号是 Copernicus 的主动微波雷达卫星（C 波段 SAR），主动发射并接收回波，不依赖阳光。\n\n可穿透云层与薄雨雾，昼夜均可观测，对地表粗糙度、水分与几何结构敏感。本面板的 VV、VH 与洪涝主要来自它。\n\n平静水面常呈弱回波（偏暗）；植被、建筑等结构散射更强（偏亮）。",
        tip: "灾后积水排查不必等晴天，优先看洪涝/VV。",
        imageSrc: glossaryAsset("sentinel-s1.png"),
        imageAlt: "哨兵一号雷达说明",
    },
    {
        key: "ndvi",
        title: "NDVI（归一化植被指数）",
        subtitle: "最常用的植被长势指标",
        body: "NDVI = (NIR − Red) / (NIR + Red)，值域通常约 −1～1。健康植被强烈反射近红外、吸收红光，NDVI 偏高。\n\n怎么看：接近 0 或负值多为水体、裸地或建筑；0.2～0.5 多为一般植被；>0.5 通常长势较好、覆盖更密。\n\n可理解为作物的「健康分」，适合生长季监测、覆盖估计与灾损对比；密植时可能饱和，可对照 EVI/NDRE。",
        tip: "看多日趋势比单日绝对值更稳。",
        imageSrc: glossaryAsset("ndvi.png"),
        imageAlt: "NDVI 专业说明图",
    },
    {
        key: "evi",
        title: "EVI（增强植被指数）",
        subtitle: "高覆盖下更稳，密植不易饱和",
        body: "EVI 在 NDVI 基础上引入蓝光与校正项，减弱大气与土壤背景干扰，在高覆盖植被区仍能更好区分细微差异。\n\n相对 NDVI：密植不易「顶满」、抗大气干扰更强，更适合精细时序与高生物量地块。\n\n通俗理解：若 NDVI 是健康分，EVI 更像升级版评分，干扰更少、结果更稳。",
        tip: "旺季密植地块对比长势差异可优先看 EVI。",
        imageSrc: glossaryAsset("evi.png"),
        imageAlt: "EVI 专业说明图",
    },
    {
        key: "drought",
        title: "干旱",
        subtitle: "水分长期不足对土壤与作物的胁迫",
        body: "干旱指降水偏少、蒸发偏强或土壤失水导致可用水不足，农业上直接影响出苗、生长与产量。\n\n判读要点：土壤变干开裂、NDVI/EVI 走低、叶温升高/萎蔫，以及持续时长与影响范围。色斑越偏干（本面板多为偏红），胁迫往往越重。\n\n宜结合降水、墒情与多日光学指数综合判断，避免单日定论。",
        tip: "连旱多日再对照田间墒情与气象更可靠。",
        imageSrc: glossaryAsset("drought.png"),
        imageAlt: "干旱监测说明图",
    },
    {
        key: "flood",
        title: "洪涝",
        subtitle: "地表积水与淹没范围识别",
        body: "洪涝由强降雨、河湖水位上涨、排水不畅或低洼积水引起，农业上易造成淹苗、根系缺氧与减产。\n\n雷达（哨兵一号）可穿云、夜间监测积水；光学（哨兵二号）在晴空日可勾画水体边界。本面板洪涝模式主要依据雷达后向散射阈值。\n\n平静积水回波弱、色斑偏暗；需结合地形、沟塘与连续日期排除湿土误判。",
        tip: "暴雨后连续对照 VV/洪涝，比等光学晴空更快。",
        imageSrc: glossaryAsset("flood.png"),
        imageAlt: "洪涝监测说明图",
    },
    {
        key: "vv",
        title: "VV（雷达同极化）",
        subtitle: "垂发垂收 · 对水面与粗糙度敏感",
        body: "VV 表示垂直发射、垂直接收的同极化 SAR 通道，常用后向散射系数 σ⁰（dB）表征。\n\n光滑水面呈镜面反射，VV 回波弱（偏暗）；粗糙地表、植被或建筑散射更强（偏亮）。适合水体/洪涝提取、土壤湿度与地表变化监测。\n\n可理解为「竖着打出去再竖着收回来」的雷达手电筒，反映表面粗糙与含水量信息。",
        tip: "与 VH 对照：开阔水面常 VV、VH 都偏弱。",
        imageSrc: glossaryAsset("vv.png"),
        imageAlt: "VV 雷达同极化说明图",
    },
    {
        key: "vh",
        title: "VH（雷达交叉极化）",
        subtitle: "垂发横收 · 对植株体散射敏感",
        body: "VH 表示垂直发射、水平接收的交叉极化通道，对植被冠层体散射更敏感，能反映结构、粗糙度与一定水分信息。\n\n特点：穿云全天候；水体 VH 通常很低（暗）；农田中等；森林/密植偏高（亮）。与 VV 互补，利于区分「水 / 土 / 有庄稼」。\n\n生长季 VH 走强常对应冠层发育；洪涝时积水区 VH 也偏弱。",
        tip: "长势与结构变化可重点看 VH 时序。",
        imageSrc: glossaryAsset("vh.png"),
        imageAlt: "VH 雷达交叉极化说明图",
    },
    {
        key: "ndmi",
        title: "NDMI（归一化水分指数）",
        subtitle: "监测植被与土壤水分状况",
        body: "NDMI = (NIR − SWIR) / (NIR + SWIR)。近红外与短波红外对水分响应不同，用以反映叶片/冠层含水量与湿润程度。\n\n高值：水分较充足、长势相对健康；低值：偏干、干旱胁迫或土壤更干。常用于干旱监测、灌溉管理与作物水分诊断。\n\n通俗理解：像给庄稼和土壤看「有多湿」。",
        tip: "与 NDVI 同降时，更像整体受旱或衰老。",
        imageSrc: glossaryAsset("ndmi.png"),
        imageAlt: "NDMI 水分指数说明图",
    },
    {
        key: "ndre",
        title: "NDRE（红边指数）",
        subtitle: "中后期长势与叶绿素变化更敏感",
        body: "NDRE = (NIR − RedEdge) / (NIR + RedEdge)，利用红边波段对叶绿素变化敏感，密植时比 NDVI 更不易饱和。\n\n高值：叶绿素较足、长势稳健；低值：可能缺肥、早衰或胁迫。适合玉米等作物中后期监测、氮素诊断与田块精细管理。\n\n若 NDVI 仍高而 NDRE 先掉，往往是早期养分压力信号。",
        tip: "封垄后可用 NDRE 圈出可疑黄化斑块再取样。",
        imageSrc: glossaryAsset("ndre.png"),
        imageAlt: "NDRE 红边指数说明",
    },
    {
        key: "cire",
        title: "CIRE（叶绿素红边指数）",
        subtitle: "叶绿素含量与营养诊断",
        body: "CIRE = (NIR / RedEdge) − 1，基于红边波段，对叶绿素浓度变化敏感。\n\n高值：叶绿素更厚实、营养与光合潜力更好；低值：可能缺素、病害或生长受抑。用于叶绿素监测、施肥决策与营养诊断。\n\n与 NDRE 同源红边信息，可互相印证；比 NDVI 更适合高生物量阶段。",
        tip: "追肥前后各看一次，便于评估肥效。",
        imageSrc: glossaryAsset("cire.png"),
        imageAlt: "CIRE 叶绿素红边说明图",
    },
    {
        key: "mndwi",
        title: "MNDWI（改进型水体指数）",
        subtitle: "提取水体、积水与洪涝范围",
        body: "MNDWI = (Green − SWIR) / (Green + SWIR)。用水体对绿光与短波红外的响应差，更好区分水面与建筑物、裸土等。\n\n高值：更可能是河塘、积水或洪涝区；低值：多为植被、土壤或城镇。相对传统 NDWI，城镇区抑制建筑噪声更好。\n\n与雷达洪涝互补：晴空光学日边界更清晰。",
        tip: "有云时改看哨兵一号洪涝。",
        imageSrc: glossaryAsset("mndwi.png"),
        imageAlt: "MNDWI 水体指数说明图",
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
                        "h-7 shrink-0 gap-1 px-2 text-xs border-primary/30 text-primary hover:bg-primary/10",
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
                    "flex max-h-[min(90vh,52rem)] w-[min(96vw,64rem)] max-w-[min(96vw,64rem)] flex-col gap-0 overflow-hidden p-0",
                    "sm:rounded-lg",
                )}
            >
                <DialogHeader className="shrink-0 space-y-1 border-b px-4 py-3 pr-12 text-left">
                    <DialogTitle className="text-base">指数与卫星说明</DialogTitle>
                    <DialogDescription className="text-xs leading-relaxed">
                        配专业示意图，说明卫星与指数原理与用法。点左侧条目切换。
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
                                    className="mx-auto max-h-[min(55vh,28rem)] w-full object-contain p-1 sm:p-2"
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

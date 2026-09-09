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
        subtitle: "光学多光谱 · 植被与水体指数来源",
        body: "【它是什么】ESA Copernicus 光学多光谱卫星（MSI，约 13 个波段），双星协同。\n\n【怎么看】空间分辨率约 10/20/60 m，重访约 5 天。晴空白天成像清晰；云、雾、夜间会缺数据或噪声增大。\n\n【应用】本面板 NDVI、EVI、NDMI、NDRE、CIRE、MNDWI 及干旱等光学指数主要来自它。",
        tip: "云量高时光学参考价值下降，可改看哨兵一号雷达（VV/VH/洪涝）。",
        imageSrc: "/glossary/sentinel-s2.png",
        imageAlt: "哨兵二号光学卫星示意",
    },
    {
        key: "sentinel1",
        title: "哨兵一号（Sentinel-1）",
        subtitle: "C 波段 SAR · 全天时全天候",
        body: "【它是什么】ESA Copernicus C 波段合成孔径雷达（SAR），主动微波成像。\n\n【怎么看】全天时、全天候、可穿云；回波强度敏感于地表粗糙度、水分与形变。平静水面回波弱（偏暗），粗糙面或体散射更强。\n\n【应用】本面板 VV、VH 与洪涝识别的主要数据来源。",
        tip: "怀疑积水或连阴天时，优先看洪涝 / VV，不必等待晴空光学。",
        imageSrc: "/glossary/sentinel-s1.png",
        imageAlt: "哨兵一号雷达卫星示意",
    },
    {
        key: "ndvi",
        title: "NDVI（归一化植被指数）",
        subtitle: "长势与覆盖 · 公式 (NIR−Red)/(NIR+Red)",
        body: "【它是什么】用近红外（NIR）与红光（Red）计算：NDVI = (NIR − Red) / (NIR + Red)，理论范围约 −1～1。\n\n【怎么看】偏高（色斑偏绿）通常表示植被活力强、覆盖密；偏低（偏棕）多见稀疏、未熟、受灾或裸地。水体与云影常为低值或负值。\n\n【应用】田块尺度筛查种植与长势；旺季整块长期偏低，需排查未种植或严重胁迫。可类比为「健康打分」——看趋势比单日绝对值更稳。",
        tip: "连降多日再排查，比单看一天绝对值更可靠。",
        imageSrc: "/glossary/ndvi.png",
        imageAlt: "NDVI 示意：绿高棕低",
    },
    {
        key: "evi",
        title: "EVI（增强植被指数）",
        subtitle: "引入蓝光校正 · 密植不易饱和",
        body: "【它是什么】在 NDVI 思路上引入蓝光波段，并对大气与土壤背景做校正的增强植被指数。\n\n【怎么看】同样反映绿意与长势；冠层很密时 NDVI 易「顶满」分不出好坏，EVI 仍能拉开差异。稀植或刚出苗阶段二者往往接近。\n\n【应用】旺季密植地块对比长势差异时，优先参考 EVI。",
        tip: "旺季密植田块，局部优劣对比可优先看 EVI。",
        imageSrc: "/glossary/evi.png",
        imageAlt: "EVI 示意",
    },
    {
        key: "drought",
        title: "干旱",
        subtitle: "水分长期不足 · 光学综合判读",
        body: "【它是什么】表征作物或土壤水分长期偏紧的胁迫状态，而非单次短时缺水。\n\n【怎么看】结合植被指数下降、水分相关指数（如 NDMI/NDDI 类）等综合判读：色斑偏红、数值偏高一般更干；偏绿/偏低相对湿润。光学受云影响，需挑选低云日期。\n\n【应用】连旱时段筛查胁迫斑块；务必结合田间墒情与气象，勿单凭一日遥感下结论。",
        tip: "连旱多日再对照气象与墒情确认，避免单日误判。",
        imageSrc: "/glossary/drought.png",
        imageAlt: "干旱指数示意：越红越干",
    },
    {
        key: "flood",
        title: "洪涝",
        subtitle: "地表积水 · 雷达穿云为主",
        body: "【它是什么】地表积水（淹水）状态识别，重点服务大雨后田块与沟塘周边排查。\n\n【怎么看】哨兵一号可穿云：平静积水镜面反射，回波弱，色斑往往偏暗。大片持续偏暗且与水系相连时，积水可能性更高；刚耕耙湿土有时也会偏暗，需结合地形与时序。晴空日可用光学（如 MNDWI）辅助勾边。\n\n【应用】灾后快速摸底积水范围与消退过程。",
        tip: "大雨后连续对照 VV/洪涝，通常比等待光学晴空更快。",
        imageSrc: "/glossary/flood.png",
        imageAlt: "洪涝雷达暗回波示意",
    },
    {
        key: "vv",
        title: "VV（雷达同极化）",
        subtitle: "垂发垂收 · σ⁰(dB) 对水面与裸土敏感",
        body: "【它是什么】雷达垂直发射、垂直接收的同极化通道；常用后向散射系数 σ⁰（分贝 dB）表示强度。\n\n【怎么看】平坦水面、湿润光滑土壤回波弱（σ⁰ 低、色斑偏暗）；粗糙地表或有结构时回波更强。\n\n【应用】洪涝识别、地表湿度与粗糙度变化的常用通道；可与 VH 对照解读。",
        tip: "与 VH 对照：开阔水面常见 VV、VH 均偏弱。",
        imageSrc: "/glossary/vv.png",
        imageAlt: "VV 同极化雷达示意",
    },
    {
        key: "vh",
        title: "VH（雷达交叉极化）",
        subtitle: "交叉极化 · 植株体散射更敏感",
        body: "【它是什么】雷达垂直发射、水平接收的交叉极化通道。\n\n【怎么看】对作物茎叶等体散射更敏感：冠层茂密时 VH 往往相对抬升；裸地或光滑水面则偏弱。\n\n【应用】与 VV 联读，区分「是水、是土，还是有庄稼」；生长季走强通常提示冠层在发育。",
        tip: "生长季 VH 走强，多与冠层发育相关；可与 VV 同屏对照。",
        imageSrc: "/glossary/radar-vv-vh.png",
        imageAlt: "VV 与 VH 雷达示意",
    },
    {
        key: "ndmi",
        title: "NDMI（归一化水分指数）",
        subtitle: "叶片与冠层含水量",
        body: "【它是什么】用近红外与短波红外估测叶片/冠层相对含水量的光学指数。\n\n【怎么看】数值偏高、色调偏湿润，说明水分相对充足；偏低可能缺水或叶片干枯。\n\n【应用】干旱筛查与水分胁迫辅助；干旱模式也会参考它。",
        tip: "与 NDVI 同降时，更像整体受旱或衰老，而不只是「缺绿」。",
        imageSrc: "/glossary/ndmi.png",
        imageAlt: "NDMI 水分示意",
    },
    {
        key: "ndre",
        title: "NDRE（红边归一化指数）",
        subtitle: "叶绿素与早期养分胁迫",
        body: "【它是什么】基于红边波段的植被指数，对叶绿素变化比普通 NDVI 更敏感。\n\n【怎么看】旺季若 NDVI 仍高而 NDRE 已降，可能是早期缺肥或缺素信号。\n\n【应用】封垄后排查局部黄化、长势不齐；田间取样前可先圈可疑斑块。",
        tip: "取样前用 NDRE 圈出可疑斑块，可提高田间效率。",
        imageSrc: "/glossary/ndre-cire.png",
        imageAlt: "NDRE 与 CIRE 示意",
    },
    {
        key: "cire",
        title: "CIRE（叶绿素红边指数）",
        subtitle: "叶绿素密度与光合潜力",
        body: "【它是什么】侧重叶绿素含量与光合强弱的红边指数。\n\n【怎么看】偏高通常叶片更绿、光合潜力更好；偏低可能缺肥、病害或衰老。\n\n【应用】与 NDRE 同源红边信息，可互相印证；追肥前后对比便于评估效果。",
        tip: "追肥前后各看一次，便于评估肥效与恢复情况。",
        imageSrc: "/glossary/ndre-cire.png",
        imageAlt: "NDRE 与 CIRE 示意",
    },
    {
        key: "mndwi",
        title: "MNDWI（改进型归一化水体指数）",
        subtitle: "开阔水面与旱地区分",
        body: "【它是什么】改进型归一化水体指数，专门用于识别开阔水面。\n\n【怎么看】高值/偏蓝区域更像河塘、淹水；低值多为旱地或植被。\n\n【应用】与雷达洪涝互补——光学晴空日可用 MNDWI 勾水面边界；有云时改看哨兵一号洪涝。",
        tip: "有云时改看哨兵一号洪涝；晴空时 MNDWI 边界通常更清晰。",
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

"use client";

import React, { useState, useEffect, useRef } from "react";
import { Link, usePathname } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { alertsApi } from "@/lib/api";
import {
    LayoutDashboard,
    Map,
    Tractor,
    Bell,
    PanelLeftClose,
    PanelLeftOpen,
    Menu,
    X,
    Leaf,
    ScrollText,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
    Tooltip,
    TooltipContent,
    TooltipProvider,
    TooltipTrigger,
} from "@/components/ui/tooltip";
import { ThemeToggle } from "@/components/theme-toggle";
import { LanguageSwitcher } from "@/components/language-switcher";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
    { href: "/dashboard" as const, labelKey: "dashboard" as const, icon: LayoutDashboard },
    { href: "/overview" as const, labelKey: "overview" as const, icon: Map },
    { href: "/farms" as const, labelKey: "farms" as const, icon: Tractor },
    { href: "/alerts" as const, labelKey: "alerts" as const, icon: Bell },
    { href: "/changelog" as const, labelKey: "changelog" as const, icon: ScrollText },
];

export function Sidebar() {
    const pathname = usePathname();
    const [mobileOpen, setMobileOpen] = useState(false);
    const [expanded, setExpanded] = useState(true);
    const tNav = useTranslations("nav");
    const tSidebar = useTranslations("sidebar");
    const tCommon = useTranslations("common");

    const isMapScreen = /^\/farms\/[^/]+\/fields\//.test(pathname);
    const wasMapScreen = useRef(false);
    useEffect(() => {
        if (isMapScreen && !wasMapScreen.current) setExpanded(false);
        wasMapScreen.current = isMapScreen;
    }, [isMapScreen]);

    const [openAlertCount, setOpenAlertCount] = useState(0);
    useEffect(() => {
        let cancelled = false;
        alertsApi.list({ status: "open", limit: 1 }).then((res) => {
            if (!cancelled) setOpenAlertCount(res.total);
        }).catch(() => { });
        return () => { cancelled = true; };
    }, []);

    return (
        <TooltipProvider delayDuration={0}>
            <Button
                variant="outline"
                size="icon"
                onClick={() => setMobileOpen(true)}
                className="fixed left-4 top-4 z-40 lg:hidden"
            >
                <Menu className="h-5 w-5" />
            </Button>

            {mobileOpen && (
                <div className="fixed inset-0 z-50 lg:hidden">
                    <div className="absolute inset-0 bg-black/30" onClick={() => setMobileOpen(false)} />
                    <div className="absolute left-0 top-0 h-full w-64 bg-background shadow-xl">
                        <Button
                            variant="ghost"
                            size="icon"
                            onClick={() => setMobileOpen(false)}
                            className="absolute right-3 top-3"
                        >
                            <X className="h-5 w-5" />
                        </Button>
                        <MobileSidebar
                            pathname={pathname}
                            openAlertCount={openAlertCount}
                            onNavigate={() => setMobileOpen(false)}
                            tNav={tNav}
                            tCommon={tCommon}
                        />
                    </div>
                </div>
            )}

            <aside
                className={cn(
                    "hidden lg:flex lg:flex-col lg:shrink-0 lg:border-r lg:bg-background lg:overflow-hidden",
                    "transition-[width] duration-300 ease-in-out",
                    expanded ? "lg:w-64" : "lg:w-16"
                )}
            >
                <div className="flex h-full flex-col overflow-hidden">
                    <div className={cn(
                        "flex items-center h-[68px] px-4 py-5",
                        expanded ? "justify-between" : "justify-center"
                    )}>
                        {expanded && (
                            <div className="flex items-center gap-2">
                                <Leaf className="h-7 w-7 shrink-0 text-primary" />
                                <span className="text-lg font-bold tracking-tight whitespace-nowrap">
                                    {tCommon("brandName")}
                                </span>
                            </div>
                        )}
                        <Tooltip>
                            <TooltipTrigger asChild>
                                <Button
                                    variant="ghost"
                                    size="icon"
                                    onClick={() => setExpanded(!expanded)}
                                    className="h-8 w-8 shrink-0 text-muted-foreground"
                                >
                                    {expanded ? (
                                        <PanelLeftClose className="h-4 w-4" />
                                    ) : (
                                        <PanelLeftOpen className="h-4 w-4" />
                                    )}
                                </Button>
                            </TooltipTrigger>
                            <TooltipContent side="right">
                                {expanded ? tSidebar("collapse") : tSidebar("expand")}
                            </TooltipContent>
                        </Tooltip>
                    </div>
                    <Separator />

                    <nav className={cn("flex-1 space-y-1 py-4", expanded ? "px-3" : "flex flex-col items-center px-1")}>
                        {NAV_ITEMS.map((item) => {
                            const active = pathname.startsWith(item.href);
                            const showBadge = item.labelKey === "alerts" && openAlertCount > 0;
                            const btn = (
                                <Button
                                    key={item.href}
                                    variant="ghost"
                                    className={cn(
                                        "gap-3 overflow-hidden relative h-auto",
                                        expanded ? "w-full justify-start px-3 py-2" : "h-10 w-10 justify-center p-0",
                                        active && "bg-primary-subtle text-primary",
                                    )}
                                    asChild
                                >
                                    <Link href={item.href}>
                                        <span className="relative shrink-0">
                                            <item.icon className="h-5 w-5" />
                                            {showBadge && !expanded && (
                                                <span className="absolute -top-1.5 -right-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive text-[10px] font-semibold text-destructive-foreground px-1">
                                                    {openAlertCount > 99 ? "99+" : openAlertCount}
                                                </span>
                                            )}
                                        </span>
                                        {expanded && (
                                            <>
                                                <span className="whitespace-nowrap flex-1">
                                                    {tNav(item.labelKey)}
                                                </span>
                                                {showBadge && (
                                                    <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-destructive text-[10px] font-semibold text-destructive-foreground px-1.5">
                                                        {openAlertCount > 99 ? "99+" : openAlertCount}
                                                    </span>
                                                )}
                                            </>
                                        )}
                                    </Link>
                                </Button>
                            );

                            if (expanded) return <React.Fragment key={item.href}>{btn}</React.Fragment>;

                            return (
                                <Tooltip key={item.href}>
                                    <TooltipTrigger asChild>{btn}</TooltipTrigger>
                                    <TooltipContent side="right">{tNav(item.labelKey)}</TooltipContent>
                                </Tooltip>
                            );
                        })}
                    </nav>

                    <Separator />
                    <div className="px-3 py-3">
                        <div className={`flex ${expanded ? "justify-center gap-1" : "flex-col items-center gap-1"} pt-1`}>
                            <ThemeToggle />
                            <LanguageSwitcher side="right" align="end" />
                        </div>
                    </div>
                </div>
            </aside>
        </TooltipProvider>
    );
}

function MobileSidebar({
    pathname,
    openAlertCount,
    onNavigate,
    tNav,
    tCommon,
}: {
    pathname: string;
    openAlertCount: number;
    onNavigate: () => void;
    tNav: (key: string) => string;
    tCommon: (key: string) => string;
}) {
    return (
        <div className="flex h-full flex-col">
            <div className="flex items-center gap-2 px-4 py-5">
                <Leaf className="h-7 w-7 text-primary" />
                <span className="text-lg font-bold tracking-tight">{tCommon("brandName")}</span>
            </div>
            <Separator />
            <nav className="flex-1 space-y-1 px-3 py-4">
                {NAV_ITEMS.map((item) => {
                    const active = pathname.startsWith(item.href);
                    const showBadge = item.labelKey === "alerts" && openAlertCount > 0;
                    return (
                        <Button
                            key={item.href}
                            variant="ghost"
                            className={cn(
                                "h-auto w-full justify-start gap-3 px-3 py-2",
                                active && "bg-primary-subtle text-primary",
                            )}
                            asChild
                        >
                            <Link href={item.href} onClick={onNavigate}>
                                <item.icon className="h-5 w-5" />
                                <span className="flex-1">{tNav(item.labelKey)}</span>
                                {showBadge && (
                                    <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-destructive text-[10px] font-semibold text-destructive-foreground px-1.5">
                                        {openAlertCount > 99 ? "99+" : openAlertCount}
                                    </span>
                                )}
                            </Link>
                        </Button>
                    );
                })}
            </nav>
            <Separator />
            <div className="px-3 py-3">
                <div className="flex justify-center gap-1 pt-2">
                    <ThemeToggle />
                    <LanguageSwitcher side="top" align="start" />
                </div>
            </div>
        </div>
    );
}

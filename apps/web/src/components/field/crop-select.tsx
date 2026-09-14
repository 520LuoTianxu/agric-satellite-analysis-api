"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Check, ChevronsUpDown } from "lucide-react";
import { cropsApi, type CropOption } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface CropSelectProps {
    value: string;
    onChange: (key: string) => void;
    id?: string;
    className?: string;
    required?: boolean;
    disabled?: boolean;
    placeholder?: string;
}

function cropLabel(c: CropOption) {
    return `${c.name_zh}（${c.name}）`;
}

export default function CropSelect({
    value,
    onChange,
    id,
    className,
    required,
    disabled,
    placeholder = "请选择作物",
}: CropSelectProps) {
    const [crops, setCrops] = useState<CropOption[]>([]);
    const [loading, setLoading] = useState(true);
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState("");

    useEffect(() => {
        let cancelled = false;
        cropsApi
            .list()
            .then((rows) => {
                if (!cancelled) setCrops(rows);
            })
            .catch(() => {
                if (!cancelled) setCrops([]);
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    useEffect(() => {
        if (!open) setQuery("");
    }, [open]);

    const selected = crops.find((c) => c.key === value);

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        if (!q) return crops;
        return crops.filter((c) => {
            const hay = `${c.name_zh} ${c.name} ${c.key}`.toLowerCase();
            return hay.includes(q);
        });
    }, [crops, query]);

    return (
        <DropdownMenu open={open} onOpenChange={setOpen}>
            <DropdownMenuTrigger asChild disabled={disabled || loading}>
                <Button
                    id={id}
                    type="button"
                    variant="outline"
                    role="combobox"
                    aria-expanded={open}
                    disabled={disabled || loading}
                    className={cn(
                        "h-9 w-full justify-between px-3 text-sm font-normal",
                        !selected && "text-muted-foreground",
                        className,
                    )}
                >
                    <span className="truncate">
                        {loading
                            ? "加载作物列表…"
                            : selected
                              ? cropLabel(selected)
                              : placeholder}
                    </span>
                    <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
                align="start"
                className="w-[min(22rem,calc(100vw-2rem))] p-0"
                onCloseAutoFocus={(e) => e.preventDefault()}
            >
                <div className="p-2">
                    <Input
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        onKeyDown={(e) => e.stopPropagation()}
                        placeholder="搜索作物…"
                        className="h-8"
                        autoFocus
                    />
                </div>
                <div className="max-h-64 overflow-y-auto p-1">
                    {!required && (
                        <DropdownMenuItem
                            onSelect={() => {
                                onChange("");
                                setOpen(false);
                            }}
                            className="text-muted-foreground"
                        >
                            {placeholder}
                        </DropdownMenuItem>
                    )}
                    {filtered.length === 0 ? (
                        <p className="px-2 py-3 text-center text-xs text-muted-foreground">
                            无匹配作物
                        </p>
                    ) : (
                        filtered.map((c) => (
                            <DropdownMenuItem
                                key={c.key}
                                onSelect={() => {
                                    onChange(c.key);
                                    setOpen(false);
                                }}
                            >
                                <Check
                                    className={cn(
                                        "mr-2 h-4 w-4",
                                        c.key === value ? "opacity-100" : "opacity-0",
                                    )}
                                />
                                {cropLabel(c)}
                            </DropdownMenuItem>
                        ))
                    )}
                </div>
            </DropdownMenuContent>
        </DropdownMenu>
    );
}

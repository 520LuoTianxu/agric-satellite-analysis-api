"use client";

import React, { useEffect, useState } from "react";
import { cropsApi, type CropOption } from "@/lib/api";
import { cn } from "@/lib/utils";

interface CropSelectProps {
    value: string;
    onChange: (key: string) => void;
    id?: string;
    className?: string;
    required?: boolean;
    disabled?: boolean;
    placeholder?: string;
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

    return (
        <select
            id={id}
            className={cn(
                "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm",
                "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                "disabled:cursor-not-allowed disabled:opacity-50",
                className,
            )}
            value={value}
            required={required}
            disabled={disabled || loading}
            onChange={(e) => onChange(e.target.value)}
        >
            <option value="">{loading ? "加载作物列表…" : placeholder}</option>
            {crops.map((c) => (
                <option key={c.key} value={c.key}>
                    {c.name_zh}（{c.name}）
                </option>
            ))}
        </select>
    );
}

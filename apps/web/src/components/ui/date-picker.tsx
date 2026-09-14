"use client";

import * as React from "react";
import { Calendar as CalendarIcon, ChevronLeft, ChevronRight } from "lucide-react";
import { useLocale } from "next-intl";
import { Button } from "@/components/ui/button";
import {
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

function parseISODate(value?: string | null): Date | null {
    if (!value) return null;
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!m) return null;
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return Number.isNaN(d.getTime()) ? null : d;
}

function toISODate(d: Date): string {
    const y = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${month}-${day}`;
}

function startOfDay(d: Date): Date {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

export interface DatePickerProps {
    value: string;
    onChange: (value: string) => void;
    id?: string;
    className?: string;
    disabled?: boolean;
    min?: string;
    max?: string;
    placeholder?: string;
}

export function DatePicker({
    value,
    onChange,
    id,
    className,
    disabled,
    min,
    max,
    placeholder,
}: DatePickerProps) {
    const locale = useLocale();
    const selected = parseISODate(value);
    const minDate = parseISODate(min);
    const maxDate = parseISODate(max);
    const [open, setOpen] = React.useState(false);
    const [cursor, setCursor] = React.useState(() => selected ?? new Date());

    React.useEffect(() => {
        const next = parseISODate(value);
        if (next) setCursor(next);
    }, [value]);

    const weekdayFmt = React.useMemo(
        () => new Intl.DateTimeFormat(locale, { weekday: "narrow" }),
        [locale],
    );
    const monthTitleFmt = React.useMemo(
        () => new Intl.DateTimeFormat(locale, { year: "numeric", month: "long" }),
        [locale],
    );
    const displayFmt = React.useMemo(
        () =>
            new Intl.DateTimeFormat(locale, {
                year: "numeric",
                month: "short",
                day: "numeric",
            }),
        [locale],
    );

    const weekdays = React.useMemo(() => {
        const base = new Date(2024, 0, 7);
        return Array.from({ length: 7 }, (_, i) => {
            const d = new Date(base);
            d.setDate(base.getDate() + i);
            return weekdayFmt.format(d);
        });
    }, [weekdayFmt]);

    const year = cursor.getFullYear();
    const month = cursor.getMonth();
    const firstWeekday = new Date(year, month, 1).getDay();
    const daysCount = new Date(year, month + 1, 0).getDate();

    const cells: Array<{ day: number | null; iso: string | null; disabled: boolean }> = [];
    for (let i = 0; i < firstWeekday; i++) {
        cells.push({ day: null, iso: null, disabled: true });
    }
    for (let day = 1; day <= daysCount; day++) {
        const d = startOfDay(new Date(year, month, day));
        const iso = toISODate(d);
        let isDisabled = false;
        if (minDate && d < startOfDay(minDate)) isDisabled = true;
        if (maxDate && d > startOfDay(maxDate)) isDisabled = true;
        cells.push({ day, iso, disabled: isDisabled });
    }

    const placeholderText =
        placeholder ??
        (locale.startsWith("zh")
            ? "选择日期"
            : locale.startsWith("es")
              ? "Elegir fecha"
              : "Pick a date");
    const clearLabel = locale.startsWith("zh")
        ? "清除"
        : locale.startsWith("es")
          ? "Borrar"
          : "Clear";

    return (
        <DropdownMenu open={open} onOpenChange={setOpen}>
            <DropdownMenuTrigger asChild disabled={disabled}>
                <Button
                    id={id}
                    type="button"
                    variant="outline"
                    disabled={disabled}
                    className={cn(
                        "h-9 w-full justify-start px-3 text-sm font-normal",
                        !selected && "text-muted-foreground",
                        className,
                    )}
                >
                    <CalendarIcon className="mr-2 h-4 w-4 shrink-0 opacity-70" />
                    <span className="truncate">
                        {selected ? displayFmt.format(selected) : placeholderText}
                    </span>
                </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
                align="start"
                className="w-[280px] p-3"
                onCloseAutoFocus={(e) => e.preventDefault()}
            >
                <div className="mb-2 flex items-center justify-between gap-1">
                    <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onPointerDown={(e) => e.preventDefault()}
                        onClick={() => setCursor(new Date(year, month - 1, 1))}
                    >
                        <ChevronLeft className="h-4 w-4" />
                    </Button>
                    <div className="text-sm font-medium">
                        {monthTitleFmt.format(new Date(year, month, 1))}
                    </div>
                    <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onPointerDown={(e) => e.preventDefault()}
                        onClick={() => setCursor(new Date(year, month + 1, 1))}
                    >
                        <ChevronRight className="h-4 w-4" />
                    </Button>
                </div>
                <div className="grid grid-cols-7 gap-1 text-center text-[11px] text-muted-foreground">
                    {weekdays.map((w, i) => (
                        <div key={`${w}-${i}`} className="h-6 leading-6">
                            {w}
                        </div>
                    ))}
                </div>
                <div className="mt-1 grid grid-cols-7 gap-1">
                    {cells.map((c, i) =>
                        c.day == null ? (
                            <div key={`empty-${i}`} />
                        ) : (
                            <Button
                                key={c.iso}
                                type="button"
                                variant={c.iso === value ? "default" : "ghost"}
                                size="sm"
                                disabled={c.disabled}
                                className="h-8 w-8 p-0 text-xs"
                                onPointerDown={(e) => e.preventDefault()}
                                onClick={() => {
                                    if (!c.iso || c.disabled) return;
                                    onChange(c.iso);
                                    setOpen(false);
                                }}
                            >
                                {c.day}
                            </Button>
                        ),
                    )}
                </div>
                <div className="mt-2 flex justify-end">
                    <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        onPointerDown={(e) => e.preventDefault()}
                        onClick={() => {
                            onChange("");
                            setOpen(false);
                        }}
                    >
                        {clearLabel}
                    </Button>
                </div>
            </DropdownMenuContent>
        </DropdownMenu>
    );
}

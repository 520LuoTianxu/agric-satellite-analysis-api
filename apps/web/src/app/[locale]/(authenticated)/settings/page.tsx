"use client";

import React from "react";
import { useTranslations } from "next-intl";

export default function SettingsPage() {
    const t = useTranslations("settingsPage");
    return (
        <div className="p-6 lg:p-8 max-w-3xl mx-auto space-y-4">
            <h1 className="text-2xl font-bold tracking-tight">{t("title")}</h1>
            <p className="text-sm text-muted-foreground">
                Organization and member management has been removed. Theme and
                language controls are in the sidebar. Independent authentication
                will be added later.
            </p>
        </div>
    );
}

"use client";

import { useEffect } from "react";
import { useRouter } from "@/i18n/navigation";

/** Settings nav removed; redirect to dashboard. */
export default function SettingsPage() {
    const router = useRouter();
    useEffect(() => {
        router.replace("/dashboard");
    }, [router]);
    return null;
}

import { defineRouting } from "next-intl/routing";

export const routing = defineRouting({
    locales: ["zh", "en", "es"],
    defaultLocale: "zh",
    localePrefix: "as-needed", // zh at /, en at /en, es at /es
    localeDetection: false,
});

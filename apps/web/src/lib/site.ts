/**
 * Canonical identity of this deployment.
 *
 * Metadata, canonicals, hreflang, sitemap, robots, structured data and
 * llms.txt all read from here, so a self-hosted instance describes
 * itself correctly by setting NEXT_PUBLIC_SITE_URL and nothing else.
 *
 * The fallback is localhost on purpose. Defaulting to a public domain
 * would make every unconfigured deployment publish canonicals pointing
 * at someone else's site. Wrong-but-local is a safer failure than
 * wrong-and-someone-else.
 */

import { routing } from "@/i18n/routing";

/** No trailing slash: everything below concatenates paths onto this. */
export const SITE_URL = (process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3000").replace(
    /\/+$/,
    "",
);

export const SITE_NAME = "agric-satellite-analysis";
export const REPO_URL = "https://github.com/520LuoTianxu/agric-satellite-analysis";
export const UPSTREAM_REPO_URL = "https://github.com/superzero11/OpenFarm";
export const DISCORD_URL = "https://discord.gg/KM9qxpEmsU";

/**
 * The entity graph an answer engine needs to tell this product apart
 * from unrelated projects. Keep the category phrase identical
 * everywhere it appears.
 */
export const SAME_AS = [REPO_URL, UPSTREAM_REPO_URL];

/** Locale to Open Graph locale. Keys must match i18n/routing.ts. */
export const OG_LOCALE: Record<string, string> = {
    zh: "zh_CN",
    en: "en_US",
    es: "es_ES",
};

/**
 * Absolute URL for a locale-prefixed path, honouring localePrefix
 * "as-needed". No trailing slash on the root, so the canonical tag and
 * the sitemap entry are byte-identical rather than two spellings of the
 * same page.
 */
export function localeUrl(locale: string, path = "/"): string {
    const clean = path === "/" ? "" : path.replace(/\/+$/, "");
    return locale === routing.defaultLocale
        ? `${SITE_URL}${clean}`
        : `${SITE_URL}/${locale}${clean}`;
}

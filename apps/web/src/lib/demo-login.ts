/** NextAuth Credentials provider id for the optional demo sign-in. */
export const DEMO_PROVIDER_ID = "demo";

function flag(value: string | undefined): boolean {
    return value === "true" || value === "1" || value === "yes";
}

/** Server-side gate: register and accept the demo Credentials provider. */
export function isDemoLoginServerEnabled(): boolean {
    return flag(process.env.ENABLE_DEMO_LOGIN);
}

/** Client-side gate: show the demo button (inlined at build time). */
export function isDemoLoginUiEnabled(): boolean {
    return flag(process.env.NEXT_PUBLIC_ENABLE_DEMO_LOGIN);
}

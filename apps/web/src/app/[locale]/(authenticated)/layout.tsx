import type { Metadata } from "next";
import { AppShell } from "./app-shell";

/* App shell is public after OpenFarm login/orgs removal.
   Independent auth will be added later. robots stay noindex for app paths. */
export const metadata: Metadata = {
    robots: { index: false, follow: false, nocache: true },
};

export default async function AuthenticatedLayout({
    children,
}: {
    children: React.ReactNode;
    params: { locale: string };
}) {
    // No session gate — OpenFarm NextAuth login removed.
    return <AppShell>{children}</AppShell>;
}

/**
 * Direct Postgres helpers for NextAuth user upsert — removed with auth/orgs.
 * Kept as a stub so accidental imports fail loudly at call sites.
 */

export async function upsertUser(
    _email: string,
    _name: string,
    _image?: string | null,
): Promise<never> {
    throw new Error("upsertUser removed — OpenFarm auth/orgs dropped (AUTH_REMOVAL.md)");
}

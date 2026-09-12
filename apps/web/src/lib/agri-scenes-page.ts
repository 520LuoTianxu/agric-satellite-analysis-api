/** Newest-page helpers for agri land scenes timeseries. */

/** OFFSET for ascending query that returns the newest `limit` rows. */
export function newestPageOffset(total: number, limit: number): number {
    if (!(limit > 0) || !(total > 0)) return 0;
    return Math.max(0, Math.floor(total) - Math.floor(limit));
}

/** Reverse a desc-ordered scenes page into ascending chronological order. */
export function reverseDescScenesPage<T>(items: readonly T[]): T[] {
    return items.length ? [...items].reverse() : [];
}

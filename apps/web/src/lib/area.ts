/** 1 hectare = 15 亩 (Chinese mu). */
export const HA_TO_MU = 15;

export function haToMu(ha: number): number {
    return ha * HA_TO_MU;
}

/** Format field area for display. Input is hectares; output uses 亩. */
export function formatAreaMu(
    ha: number | null | undefined,
    digits = 2
): string {
    if (ha == null || Number.isNaN(ha)) return "-";
    return `${haToMu(ha).toFixed(digits)} 亩`;
}

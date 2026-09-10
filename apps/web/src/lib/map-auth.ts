/**
 * MapLibre transformRequest helper.
 * Auth removed — tile requests no longer inject JWT.
 */

import type { RequestParameters } from "maplibre-gl";

/** MapLibre transformRequest callback (pass-through after auth removal). */
export function createTransformRequest() {
    return (url: string, resourceType?: string): RequestParameters => {
        void resourceType;
        return { url };
    };
}

/** No-op token refresh (auth removed). */
export async function refreshMapToken(): Promise<void> {
    return;
}

# OpenFarm auth / orgs removal (0017 + 0018)

## What this change does

- **API**: `AUTH_DISABLED=True` in `services/api/app/middleware/auth.py`. JWT and `X-Org-Id` are not required. `require_roles` always passes. Org scoping helpers (`org_scope` / `org_matches`) are no-ops.
- **Migration 0017**: Dropped `*_org_id_fkey` FKs to `orgs.id`, made domain `org_id` nullable, dropped selected `users.id` FKs on domain writers and made them nullable.
- **Migration 0018 (DESTRUCTIVE)**:
  - **Dropped columns**: `org_id` from `farms`, `fields`, `raster_layers`, `field_stats`, `alerts`, `scouting_observations`, `jobs`, `audit_events`, `share_links`, `weather_daily`, `soil_profiles` (plus `idx_weather_org_id` / `idx_soil_profiles_org_id`).
  - **Dropped user-attribution columns**: `fields.created_by`, `scouting_observations.created_by`, `jobs.created_by`, `audit_events.user_id`, `share_links.created_by`, `share_links.revoked_by`.
  - **Dropped tables** (FK order): `invites`, `org_members`, `orgs`, `users`.
- **mq_result_writer**: Weather/soil upserts no longer write `org_id`.
- **Ingest**: ORM inserts omit `org_id` / `created_by`. Object storage path segment that formerly used org UUID is the literal `default` (e.g. `cogs/default/{field_id}/...`). Existing objects under historical org UUID prefixes are not rewritten.
- **API routers**: `/v1/orgs*` and `/v1/users*` return **410 Gone**. Domain creates no longer set `org_id` / `created_by`.
- **Frontend**: Login / NextAuth / org switcher / `OrgProvider` / `openfarm_org_id` localStorage / demo-login / `/api/auth/*` removed. App shell loads farm/field UI without auth chrome.

## Destructive note

Migration **0018 permanently deletes** auth tables and org/user columns. Downgrade only recreates empty table/column shells — **data is not restored**. Backup before applying on any shared or production database.

## MQ / PR #17

Download/process queue split and result writer paths are preserved. Do not start competing `mq_consumer` / `mq_result_writer` workers against the shared CloudAMQP during verification.

## Residual risks

- Historical COG keys under `cogs/{old-org-uuid}/...` are not migrated; bridge/list code now prefers `cogs/default/...`.
- Response schemas may still expose optional `org_id` / `created_by` fields as `null` for API compatibility.
- NextAuth npm dependency may remain in `package.json` until a follow-up dependency cleanup.

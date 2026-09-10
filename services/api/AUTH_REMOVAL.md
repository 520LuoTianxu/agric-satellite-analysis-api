# OpenFarm auth / orgs removal (0017)

## What this change does

- **API**: `AUTH_DISABLED=True` in `services/api/app/middleware/auth.py`. JWT and `X-Org-Id` are not required. `require_roles` always passes. Org scoping helpers (`org_scope` / `org_matches`) are no-ops.
- **Migration 0017**: Drops all `*_org_id_fkey` FKs to `orgs.id`, makes domain `org_id` columns **nullable**, drops selected `users.id` FKs on domain writers (`created_by` / `audit_events.user_id` / `share_links.*`) and makes them nullable.
- **mq_result_writer**: Weather upsert no longer requires `org_id`; inserts `NULL` when missing (fixes cross-host `ForeignKeyViolation` when Demo org UUID is absent).
- **Frontend**: Authenticated layout no longer redirects to login. `apiFetch` works without a NextAuth session / JWT.

## Leftover (follow-up)

- Tables still present: `users`, `orgs`, `org_members`, `invites`.
- Domain columns still present (nullable, no FK): `org_id` on `farms`, `fields`, `raster_layers`, `field_stats`, `alerts`, `scouting_observations`, `jobs`, `audit_events`, `share_links`, `weather_daily`, `soil_profiles`.
- NextAuth providers / `/api/auth/*` / sidebar org switcher / sign-out UI remain but are unused for API access.
- Dropping `org_id` columns and auth tables is deferred to a later PR.

## Destructive note

Migration **0017 is schema-loosening** (drops FKs, allows NULLs). Downgrade may fail if NULL `org_id` / `created_by` rows exist. Backup before applying on production.

## MQ / PR #17

Download/process queue split and result writer paths are preserved. Weather/soil writes no longer depend on a shared Demo org row.

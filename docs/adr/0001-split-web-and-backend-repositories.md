# ADR 0001: Split the web and backend repositories

- Status: Accepted
- Date: 2026-09-15

## Context

The previous checkout kept the Next.js application under `apps/web` in the
same Git repository as the Python services. That made frontend-only changes
rebuild and review backend files, and deployment could accidentally use a
local frontend tree instead of the published web repository. The workers also
imported a shared Python package whose name still carried the old
`openfarm_common` namespace.

## Decision

Use two sibling repositories under one workspace directory:

```text
agric-satellite-analysis-workspace/
├── agric-satellite-analysis/
└── agric-satellite-analysis-web/
```

The backend repository owns the API, workers, migrations, Docker Compose and
deployment scripts. The frontend repository owns the complete Next.js project,
its Dockerfile, frontend CI, i18n checks and UI design guidance.

The backend Compose file builds the `web` service from the sibling frontend
repository while keeping the Compose service name and internal URL
`http://api:8000` unchanged. This preserves the existing Caddy and container
network contract without copying frontend source into the backend image.

The shared Python package is renamed in both filesystem and import namespace:

```text
packages/agric_satellite_analysis_common/
└── agric_satellite_analysis_common/
```

All services and tests import this new namespace directly. No compatibility
alias for `openfarm_common` is retained.

## Consequences

Positive:

- Frontend and backend can be versioned, reviewed and built independently.
- The frontend Docker build context contains only frontend files.
- Backend workers have one unambiguous shared package namespace.
- The full stack still starts from the backend directory when both sibling
  repositories are present.

Trade-offs:

- A full Compose deployment needs both repositories at the documented sibling
  paths.
- Backend and frontend pull requests are separate and must be coordinated when
  an API contract changes.
- Local tooling and deployment scripts must explicitly clone or update both
  repositories.

## Alternatives considered

- Keep the monorepo: rejected because it preserves the build and ownership
  coupling that motivated the split.
- Use a Git submodule: rejected because it adds detached-submodule workflow
  friction and still hides the frontend repository boundary.

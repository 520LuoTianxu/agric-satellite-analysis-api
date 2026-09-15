## Summary
- What does this PR change and why?

## Testing
- [ ] `python3 scripts/check-env-parity.py`
- [ ] `ruff check .` and `ruff format --check .` in `services/api`
- [ ] API unit tests, or describe API-only testing
- [ ] `docker compose up --build` (full stack), when the sibling frontend repo is available
- [ ] Other: 

## Screenshots (if UI change)

## Checklist
- [ ] I added/updated docs where needed (README/CONTRIBUTING)
- [ ] I updated the frontend repository separately if an API contract changed
- [ ] I added/updated Alembic migration if the DB schema changed
- [ ] I handled `X-Org-Id` on org-scoped API calls
- [ ] No secrets or credentials committed

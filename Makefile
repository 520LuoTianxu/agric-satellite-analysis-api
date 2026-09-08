# OpenFarm / agric-satellite-analysis helpers

.PHONY: agri-schema agri-seed agri-seed-reset

agri-schema:
	./scripts/agri_seed/import_agri_seed.sh --schema-only

agri-seed:
	./scripts/agri_seed/import_agri_seed.sh $${AGRI_SQL:-data/agri_export.sql}

agri-seed-reset:
	./scripts/agri_seed/import_agri_seed.sh --reset $${AGRI_SQL:-data/agri_export.sql}

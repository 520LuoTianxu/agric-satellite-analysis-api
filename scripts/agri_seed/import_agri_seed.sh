#!/usr/bin/env bash
# Import Aliyun agri schema/data into OpenFarm Postgres.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
SEED_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SCHEMA_ONLY_SQL="$SEED_DIR/001_agri_schema.sql"
DEFAULT_SQL="${AGRI_SQL:-$ROOT/data/agri_export.sql}"

RESET=0
SCHEMA_ONLY=0
DATA_DIR=""
SQL_PATH=""

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options] [agri_export.sql]

Options:
  --reset          DROP SCHEMA agri CASCADE before import
  --schema-only    Apply scripts/agri_seed/001_agri_schema.sql only (no COPY data)
  --data-dir DIR   Join agri_export.sql.part-*.sql in DIR, then import
  -h, --help       Show this help

Env:
  DATABASE_URL_SYNC / DATABASE_URL   Target DB (asyncpg URL is normalized)
  POSTGRES_USER POSTGRES_DB          Docker compose defaults (openfarm)
  COMPOSE_PROJECT / DOCKER_DB        Override docker db container name
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset) RESET=1; shift ;;
    --schema-only) SCHEMA_ONLY=1; shift ;;
    --data-dir) DATA_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      SQL_PATH="$1"
      shift
      ;;
  esac
done

join_parts() {
  local dir="$1"
  local out="$2"
  shopt -s nullglob
  local parts=("$dir"/agri_export.sql.part-*.sql)
  shopt -u nullglob
  if [[ ${#parts[@]} -eq 0 ]]; then
    echo "No agri_export.sql.part-*.sql in $dir" >&2
    exit 1
  fi
  # shellcheck disable=SC2207
  IFS=$'\n' parts=($(printf '%s\n' "${parts[@]}" | sort))
  cat "${parts[@]}" > "$out"
  echo "Joined ${#parts[@]} parts -> $out ($(wc -c < "$out") bytes)"
}

normalize_db_url() {
  local url="${1:-}"
  url="${url/postgresql+asyncpg:\/\//postgresql:\/\/}"
  url="${url/postgres+asyncpg:\/\//postgresql:\/\/}"
  printf '%s' "$url"
}

# Resolve SQL file
if [[ "$SCHEMA_ONLY" -eq 1 ]]; then
  SQL_PATH="$SCHEMA_ONLY_SQL"
elif [[ -n "$DATA_DIR" ]]; then
  mkdir -p "$ROOT/data"
  JOINED="$ROOT/data/agri_export.sql"
  join_parts "$DATA_DIR" "$JOINED"
  SQL_PATH="$JOINED"
elif [[ -z "$SQL_PATH" ]]; then
  SQL_PATH="$DEFAULT_SQL"
fi

if [[ ! -f "$SQL_PATH" ]]; then
  echo "SQL file not found: $SQL_PATH" >&2
  echo "Place joined dump at data/agri_export.sql or pass --data-dir / --schema-only." >&2
  exit 1
fi

# Prefer a cleaned sibling if present (no \\restrict)
CLEAN_CANDIDATE="$(dirname "$SQL_PATH")/agri_export_clean.sql"
if [[ "$SCHEMA_ONLY" -eq 0 && -f "$CLEAN_CANDIDATE" && "$(basename "$SQL_PATH")" == "agri_export.sql" ]]; then
  echo "Using cleaned dump: $CLEAN_CANDIDATE"
  SQL_PATH="$CLEAN_CANDIDATE"
fi

echo "Import source: $SQL_PATH ($(wc -c < "$SQL_PATH") bytes)"

DB_URL="$(normalize_db_url "${DATABASE_URL_SYNC:-${DATABASE_URL:-}}")"
PGUSER="${POSTGRES_USER:-openfarm}"
PGDB="${POSTGRES_DB:-openfarm}"
DOCKER_DB="${DOCKER_DB:-}"
if [[ -z "$DOCKER_DB" ]]; then
  # Heuristic: compose project folder name
  DOCKER_DB="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '_db_1$|-db-1$' | head -n1 || true)"
fi

run_psql_file() {
  local file="$1"
  # Strip pg_dump \\restrict / \\unrestrict which older clients reject
  local tmp
  tmp="$(mktemp)"
  # shellcheck disable=SC2016
  sed -e '/^\\restrict /d' -e '/^\\unrestrict /d' "$file" > "$tmp"

  if [[ -n "$DB_URL" ]] && command -v psql >/dev/null 2>&1; then
    echo "psql via DATABASE_URL…"
    psql -v ON_ERROR_STOP=1 "$DB_URL" -f "$tmp"
  elif [[ -n "$DOCKER_DB" ]]; then
    echo "psql via docker exec $DOCKER_DB (user=$PGUSER db=$PGDB)…"
    # Copy into container to avoid huge stdin issues
    local remote="/tmp/agri_import_$$.sql"
    docker cp "$tmp" "$DOCKER_DB:$remote"
    docker exec -i "$DOCKER_DB" psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB" -f "$remote"
    docker exec "$DOCKER_DB" rm -f "$remote" || true
  elif command -v psql >/dev/null 2>&1; then
    echo "psql local (user=$PGUSER db=$PGDB host=${PGHOST:-localhost})…"
    PGPASSWORD="${POSTGRES_PASSWORD:-${PGPASSWORD:-}}" \
      psql -v ON_ERROR_STOP=1 -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" \
      -U "$PGUSER" -d "$PGDB" -f "$tmp"
  else
    echo "Neither usable DATABASE_URL/psql nor docker db container found." >&2
    rm -f "$tmp"
    exit 1
  fi
  rm -f "$tmp"
}

run_sql() {
  local sql="$1"
  if [[ -n "$DB_URL" ]] && command -v psql >/dev/null 2>&1; then
    psql -v ON_ERROR_STOP=1 "$DB_URL" -c "$sql"
  elif [[ -n "$DOCKER_DB" ]]; then
    docker exec -i "$DOCKER_DB" psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB" -c "$sql"
  else
    PGPASSWORD="${POSTGRES_PASSWORD:-${PGPASSWORD:-}}" \
      psql -v ON_ERROR_STOP=1 -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" \
      -U "$PGUSER" -d "$PGDB" -c "$sql"
  fi
}

if [[ "$RESET" -eq 1 ]]; then
  echo "DROP SCHEMA agri CASCADE…"
  run_sql "DROP SCHEMA IF EXISTS agri CASCADE;"
fi

echo "Applying…"
run_psql_file "$SQL_PATH"

echo "Row counts:"
run_sql "SELECT relname AS table, n_live_tup AS approx_rows
         FROM pg_stat_user_tables WHERE schemaname='agri' ORDER BY 1;"

echo "Done. Tip: GET /v1/agri/stats or /v1/agri/admin/import-status"

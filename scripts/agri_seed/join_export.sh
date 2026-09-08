#!/usr/bin/env bash
set -euo pipefail
base="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
parts=("$base"/agri_export.sql.part-*.sql)
if [ ! -e "${parts[0]}" ]; then
  echo "没有找到 agri_export.sql.part-*.sql" >&2
  exit 1
fi
cat "${parts[@]}" > "$base/agri_export.sql"
echo "已合并 ${#parts[@]} 个分片到 $base/agri_export.sql"

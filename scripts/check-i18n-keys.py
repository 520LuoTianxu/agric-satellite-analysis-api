#!/usr/bin/env python3
"""Ensure locale message files share the same JSON key tree as en.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESSAGES = ROOT / "apps" / "web" / "messages"


def leaf_keys(obj: object, prefix: str = "") -> set[str]:
    if isinstance(obj, dict):
        keys: set[str] = set()
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys |= leaf_keys(value, path)
        return keys
    return {prefix}


def main() -> int:
    en_path = MESSAGES / "en.json"
    en = json.loads(en_path.read_text(encoding="utf-8"))
    en_keys = leaf_keys(en)
    failures: list[str] = []

    for locale in ("es", "zh"):
        path = MESSAGES / f"{locale}.json"
        if not path.exists():
            failures.append(f"missing {path.relative_to(ROOT)}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = leaf_keys(data)
        missing = sorted(en_keys - keys)
        extra = sorted(keys - en_keys)
        if missing:
            failures.append(f"{locale}.json missing keys: " + ", ".join(missing[:20]))
            if len(missing) > 20:
                failures.append(f"  … and {len(missing) - 20} more")
        if extra:
            failures.append(f"{locale}.json extra keys: " + ", ".join(extra[:20]))
            if len(extra) > 20:
                failures.append(f"  … and {len(extra) - 20} more")

    if failures:
        print("FAIL: i18n message keys do not match en.json")
        for item in failures:
            print(f"  - {item}")
        return 1

    print(f"  i18n keys OK ({len(en_keys)} leaves across en/es/zh)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

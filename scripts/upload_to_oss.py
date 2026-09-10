#!/usr/bin/env python3
"""Upload local files/directories to Aliyun OSS (shared helper).

Reads credentials from the environment (or a KEY=VALUE env file):

  OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET
  OSS_ENDPOINT   (default https://oss-cn-beijing.aliyuncs.com)
  OSS_BUCKET     (default agric-dev)
  OSS_ENV        optional path to env file (also tries
                 /home/box/.config/aliyun/oss.env and repo .env)

Examples:

  # Upload glossary PNGs as public web assets
  python3 scripts/upload_to_oss.py \\
    --src apps/web/public/glossary \\
    --prefix web/glossary/ \\
    --public-read \\
    --write-manifest apps/web/public/glossary/oss-manifest.json

  # Upload a single file
  python3 scripts/upload_to_oss.py --src ./foo.png --prefix web/misc/ --public-read

  # Dry run
  python3 scripts/upload_to_oss.py --src apps/web/public/glossary --prefix web/glossary/ --dry-run
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

try:
    import oss2
except ImportError as e:  # pragma: no cover
    print("Missing dependency: pip install oss2", file=sys.stderr)
    raise SystemExit(1) from e

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_CANDIDATES = [
    Path(os.environ.get("OSS_ENV", "")),
    Path.home() / ".config/aliyun/oss.env",
    REPO_ROOT / ".env",
]


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def load_oss_config() -> dict[str, str]:
    for cand in DEFAULT_ENV_CANDIDATES:
        if cand and str(cand) != ".":
            _load_env_file(cand)
    cfg = {
        "endpoint": os.environ.get("OSS_ENDPOINT", "https://oss-cn-beijing.aliyuncs.com"),
        "bucket": os.environ.get("OSS_BUCKET", "agric-dev"),
        "ak": os.environ.get("OSS_ACCESS_KEY_ID", ""),
        "sk": os.environ.get("OSS_ACCESS_KEY_SECRET", ""),
    }
    if not cfg["ak"] or not cfg["sk"]:
        raise SystemExit(
            "OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET missing. "
            "Export them or put them in OSS_ENV / ~/.config/aliyun/oss.env / .env"
        )
    return cfg


def public_url(endpoint: str, bucket: str, key: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.netloc or parsed.path
    scheme = parsed.scheme or "https"
    return f"{scheme}://{bucket}.{host}/{key.lstrip('/')}"


def guess_content_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def iter_files(src: Path) -> list[Path]:
    if src.is_file():
        return [src]
    if not src.is_dir():
        raise SystemExit(f"Not found: {src}")
    files: list[Path] = []
    for p in sorted(src.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            # skip manifests / text sidecars unless explicitly wanted
            if p.suffix.lower() in {".json", ".md", ".txt"} and p.name.startswith("oss-"):
                continue
            files.append(p)
    return files


def object_key(src_root: Path, file_path: Path, prefix: str) -> str:
    prefix = prefix.lstrip("/")
    if not prefix.endswith("/") and prefix:
        prefix = f"{prefix}/"
    if src_root.is_file():
        return f"{prefix}{src_root.name}"
    rel = file_path.relative_to(src_root).as_posix()
    return f"{prefix}{rel}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload local assets to Aliyun OSS")
    ap.add_argument("--src", required=True, help="Local file or directory")
    ap.add_argument(
        "--prefix",
        default="web/assets/",
        help="OSS key prefix (default: web/assets/)",
    )
    ap.add_argument(
        "--public-read",
        action="store_true",
        help="Set object ACL to public-read (needed for browser <img>)",
    )
    ap.add_argument(
        "--cache-control",
        default="public, max-age=31536000, immutable",
        help="Cache-Control header (empty to omit)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--write-manifest",
        default="",
        help="Write JSON mapping {filename: public_url} to this path",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip upload when object already exists",
    )
    args = ap.parse_args()

    src = Path(args.src).expanduser().resolve()
    files = iter_files(src)
    if not files:
        print(f"No files under {src}", file=sys.stderr)
        return 1

    cfg = load_oss_config()
    auth = oss2.Auth(cfg["ak"], cfg["sk"])
    bucket = oss2.Bucket(auth, cfg["endpoint"], cfg["bucket"])

    mapping: dict[str, str] = {}
    uploaded = 0
    skipped = 0

    print(f"bucket={cfg['bucket']} endpoint={cfg['endpoint']} files={len(files)}")
    for fp in files:
        key = object_key(src if src.is_dir() else src, fp, args.prefix)
        url = public_url(cfg["endpoint"], cfg["bucket"], key)
        name = fp.name
        mapping[name] = url

        if args.skip_existing and not args.dry_run and bucket.object_exists(key):
            print(f"skip  {key}")
            skipped += 1
            continue

        headers: dict[str, str] = {"Content-Type": guess_content_type(fp)}
        if args.cache_control:
            headers["Cache-Control"] = args.cache_control
        if args.public_read:
            headers["x-oss-object-acl"] = "public-read"

        if args.dry_run:
            print(f"dry   {fp} -> {key} ({url})")
            continue

        bucket.put_object_from_file(key, str(fp), headers=headers)
        print(f"put   {key} -> {url}")
        uploaded += 1

    if args.write_manifest:
        out = Path(args.write_manifest).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bucket": cfg["bucket"],
            "endpoint": cfg["endpoint"],
            "prefix": args.prefix,
            "base_url": public_url(cfg["endpoint"], cfg["bucket"], args.prefix.rstrip("/") + "/"),
            "files": mapping,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"manifest -> {out}")

    print(f"done uploaded={uploaded} skipped={skipped} total={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

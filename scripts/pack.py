#!/usr/bin/env python3
"""Pack the skill into a distributable zip.

    python scripts/pack.py [--out PATH] [--version 2506]

The archive holds one top-level folder, `tc-help/`, so a colleague can unzip it
straight into their skills directory. It carries only what a colleague needs to
query: SKILL.md, README.md, `references/`, `scripts/help.py`, the corpus
database, plus MANIFEST.json. Build, test and packing tooling (`scripts/data/`,
`scripts/test/`, this file) stay out of the archive.

Rebuild the database first (`scripts/data/build_db.py`), then pack;
`help.py --selftest` should be green before shipping.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
INCLUDE_DIRS = ("scripts", "references")
INCLUDE_FILES = ("SKILL.md", "README.md")
EXCLUDE_DIRS = (("scripts", "data"), ("scripts", "test"))
EXCLUDE_FILES = ("scripts/pack.py",)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def excluded(path):
    rel = path.relative_to(SKILL_ROOT)
    if rel.as_posix() in EXCLUDE_FILES:
        return True
    parts = rel.parts
    return any(parts[:len(d)] == d for d in EXCLUDE_DIRS)


def collect(version):
    files = [SKILL_ROOT / name for name in INCLUDE_FILES]
    for d in INCLUDE_DIRS:
        base = SKILL_ROOT / d
        if base.is_dir():
            files += [p for p in sorted(base.rglob("*"))
                      if p.is_file() and "__pycache__" not in p.parts
                      and p.suffix != ".pyc" and not excluded(p)]
    db = SKILL_ROOT / "data" / version / f"tc_help_{version}.db"
    if not db.exists():
        sys.exit(f"database missing: {db} (build it first)")
    files.append(db)
    missing = [str(p) for p in files if not p.exists()]
    if missing:
        sys.exit("missing files: " + ", ".join(missing))
    return files, db


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="2506")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    files, db = collect(args.version)
    out = Path(args.out) if args.out else SKILL_ROOT / f"tc-help-{args.version}.zip"
    if out.exists():
        out.unlink()

    meta = {
        "name": "tc-help",
        "version": args.version,
        "packed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "database": {"path": db.relative_to(SKILL_ROOT).as_posix(),
                     "bytes": db.stat().st_size, "sha256": sha256(db)},
        "files": [{"path": p.relative_to(SKILL_ROOT).as_posix(),
                   "bytes": p.stat().st_size} for p in files],
    }

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            z.write(p, f"tc-help/{p.relative_to(SKILL_ROOT).as_posix()}")
        z.writestr("tc-help/MANIFEST.json",
                   json.dumps(meta, ensure_ascii=False, indent=1))

    size = out.stat().st_size
    print(f"packed {len(files)} files -> {out}")
    print(f"  archive {size/1048576:.1f} MB   database {db.stat().st_size/1048576:.1f} MB")
    print(f"  zip sha256 {sha256(out)[:16]}...   db sha256 {meta['database']['sha256'][:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Point every item at its generated photo from data/item_photos/.

gen_item_photos.py only writes image files and a manifest; nothing it does is
visible on the site. This script is the second half: it compresses each generated
photo into data/uploads/ and rewrites that one item's photo_path.

It touches exactly one column of one table. Seeding is never re-run, so the twelve
weeks of simulated demand, the sold records and the tester accounts are all left
alone. The old placeholder files stay on disk, and the pre-change database is
zipped into backups/ first, so the whole thing is reversible.

    python scripts/apply_item_photos.py --dry-run     # list what would change
    python scripts/apply_item_photos.py               # apply
    python scripts/apply_item_photos.py --rollback backups/locswap_before_photos_*.zip

By default an item is only updated while it still shows a 640x480 placeholder, so
re-running is safe and idempotent. --force re-applies to items already switched
over, which is what you want after regenerating a photo you were unhappy with.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import _bootstrap  # noqa: F401  # isort:skip

from PIL import Image

from app.config import settings
from app.services.images import compress_and_save

PHOTO_DIR = settings.data_path / "item_photos"
MANIFEST = PHOTO_DIR / "manifest.json"
DB_PATH = settings.data_path / "locswap.db"
BACKUP_DIR = settings.data_path.parent / "backups"
PLACEHOLDER_SIZE = (640, 480)


def is_placeholder(relative: str | None) -> bool:
    if not relative:
        return False
    path = settings.data_path / relative
    try:
        with Image.open(path) as im:
            return im.size == PLACEHOLDER_SIZE
    except OSError:
        return False


def backup_database() -> Path:
    """Zip the database (plus any -wal/-shm) before the first write."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"locswap_before_photos_{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(DB_PATH) + suffix)
            if source.exists():
                zf.write(source, source.name)
    return target


def rollback(archive: Path) -> int:
    if not archive.exists():
        raise SystemExit(f"Backup not found: {archive}")
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        print(f"Restoring {names} from {archive.name}")
        zf.extractall(DB_PATH.parent)
    print("Database restored. The generated photos in data/item_photos/ are untouched.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply generated photos to items")
    ap.add_argument("--dry-run", action="store_true", help="list changes, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="also re-apply to items that already left the placeholder")
    ap.add_argument("--only-ids", default="", help="comma-separated item ids")
    ap.add_argument("--rollback", metavar="ZIP", help="restore the database from a backup zip")
    args = ap.parse_args()

    if args.rollback:
        return rollback(Path(args.rollback))

    if not MANIFEST.exists():
        raise SystemExit(f"No manifest at {MANIFEST} — run gen_item_photos.py first.")
    manifest = json.loads(MANIFEST.read_text("utf-8"))

    wanted = {int(x) for x in args.only_ids.split(",") if x.strip()} if args.only_ids else None

    conn = sqlite3.connect(DB_PATH)
    current = {
        row[0]: row[1]
        for row in conn.execute("select id, photo_path from items")
    }

    todo, skipped, missing = [], [], []
    for key, rec in manifest.items():
        item_id = int(key)
        if wanted is not None and item_id not in wanted:
            continue
        if item_id not in current:
            missing.append((item_id, "no such item in the database"))
            continue
        source = PHOTO_DIR / rec["file"]
        if not source.exists():
            missing.append((item_id, f"generated file gone: {rec['file']}"))
            continue
        if not args.force and not is_placeholder(current[item_id]):
            skipped.append(item_id)
            continue
        todo.append((item_id, source, rec))

    print(
        f"manifest {len(manifest)} photos | to apply {len(todo)}"
        f" | already applied {len(skipped)} | problems {len(missing)}"
    )
    for item_id, reason in missing[:10]:
        print(f"  !! #{item_id}: {reason}")
    if not todo:
        print("Nothing to do.")
        return 0

    for item_id, _src, rec in todo[:15]:
        # Round-one manifest entries predate the "descriptor" field.
        label = rec.get("descriptor") or rec.get("name") or rec.get("base", "?")
        print(f"  #{item_id:4d} {current[item_id]}  ->  {rec['file']}  ({label})")
    if len(todo) > 15:
        print(f"  ... and {len(todo) - 15} more")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    backup = backup_database()
    print(f"\nDatabase backed up to {backup}")

    applied = failed = 0
    for item_id, source, rec in todo:
        try:
            relative = compress_and_save(source.read_bytes(), rec["file"])
            conn.execute(
                "update items set photo_path = ? where id = ?", (relative, item_id)
            )
            conn.commit()
            applied += 1
            if applied % 50 == 0:
                print(f"  ... {applied}/{len(todo)}")
        except Exception as exc:  # noqa: BLE001 — one bad file must not sink the batch
            failed += 1
            print(f"  !! #{item_id} failed: {str(exc)[:160]}")

    still_placeholder = sum(
        1 for (_i, p) in conn.execute("select id, photo_path from items") if is_placeholder(p)
    )
    conn.close()

    print(
        f"\nApplied {applied}, failed {failed}."
        f"\nItems still on a placeholder: {still_placeholder}."
        f"\nOld placeholder files were left in data/uploads/ — nothing was deleted."
        f"\nRoll back with:  python scripts/apply_item_photos.py --rollback {backup}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

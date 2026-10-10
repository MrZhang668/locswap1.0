"""Batch-generate "used-item snapshot" photos per product prototype into the data/item_images/ library.

The library is separate from data/uploads/: seed scripts delete uploads/*.jpg on every
wipe, while the library only needs to be generated once and is then reused by every
seed run (seed_mock.py matches library photos by "base|Color"). manifest.json records
the (base, color) -> file mapping and is written after every image, so the run can be
interrupted and resumed at any time; existing (base, color) pairs are skipped.

    python scripts/gen_item_images.py --dry-run              # only print the generation plan
    python scripts/gen_item_images.py                        # default 30 images (test the waters)
    python scripts/gen_item_images.py --count 300            # fill in the whole library
    python scripts/gen_item_images.py --colors-per 3         # at most 3 color variants per prototype

Generation order is ranked by "expected item coverage": prototypes with a higher
category weight / prototype count ratio go first, and the first pass generates one
color per prototype before a second pass adds a second color — so the same 30-image
budget covers as many prototypes as possible.

Billing goes through the OpenRouter key in .env (LLM_API_KEY / LLM_BASE_URL); the
default model is google/gemini-2.5-flash-image, roughly $0.04 per image.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import time

import _bootstrap  # noqa: F401  # isort:skip

import httpx
from PIL import Image

from app.config import settings
from mock_catalog import CATALOG
from seed_mock import CATEGORY_WEIGHTS

LIBRARY_DIR = settings.data_path / "item_images"
MANIFEST = LIBRARY_DIR / "manifest.json"

PROMPT_TEMPLATE = (
    "A photo of an item listed on a second-hand marketplace app: a {color} {base} ({variant}). "
    "The item sits on a desk, bed or floor in a student apartment and fills most of the frame, "
    "framed like a casual phone snapshot with natural indoor light; the background is an ordinary "
    "dorm/apartment room, slightly blurred. "
    "The item is in good condition but looks genuinely lived-with. "
    "No text, watermarks, brand logos or people anywhere in the image. Photorealistic, not a render."
)


def plan_targets(colors_per: int) -> list[dict]:
    """The (prototype, color) generation plan, ordered by expected coverage."""
    scored = []
    for cat, protos in CATALOG.items():
        weight = CATEGORY_WEIGHTS.get(cat, 1)
        score = weight / max(1, len(protos))
        for base, _brands, variants, _price, colors, _specs, _uses in protos:
            scored.append((score, cat, base, variants[0], colors))
    scored.sort(key=lambda t: -t[0])

    targets: list[dict] = []
    for color_round in range(colors_per):
        for score, cat, base, variant, colors in scored:
            if color_round < len(colors):
                targets.append({
                    "category": cat,
                    "base": base,
                    "variant": variant,
                    "color": colors[color_round],
                    "score": score,
                })
    return targets


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text("utf-8"))
    return {}


def save_manifest(manifest: dict) -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")


def generate_one(client: httpx.Client, model: str, target: dict) -> bytes:
    prompt = PROMPT_TEMPLATE.format(**target)
    resp = client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    images = (data.get("choices") or [{}])[0].get("message", {}).get("images") or []
    if not images:
        raise RuntimeError(f"No image in response: {json.dumps(data)[:400]}")
    url = images[0]["image_url"]["url"]  # data:image/png;base64,xxxx
    b64 = url.split(",", 1)[1]
    return base64.b64decode(b64)


def to_jpeg(raw: bytes) -> bytes:
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    max_edge = settings.image_max_edge
    if max(image.size) > max_edge:
        ratio = max_edge / max(image.size)
        image = image.resize(
            (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=settings.image_jpeg_quality, optimize=True)
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-generate the product prototype photo library")
    parser.add_argument("--count", type=int, default=30, help="max new images this run (default 30)")
    parser.add_argument("--colors-per", type=int, default=2, help="max color variants per prototype (default 2)")
    parser.add_argument("--model", default="google/gemini-2.5-flash-image",
                        help="OpenRouter model slug that supports image output")
    parser.add_argument("--dry-run", action="store_true", help="only print the plan, no API calls")
    args = parser.parse_args()

    targets = plan_targets(args.colors_per)
    manifest = load_manifest()

    todo = [t for t in targets if f"{t['base']}|{t['color']}" not in manifest][: args.count]
    if not todo:
        print(f"Library already has {len(manifest)} images; nothing new in this plan.")
        return 0

    print(f"Library has {len(manifest)} images; planning {len(todo)} new ones"
          f" (model {args.model}, roughly ${len(todo) * 0.04:.2f}):")
    for t in todo:
        print(f"  {t['category']} | {t['color']} {t['base']} ({t['variant']})")
    if args.dry_run:
        return 0

    if not settings.llm_api_key or not settings.llm_base_url:
        raise SystemExit("LLM_API_KEY / LLM_BASE_URL missing (.env); cannot call the image model")

    client = httpx.Client(
        base_url=settings.llm_base_url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=180.0,
    )

    ok = failed = 0
    for i, t in enumerate(todo, 1):
        key = f"{t['base']}|{t['color']}"
        label = f"[{i}/{len(todo)}] {t['color']} {t['base']}"
        for attempt in (1, 2):
            try:
                raw = generate_one(client, args.model, t)
                jpeg = to_jpeg(raw)
                name = f"{hashlib.md5(key.encode('utf-8')).hexdigest()[:12]}.jpg"
                LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
                (LIBRARY_DIR / name).write_bytes(jpeg)
                manifest[key] = {
                    "file": name,
                    "category": t["category"],
                    "base": t["base"],
                    "color": t["color"],
                    "model": args.model,
                }
                save_manifest(manifest)
                ok += 1
                print(f"{label} -> {name} ({len(jpeg) // 1024} KB)")
                break
            except Exception as exc:  # noqa: BLE001 — one failed image must not sink the batch
                if attempt == 1:
                    print(f"{label} attempt 1 failed ({exc}), retrying...")
                    time.sleep(3)
                else:
                    failed += 1
                    print(f"{label} failed: {exc}")

    print(f"\nDone: {ok} new, {failed} failed, library now {len(manifest)} images -> {LIBRARY_DIR}")
    if ok:
        print("Re-run python scripts/seed_mock.py so items pick up the library photos.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""Generate one unique "real listing snapshot" photo per item into data/item_photos/.

Unlike gen_item_images.py — which builds a small shared library keyed by
prototype|Color and lets many items reuse the same picture — this script generates
a distinct photo for every item that still shows a placeholder, so no two listings
look alike.

Style is anchored on the real marketplace photos in training_source_image_product/:
a few category-matched reference crops are attached to every request, and several
per-item randomised axes (setting / framing / lighting / camera defect / clutter /
angle) are seeded from the item id, so a re-run reproduces the same look.

The listing title is deliberately NOT sent to the model: round one showed it gets
painted onto the object as literal text ("Weekly 2026 academic planner" printed on
a planner cover). Only a brand-stripped, generic noun phrase is sent, and settings
are filtered by the prototype's physical size so a bicycle never lands on a desk.

Output goes to data/item_photos/ and progress is recorded in manifest.json after
every image, so the run can be interrupted and resumed; items already present are
skipped unless --force is given. Nothing touches the database —
apply_item_photos.py does that separately.

    python scripts/gen_item_photos.py --dry-run           # print the plan only
    python scripts/gen_item_photos.py --count 20          # trial batch
    python scripts/gen_item_photos.py --count 9999        # the whole backlog
    python scripts/gen_item_photos.py --only-ids 12,34 --force   # redo two items

Billing goes through the OpenRouter key in .env (LLM_API_KEY / LLM_BASE_URL);
the default model is google/gemini-2.5-flash-image, about $0.039 per image.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import _bootstrap  # noqa: F401  # isort:skip

import httpx
from PIL import Image

from app.config import settings

OUT_DIR = settings.data_path / "item_photos"
MANIFEST = OUT_DIR / "manifest.json"
BACKUP_DIR = OUT_DIR / "_superseded"
REF_DIR = (
    settings.data_path.parent
    / "training_source_image_product"
    / "商品图训练集_images"
)

# ---------------------------------------------------------------- size classes
# Round one put a mountain bike on a dining table and an e-scooter on a kitchen
# counter. Anything here may only be photographed on the floor, against a wall or
# in a room scene — never on a desk, table or counter.
LARGE = {
    "mountain bike", "road bike", "commuter bike", "folding bike", "e-scooter",
    "desk", "desk chair", "floor lamp", "mini fridge", "futon", "bookshelf",
    "full-length mirror", "bed frame", "dresser", "area rug", "mattress topper",
    "vacuum cleaner", "space heater", "tent", "suitcase", "camping chair",
    "digital piano", "guitar amp", "acoustic guitar", "electric guitar",
    "bass guitar",
}
# Fits on a counter or dining table, but looks wrong crammed onto a study desk.
MEDIUM = {
    "microwave", "humidifier", "comforter set", "throw blanket", "cookware set",
    "dinnerware set", "storage bins", "monitor", "game console", "laptop",
    "skateboard", "sleeping bag", "hiking backpack", "backpack", "winter coat",
    "rain jacket", "denim jacket", "MIDI keyboard", "electronic drum pad",
    "violin", "flute", "ukulele", "air fryer", "coffee maker", "blender",
    "electric pressure cooker", "rice cooker", "electric kettle", "yoga mat",
    "dumbbells", "kettlebell", "tennis racket", "badminton set",
    "desktop whiteboard", "shower caddy", "string lights",
}

# Orthogonal to size: objects whose identity lives in their keys and display. Forbidding
# all text on these erases the very feature that makes them recognisable.
KEYPAD = {
    "graphing calculator", "scientific calculator", "mechanical keyboard",
    "game controller", "MIDI keyboard", "electronic drum pad", "game console",
    "desktop whiteboard",
}
CLOTHING_BASES = {
    "winter coat", "rain jacket", "hoodie", "denim jacket", "jeans", "joggers",
    "dress", "running shoes", "sneakers", "winter boots", "hat", "scarf",
    "button-down shirt",
}

FLOOR_SETTINGS = [
    "standing on the floor against a plain painted apartment wall",
    "on a carpeted bedroom floor, wall and skirting board behind it",
    "in the corner of a small apartment room",
    "on a wooden floor near an open doorway",
    "in a narrow hallway beside the front door",
]
SURFACE_SETTINGS = [
    "on a kitchen counter next to a tiled backsplash",
    "on a laminate dining table",
    "on an unmade bed with rumpled sheets",
    "on a small side table in a shared living room",
]
DESK_SETTINGS = [
    "on a cluttered student desk",
    "on a desk beside a laptop and a mug",
]

FRAMINGS = [
    "tight close-up, the item fills almost the whole frame",
    "medium shot, the item fills about half the frame with the surroundings visible",
    "wider shot, the item sits in the room and takes up roughly a third of the frame",
    "slightly too close, one edge of the item is cropped out of frame",
    "off-centre composition with noticeable empty space on one side",
]
# Round one produced a perfect circular spotlight on a floor and creamy bokeh
# backgrounds — both read as studio work. These are all plain room lighting.
LIGHTINGS = [
    "plain warm ceiling light, slightly orange cast, lighting the whole room evenly",
    "ordinary daylight through a window, the far side of the item a little darker",
    "on-camera phone flash, flat and bright with a hard shadow on the wall behind",
    "dim ordinary room light, slightly underexposed and a bit grainy",
    "cool white fluorescent ceiling light, flat and slightly blue",
    "overcast daylight from a window, soft and even with no strong shadows",
]
DEFECTS = [
    "very slightly out of focus",
    "a little motion blur from an unsteady hand",
    "mildly overexposed, some highlights blown out",
    "the horizon is tilted by a few degrees",
    "visible JPEG compression noise, as if resized down and re-saved",
    "flat dull colours and faint sensor noise, the look of a cheap phone camera",
]
CLUTTERS = [
    "the background is tidy and mostly empty",
    "a few solid everyday objects sit around the edges of the frame",
    "a lived-in background — power cables, a couple of books, a mug, a backpack",
    "a cardboard moving box and a closed storage bin are visible behind it",
    "some furniture and a few boxes are visible further back",
]
ANGLES = [
    "shot straight on at eye level",
    "shot from a three-quarter angle",
    "shot from above, looking down at the item",
    "shot from a low angle, close to the surface it rests on",
]
# A small object on the floor gets photographed by someone standing over it, not by
# someone lying on the carpet next to it — round three produced both.
FLOOR_SMALL_ANGLES = [
    "shot from standing height, the camera pointing straight down at the floor",
    "shot from standing height, looking down at the item at a steep angle",
    "shot from waist height, tilted down towards the floor",
]
WEAR = [
    "It looks lightly used and clean.",
    "It shows honest wear — small scuffs and a bit of dust.",
    "It is visibly second-hand: scratches, a few fingerprints, slightly grubby.",
]

# Real titles and author names are trademarks in their own right; round one
# rendered "HALLIDAY RESNICK WALKER" on a physics textbook. Generalise them.
GENERIC_BASE = {
    "Stewart Calculus textbook": "calculus textbook",
    "Organic Chemistry textbook": "organic chemistry textbook",
    "CLRS algorithms textbook": "algorithms textbook",
    "Campbell Biology textbook": "biology textbook",
    "Intro Psychology textbook": "introductory psychology textbook",
    "MCAT prep set": "set of standardised-test prep books",
    "LSAT prep set": "set of law-school admission test prep books",
    "APA style guide": "academic writing style guide",
    "Spanish textbook": "Spanish language textbook",
}

PROMPT = """\
Create a photograph for a second-hand marketplace listing posted by a university student.

THE OBJECT: {descriptor}. Its finish is {color_phrase}. {wear}

The attached images are REAL photos from listings on this marketplace. Copy their visual
character, not their subject: quick amateur phone snapshots, never product photography.

Shoot it this way:
- Setting: {setting}
- Framing: {framing}
- Lighting: {lighting}
- Camera: {defect}
- Background: {clutter}
- Angle: {angle}

Hard rules:
- It must read as an ordinary phone photo a student took in thirty seconds. NOT a studio
  shot, NOT a catalogue image, NOT a clean white background, NOT a 3D render.
- Keep the whole scene sharp from front to back, the way a phone camera renders a room.
  The wall, the floor and whatever stands behind the object must stay clearly
  recognisable, with readable edges and surfaces.
- Light the whole room evenly and plainly, the way one ceiling light or one window does.
- Every surface must carry real-world imperfection — dust, fingerprints, uneven wear,
  slightly crooked placement — never the clean uniform surfaces and perfect symmetry of a
  3D render or a press photo.
- {clutter_rule}
- {text_rule}
- Never write the word "{color}" anywhere in the image.
- Do NOT render any real-world brand name, logo, wordmark or trademark, on the object or
  anywhere else in the frame.
- Do NOT add any caption, label, sticker, price tag, handwritten sign or note describing
  the object. Nothing in the picture may name or describe what is being sold.
- No people, no faces, no hands. No watermarks, no app interface, no screenshot borders.
"""

TEXT_RULE_BOOK = (
    "The cover may carry a short, plainly spelled subject title of one or two words "
    "(for example CALCULUS or PHYSICS) plus generic small print. Keep every word "
    "correctly spelled and do not repeat words."
)
# Round two turned a graphing calculator into a featureless black slab: forbidding all
# text stripped the keypad, and the keypad is what makes it recognisable.
TEXT_RULE_KEYPAD = (
    "The keys, buttons and display are what make this object recognisable, so show them "
    "clearly. Ordinary key markings are expected — digits, arithmetic and arrow symbols, "
    "short plain function words — and every one must be correctly spelled. Put no brand "
    "name, model number or logo anywhere."
)
TEXT_RULE_DEFAULT = (
    "Put no readable text on the object at all. Small control markings and button "
    "symbols are fine as long as they are tiny and not legible as words."
)

# Round three showed these two halves have to be stated together. Banning soft mess on
# its own stripped out the visual noise that signals "real photo", and small objects on a
# surface collapsed into clean 3D product renders.
_KEEP_THE_MESS = (
    "But the room must still look lived in: leave hard everyday things around — a tangle "
    "of cables, a mug, a cardboard box, a couple of books, a backpack — and leave the "
    "surface itself imperfect, with dust, crumbs, smudges and scratches."
)
CLUTTER_RULE_DEFAULT = (
    "Keep soft mess out of the frame: no clothing, no laundry piles, no crumpled paper, "
    "no tissues, no torn-open wrappers. (Bed sheets are fine when the item is "
    "photographed on a bed.) " + _KEEP_THE_MESS
)
CLUTTER_RULE_CLOTHING = (
    "Other garments may appear in the background since this is a clothing listing, but "
    "keep them folded or laid flat, never a heaped pile. No crumpled paper, no tissues, "
    "no torn-open wrappers anywhere in the frame. " + _KEEP_THE_MESS
)


def brand_pattern(catalog) -> re.Pattern:
    brands = {b for _c, ps in catalog.items() for _b, bl, *_ in ps for b in bl if b}
    ordered = sorted(brands, key=len, reverse=True)
    return re.compile("|".join(re.escape(b) for b in ordered), re.IGNORECASE)


def size_class(base: str) -> str:
    if base in LARGE:
        return "large"
    if base in MEDIUM:
        return "medium"
    return "small"


def axes_for(item_id: int, base: str) -> dict:
    """Deterministic per-item style draw — same id always yields the same look."""
    rng = random.Random(item_id * 7919)
    cls = size_class(base)
    if cls == "large":
        settings_pool = FLOOR_SETTINGS
    elif cls == "medium":
        settings_pool = FLOOR_SETTINGS + SURFACE_SETTINGS
    else:
        settings_pool = FLOOR_SETTINGS + SURFACE_SETTINGS + DESK_SETTINGS
    setting = rng.choice(settings_pool)
    # Large things stay at eye level even on the floor — a bicycle seen from overhead
    # would be the odd one out. Everything smaller gets shot from standing height.
    on_floor = setting in FLOOR_SETTINGS and cls != "large"
    return {
        "setting": setting,
        "framing": rng.choice(FRAMINGS),
        "lighting": rng.choice(LIGHTINGS),
        "defect": rng.choice(DEFECTS),
        "clutter": rng.choice(CLUTTERS),
        "angle": rng.choice(FLOOR_SMALL_ANGLES if on_floor else ANGLES),
        "wear": rng.choice(WEAR),
    }


def describe(item: dict, brands: re.Pattern) -> dict:
    """Brand-stripped generic noun phrase — the listing title is never sent."""
    base = item["base"]
    generic = GENERIC_BASE.get(base, base)
    clean = brands.sub("", generic)
    clean = re.sub(r"\s{2,}", " ", clean).strip(" -,")
    color = item["color"] or ""
    if base in KEYPAD:
        text_rule = TEXT_RULE_KEYPAD
    elif item["category"] == "图书与教材":
        text_rule = TEXT_RULE_BOOK
    else:
        text_rule = TEXT_RULE_DEFAULT
    is_clothing = base in CLOTHING_BASES or item["category"] == "服装与鞋靴"
    return {
        "descriptor": f"a used {clean}" if clean else "a used household item",
        "color": color or "unspecified",
        "color_phrase": color.lower() if color else "an ordinary neutral colour",
        "text_rule": text_rule,
        "clutter_rule": CLUTTER_RULE_CLOTHING if is_clothing else CLUTTER_RULE_DEFAULT,
    }


# ---------------------------------------------------------------- reference photos
def load_references() -> dict[str, list[tuple[str, str]]]:
    """category -> [(filename, data-uri)], from the annotated training crops."""
    ann = REF_DIR / "annotations.json"
    if not ann.exists():
        raise SystemExit(f"Reference set not found: {ann}")
    by_cat: dict[str, list[tuple[str, str]]] = {}
    for rec in json.loads(ann.read_text("utf-8")):
        if rec.get("confidence") == "低":
            continue  # the annotator flagged these as a poor match
        path = REF_DIR / rec["filename"]
        if not path.exists():
            continue
        uri = "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()
        by_cat.setdefault(rec["category"], []).append((rec["filename"], uri))
    return by_cat


def pick_references(by_cat, category: str, item_id: int, n: int = 3):
    """Prefer same-category references; top up from the whole pool if thin."""
    rng = random.Random(item_id * 104729)
    same = list(by_cat.get(category, []))
    rng.shuffle(same)
    chosen = same[:n]
    if len(chosen) < n:
        pool = [r for recs in by_cat.values() for r in recs if r not in chosen]
        rng.shuffle(pool)
        chosen += pool[: n - len(chosen)]
    return chosen


# ---------------------------------------------------------------- generation
def generate_one(client: httpx.Client, model: str, item: dict, refs, desc: dict) -> bytes:
    text = PROMPT.format(**desc, **axes_for(item["id"], item["base"]))
    content = [{"type": "text", "text": text}]
    for _name, uri in refs:
        content.append({"type": "image_url", "image_url": {"url": uri}})
    resp = client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    images = (data.get("choices") or [{}])[0].get("message", {}).get("images") or []
    if not images:
        raise RuntimeError(f"No image in response: {json.dumps(data)[:300]}")
    return base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1])


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


# ---------------------------------------------------------------- item selection
def load_placeholder_items() -> tuple[list[dict], re.Pattern]:
    """Items whose current photo is the locally drawn 640x480 placeholder."""
    sys.path.insert(0, str(Path(__file__).parent))
    from mock_catalog import CATALOG

    bases = sorted(
        (b for _c, ps in CATALOG.items() for b, *_ in ps), key=len, reverse=True
    )
    conn = sqlite3.connect(settings.data_path / "locswap.db")
    out = []
    for iid, name, attrs_raw, photo in conn.execute(
        "select id, name, attributes, photo_path from items order by id"
    ):
        try:
            with Image.open(settings.data_path / photo) as im:
                if im.size != (640, 480):
                    continue
        except OSError:
            continue
        attrs = json.loads(attrs_raw) if attrs_raw else {}
        low = name.lower()
        base = next((b for b in bases if b.lower() in low), None)
        out.append(
            {
                "id": iid,
                "name": name,
                "base": base or name,
                "color": attrs.get("color") or "",
                "condition": attrs.get("condition") or "成色良好",
                "category": attrs.get("category") or "",
            }
        )
    return out, brand_pattern(CATALOG)


def spread_across_categories(items: list[dict], count: int) -> list[dict]:
    """Round-robin over categories so a small batch stays representative.

    Seeded with crc32 rather than hash(): PYTHONHASHSEED is randomised per process,
    which made --dry-run disagree with the real run.
    """
    buckets: dict[str, list[dict]] = {}
    for it in items:
        buckets.setdefault(it["category"], []).append(it)
    for cat, group in buckets.items():
        random.Random(zlib.crc32(cat.encode())).shuffle(group)
    picked, order = [], sorted(buckets)
    while len(picked) < count and any(buckets.values()):
        for cat in order:
            if buckets[cat] and len(picked) < count:
                picked.append(buckets[cat].pop())
    return picked


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Generate one listing photo per item")
    ap.add_argument("--count", type=int, default=20, help="max new images this run")
    ap.add_argument("--model", default="google/gemini-2.5-flash-image")
    ap.add_argument("--workers", type=int, default=4, help="parallel requests")
    ap.add_argument("--refs", type=int, default=3, help="reference photos per request")
    ap.add_argument("--only-ids", default="", help="comma-separated item ids")
    ap.add_argument("--spread", action="store_true", help="spread across categories")
    ap.add_argument("--force", action="store_true", help="redo items already generated")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() else {}

    items, brands = load_placeholder_items()
    if args.only_ids:
        wanted = [int(x) for x in args.only_ids.split(",") if x.strip()]
        index = {i["id"]: i for i in items}
        items = [index[i] for i in wanted if i in index]
    todo = items if args.force else [i for i in items if str(i["id"]) not in manifest]
    todo = spread_across_categories(todo, args.count) if args.spread else todo[: args.count]

    print(
        f"{len(items)} candidate items; {len(manifest)} already generated"
        f"{' (--force: regenerating anyway)' if args.force else ''}.\n"
        f"This run: {len(todo)} images via {args.model}, about ${len(todo) * 0.039:.2f}.\n"
    )
    for it in todo:
        d = describe(it, brands)
        a = axes_for(it["id"], it["base"])
        print(
            f"  #{it['id']:4d} [{size_class(it['base']):6s}] {d['descriptor']}"
            f" — {d['color_phrase']}\n"
            f"         {a['setting']}; {a['framing'].split(',')[0]}"
        )
    if args.dry_run:
        return 0

    if not settings.llm_api_key or not settings.llm_base_url:
        raise SystemExit("LLM_API_KEY / LLM_BASE_URL missing (.env)")

    by_cat = load_references()
    print(f"\nReference set: {sum(len(v) for v in by_cat.values())} photos, {len(by_cat)} categories.\n")

    client = httpx.Client(
        base_url=settings.llm_base_url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=240.0,
    )
    lock = threading.Lock()
    done = {"ok": 0, "failed": 0}

    def work(item: dict) -> str:
        desc = describe(item, brands)
        refs = pick_references(by_cat, item["category"], item["id"], args.refs)
        label = f"#{item['id']} {desc['descriptor']}"
        name = f"item_{item['id']:05d}.jpg"
        for attempt in (1, 2):
            try:
                jpeg = to_jpeg(generate_one(client, args.model, item, refs, desc))
                target = OUT_DIR / name
                if target.exists():  # keep the superseded round for comparison
                    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, BACKUP_DIR / name)
                target.write_bytes(jpeg)
                with lock:
                    manifest[str(item["id"])] = {
                        "file": name,
                        "name": item["name"],
                        "base": item["base"],
                        "color": item["color"],
                        "category": item["category"],
                        "size_class": size_class(item["base"]),
                        "descriptor": desc["descriptor"],
                        "model": args.model,
                        "refs": [r[0] for r in refs],
                        "axes": axes_for(item["id"], item["base"]),
                    }
                    MANIFEST.write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8"
                    )
                    done["ok"] += 1
                    n = done["ok"] + done["failed"]
                return f"[{n}/{len(todo)}] {label} -> {name} ({len(jpeg) // 1024} KB)"
            except Exception as exc:  # noqa: BLE001 — one bad image must not sink the batch
                if attempt == 1:
                    time.sleep(3)
                    continue
                with lock:
                    done["failed"] += 1
                    n = done["ok"] + done["failed"]
                return f"[{n}/{len(todo)}] {label} FAILED: {str(exc)[:160]}"
        return ""

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fut in as_completed([pool.submit(work, it) for it in todo]):
            print(fut.result(), flush=True)

    mins = (time.time() - started) / 60
    print(
        f"\nDone: {done['ok']} new, {done['failed']} failed in {mins:.1f} min."
        f"\nLibrary now {len(manifest)} photos -> {OUT_DIR}"
        # Rejected requests are not billed, so only successes count towards spend.
        f"\nSpent roughly ${done['ok'] * 0.039:.2f}."
    )
    return 0 if done["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

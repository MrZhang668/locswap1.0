"""Stability spot-check for listing generation (LLM call site A).

Runs generation twice per sample image and reports:
  1. schema validation pass rate
  2. cross-run consistency on key fields (category/brand/condition/color exact
     match; name/summary text similarity)
For human inspection -- this is **not** a unit-test framework (the project brief
explicitly rules out unit tests, section 0).

    python scripts/eval_listing.py                     # use images in scripts/eval_images/
    python scripts/eval_listing.py --runs 3            # 3 runs per image
    python scripts/eval_listing.py --from-seed         # fall back to seed placeholder images
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  # isort:skip

from app.config import settings
from app.db import init_db
from app.services.images import compress_and_save
from app.db import SessionLocal
from app.services.publishing.pipeline import PublishingError, extract_listing
from app.services.llm import get_llm_client

EVAL_DIR = Path(__file__).resolve().parent / "eval_images"
KEY_ATTRS = ("category", "brand", "condition", "color")

#: One line of seller text per image, simulating a real listing flow --
#: deliberately messy, the way sellers actually type
DEFAULT_SELLER_TEXT = "used it for a while, everything works fine, graduating so it has to go"


def collect_images(from_seed: bool, limit: int) -> list[Path]:
    images = sorted(
        p for p in EVAL_DIR.glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if images:
        return images[:limit]
    if from_seed:
        seeded = sorted(settings.upload_path.glob("*.jpg"))[:limit]
        if seeded:
            print(f"{EVAL_DIR} not found; using {len(seeded)} seed placeholder images instead "
                  "(placeholders carry little information -- this only verifies the pipeline)")
            return seeded
    return []


def text_similarity(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, a or "", b or "").ratio(), 3)


def sentence_count(text: str) -> int:
    """Count sentence-ending punctuation ('.', '!', '?' followed by space or end),
    so decimals like '9.5' don't count."""
    return len(re.findall(r"[.!?](?:\s|$)", text or ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2, help="generations per image")
    parser.add_argument("--limit", type=int, default=5, help="max number of images")
    parser.add_argument("--from-seed", action="store_true",
                        help="use seed placeholder images when no sample images exist")
    args = parser.parse_args()

    init_db()
    client = get_llm_client()
    if not client.available:
        print(f"Skipped: {client.unavailable_reason}. Set ANTHROPIC_API_KEY in .env and rerun.")
        return 1

    images = collect_images(args.from_seed, args.limit)
    if not images:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        print(f"No sample images. Put 3-5 product photos into {EVAL_DIR}, "
              "or pass --from-seed to use placeholders.")
        return 1

    print(f"Model: {settings.llm_model_main} | {len(images)} images x {args.runs} runs\n")

    report: list[dict] = []
    total_calls = schema_ok = 0
    total_in = total_out = 0

    for image in images:
        # Go through the same compression path as production
        relative = compress_and_save(image.read_bytes(), image.name)
        runs: list[dict] = []
        for run_index in range(args.runs):
            draft = None
            error = ""
            in_tok = out_tok = 0
            try:
                with SessionLocal() as db:
                    draft, in_tok, out_tok = extract_listing(
                        db, text=DEFAULT_SELLER_TEXT, photo_path=relative
                    )
            except PublishingError as exc:
                error = str(exc)
            total_calls += 1
            total_in += in_tok
            total_out += out_tok
            if draft:
                schema_ok += 1
                runs.append(
                    {
                        "ok": True,
                        "name": draft.name,
                        "summary": draft.summary,
                        "description": draft.description,
                        "attributes": draft.attributes.model_dump(),
                        "missing_fields": draft.missing_fields,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                    }
                )
            else:
                runs.append({"ok": False, "error": error})
            print(f"  {image.name} run{run_index + 1}: "
                  f"{'OK' if draft else 'FAIL'}")

        entry: dict = {"image": image.name, "runs": runs}
        good = [r for r in runs if r["ok"]]
        if len(good) >= 2:
            a, b = good[0], good[1]
            entry["consistency"] = {
                "name_similarity": text_similarity(a["name"], b["name"]),
                "summary_similarity": text_similarity(a["summary"], b["summary"]),
                "attributes_match": {
                    k: (a["attributes"].get(k) == b["attributes"].get(k)) for k in KEY_ATTRS
                },
                "attributes_agreement": round(
                    sum(a["attributes"].get(k) == b["attributes"].get(k) for k in KEY_ATTRS)
                    / len(KEY_ATTRS),
                    3,
                ),
                "summary_sentences_ok": all(
                    sentence_count(r["summary"]) <= 2 for r in good
                ),
            }
        report.append(entry)

    print("\n===== Summary =====")
    print(f"schema pass rate: {schema_ok}/{total_calls} = {schema_ok / max(total_calls, 1):.0%}")
    print(f"tokens: input {total_in}, output {total_out}, average per call "
          f"{(total_in + total_out) / max(total_calls, 1):.0f}")
    print("\nKey-field consistency (comparing two runs):")
    for entry in report:
        c = entry.get("consistency")
        if not c:
            print(f"  {entry['image']}: fewer than 2 valid runs, cannot compare")
            continue
        mismatched = [k for k, v in c["attributes_match"].items() if not v]
        print(
            f"  {entry['image']}: attribute agreement {c['attributes_agreement']:.0%}"
            f" | name similarity {c['name_similarity']}"
            f" | summary similarity {c['summary_similarity']}"
            f" | summary<=2 sentences {'yes' if c['summary_sentences_ok'] else 'no'}"
            + (f" | mismatched fields: {mismatched}" if mismatched else "")
        )

    out_path = settings.data_path / "eval_listing_report.json"
    out_path.write_text(
        json.dumps(
            {
                "model": settings.llm_model_main,
                "runs_per_image": args.runs,
                "schema_pass_rate": round(schema_ok / max(total_calls, 1), 3),
                "total_input_tokens": total_in,
                "total_output_tokens": total_out,
                "details": report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nDetails written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Translate the mock data generators' English corpus into Chinese.

`mock_catalog.py` is the source vocabulary for two things at once: the listings
seed_mock.py generates, and — through `build_query_vocabulary()` — the search
phrasings simulate_platform.py replays. That shared origin is deliberate (a
query vocabulary invented separately left a third of listings with no matching
search wording), so both have to be translated together or not at all.

The structural keys (category, condition) were swapped mechanically during the
2026-09-15 localization; this handles the prose: product head nouns, spec
variants, condition clauses, detail clauses and use cases.

    python scripts/translate_catalog_to_zh.py --dry-run   # what would change
    python scripts/translate_catalog_to_zh.py             # translate -> cache
    python scripts/translate_catalog_to_zh.py --apply     # rewrite the .py files

Translation goes through the cache at data/_zh_catalog_cache.json, keyed by the
source string, so a re-run is free and an interrupted run resumes. `--apply`
rewrites each source string in place; nothing structural is touched, so a diff
shows only quoted prose.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  path setup

from app.config import LOCATION_BY_NAME, PROJECT_ROOT, settings  # noqa: E402
from app.services.llm.client import get_llm_client  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

CACHE_PATH = PROJECT_ROOT / "data" / "_zh_catalog_cache.json"
SCRIPTS = PROJECT_ROOT / "scripts"
TARGET_FILES = ["mock_catalog.py", "seed.py"]

BATCH = 30

#: Strings that must stay exactly as they are: brand names, model numbers,
#: emails, format strings and anything that is not prose.
KEEP_RE = re.compile(
    r"^(?:"
    r"[A-Z][A-Za-z0-9.'&+-]*(?:\s+[A-Z][A-Za-z0-9.'&+-]*)*"   # Brand Names / Proper Nouns
    r"|[\w.+-]+@[\w.-]+"                                       # emails
    r"|[\d.]+"                                                 # bare numbers
    r"|[a-z_]+(?:\.[a-z_]+)+"                                  # dotted identifiers
    r"|__\w+__"                                                # dunders
    r"|%[sd].*"                                                # format strings
    r"|\w+\d+"                                                 # demo1234 and friends
    r")$"
)

#: Internal identifiers that happen to read like English words. CONDITION_FAMILY
#: maps a category to one of these, and CONDITION_CLAUSES is keyed by them —
#: translating either side silently breaks the lookup rather than erroring.
KEEP_EXACT = {"electronics", "books", "clothing", "general"}

#: Pickup-time strings are a closed vocabulary handled by the corpus migration's
#: `translate_pickup_time` (PICKUP_DAY_EN_TO_ZH + a clock format), not by the
#: model — two translators of the same vocabulary would disagree on "Sat".
TIME_RE = re.compile(
    r"^(?:Weekdays?|Weeknights?|Weekends?|Daily|Anytime|Mon|Tue|Wed|Thu|Fri|Sat|Sun|By appointment)"
    r"(?:\s+.*)?$"
)


class Phrase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    text: str = Field(min_length=1, max_length=400)


class PhraseBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Phrase]


SYSTEM = """\
你在把一个校园二手交易平台的**商品语料库**从英文翻译成中文。这些短语会被拼装成商品标题、
描述和买家搜索词，所以它们既要像真实商品信息，也要像真实的人打出来的搜索词。

保持原样、不要翻译的内容：
- 品牌名（Apple、IKEA、Trek、Uniqlo、Hamilton Beach、Kindle 等）保留英文原拼写
- 型号与技术参数（M1、RTX 3050、WH-1000XM4、TI-84、USB-C、8GB、512GB）保留原样
- 自提地点名（The Marylander、Nine East 33rd 这类）保留英文原文

语言要求：
- 品类词（如 "laptop"、"rice cooker"）翻成中国学生真的会用的说法：笔记本电脑、电饭煲
- 规格短语（如 "13-inch M1"）译成"13 英寸 M1"这样的中文写法，数字和型号保留
- 描述句保持原意、原信息量，不要增删事实，不要加营销词
- 不要加句号以外的多余标点；原文没有句号结尾的，译文也不要加
- 逗号一律用中文逗号"，"，除非原文本身是参数列表

只返回 JSON：{"items": [{"id": 数字, "text": "译文"}]}
每一条输入都必须有对应的输出，id 原样返回。
"""


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def collect_strings(path: Path) -> list[str]:
    """Every prose string literal in one generator file, in source order."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
               and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    out: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        s = node.value
        if not s.strip() or s in seen:
            continue
        if not re.search(r"[a-z]{3}", s):        # no lowercase words -> not prose
            continue
        stripped = s.strip()
        if stripped in KEEP_EXACT or stripped in LOCATION_BY_NAME:
            continue
        if KEEP_RE.match(stripped) or TIME_RE.match(stripped):
            continue
        if re.search(r"[\u4e00-\u9fff]", s):     # already Chinese
            continue
        seen.add(s)
        out.append(s)
    return out


def translate(strings: list[str], cache: dict) -> None:
    todo = [s for s in strings if s not in cache]
    print(f"  {len(strings)} strings, {len(todo)} to translate")
    client = get_llm_client()
    if todo and not client.available:
        raise SystemExit(f"LLM unavailable: {client.unavailable_reason}")
    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        payload = [{"id": i, "text": s} for i, s in enumerate(batch)]
        result = client.complete_json(
            call_site="translate_catalog_zh",
            model=settings.llm_model_main,
            system=SYSTEM,
            content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            schema=PhraseBatch,
            max_tokens=6000,
            temperature=0.3,
        )
        if not result.ok:
            raise RuntimeError(f"translation failed: {result.error}")
        by_id = {p.id: p.text for p in result.data.items}
        missing = [i for i in range(len(batch)) if i not in by_id]
        if missing:
            raise RuntimeError(f"model skipped ids {missing}")
        for i, s in enumerate(batch):
            cache[s] = by_id[i]
        save_cache(cache)
        print(f"  {min(start + BATCH, len(todo))}/{len(todo)}")


def apply_to_file(path: Path, cache: dict) -> int:
    """Replace each translated string literal in place, longest first.

    Longest-first matters: a short phrase can be a substring of a longer one,
    and replacing the short one first would corrupt the longer literal.
    """
    src = io.open(path, encoding="utf-8").read()
    n = 0
    for en in sorted(cache, key=len, reverse=True):
        zh = cache[en]
        for quote in ('"', "'"):
            token = quote + en + quote
            if token in src:
                n += src.count(token)
                src = src.replace(token, quote + zh + quote)
    io.open(path, "w", encoding="utf-8", newline="\n").write(src)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="rewrite the generator files from the cache")
    ap.add_argument("--dry-run", action="store_true", help="only list what would be translated")
    args = ap.parse_args()

    cache = load_cache()

    if args.apply:
        total = 0
        for name in TARGET_FILES:
            n = apply_to_file(SCRIPTS / name, cache)
            print(f"{name}: {n} literals replaced")
            total += n
        print(f"total {total}")
        return 0

    for name in TARGET_FILES:
        print(f"[{name}]")
        strings = collect_strings(SCRIPTS / name)
        if args.dry_run:
            for s in strings[:20]:
                print("   ", s[:100])
            print(f"   ... {len(strings)} strings total")
            continue
        translate(strings, cache)

    if not args.dry_run:
        save_cache(cache)
        print(f"cache: {len(cache)} entries -> {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

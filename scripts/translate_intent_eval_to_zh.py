"""Translate the intent-routing eval set (scripts/intent_eval/) into Chinese.

500 labelled seller messages feed the router's acceptance run. The labels
(leaf, style, category, gold agent/scenario) are unchanged — only the seller's
own words are translated, because the router is now reading Chinese.

Two directions, not one:

* Ordinary rows are English and become Chinese.
* The 22 rows tagged ``non_english`` were **Chinese input against an English
  product** — they existed to prove the router survives a language it was not
  written in. Translating them to Chinese would delete the only thing they
  tested. They go the other way: to English, with their note rewritten.

Rewriting is done through the AST, replacing each string literal by its exact
source span, so nothing but the quoted text moves.

    python scripts/translate_intent_eval_to_zh.py --dry-run
    python scripts/translate_intent_eval_to_zh.py            # translate -> cache
    python scripts/translate_intent_eval_to_zh.py --apply    # rewrite the files
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

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.services.llm import get_llm_client  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

CACHE_PATH = PROJECT_ROOT / "data" / "_zh_intent_eval_cache.json"
EVAL_DIR = PROJECT_ROOT / "scripts" / "intent_eval"
TARGETS = ["part_a.py", "part_b.py", "part_cde.py"]

BATCH = 25
CJK = re.compile(r"[\u4e00-\u9fff]")


class Phrase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    text: str = Field(min_length=1, max_length=600)


class PhraseBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Phrase]


SYSTEM_TO_ZH = """\
你在翻译一个校园二手交易平台的**卖家提问评测集**。每一条都是卖家对平台里销售助手说的一句话，
之后会被用来评测意图路由是否判断正确。

翻译要求：
- 译成中国学生真的会打出来的话，口语、简短、可以不规范
- **保留每一条的风格特征**，这是评测集的关键：
  - 原文是打字潦草、没有标点的，译文也要潦草、没有标点
  - 原文有拼写错误的（例如 "mehcanical keyborad"），译文要有对应的中文错别字（例如"机戒键盘"）
  - 原文是含糊不清的，译文要同样含糊；原文自相矛盾的，译文也要矛盾
  - 原文很长很啰嗦的，译文也要一样长、一样啰嗦，不要概括
- 品牌名、型号（Switch Lite、Instant Pot、Giant、TI-84）保留英文原样
- 金额保留数字；"$279" 写成"279 刀"或"279 美元"都可以
- 不要回答问题，不要补充信息，只翻译

只返回 JSON：{"items": [{"id": 数字, "text": "译文"}]}
每一条输入都必须有对应的输出，id 原样返回。
"""

SYSTEM_TO_EN = """\
You are translating rows of a **seller-question eval set** for a campus
second-hand marketplace. The product's interface and its agent are in Chinese;
these particular rows exist to prove the intent router still works when the
seller writes in a language the product is *not* written in. So they must come
out as natural English.

Requirements:
- Write what an English-speaking student would actually type: colloquial, short,
  not necessarily well-punctuated.
- **Preserve each row's style**: a terse row stays terse, a rude or blunt one
  stays blunt, a prompt-injection attempt stays an injection attempt.
- Keep brand names and model numbers as they are.
- Do not answer the question and do not add information. Translate only.

Return JSON only: {"items": [{"id": <number>, "text": "<english>"}]}
Every input must have a matching output; echo the id unchanged.
"""


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def collect(path: Path) -> list[tuple[ast.Constant, str]]:
    """Every Q() row's seller text (and its ctx), with the row's style tag.

    Returns the AST nodes so `--apply` can replace them by source span.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[ast.Constant, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "Q":
            continue
        style = ""
        if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
            style = node.args[2].value
        for kw in node.keywords:
            if kw.arg == "style" and isinstance(kw.value, ast.Constant):
                style = kw.value.value
        # args[1] is the seller's message
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
           and isinstance(node.args[1].value, str):
            out.append((node.args[1], style))
        # ctx is a previous turn, and reads as the same conversation
        for kw in node.keywords:
            if kw.arg == "ctx" and isinstance(kw.value, ast.Constant) \
               and isinstance(kw.value.value, str) and kw.value.value.strip():
                out.append((kw.value, style))
    return out


def _call(system: str, batch: list[str]) -> dict[int, str]:
    client = get_llm_client()
    if not client.available:
        raise SystemExit(f"LLM unavailable: {client.unavailable_reason}")
    payload = [{"id": i, "text": t} for i, t in enumerate(batch)]
    result = client.complete_json(
        call_site="translate_intent_eval",
        model=settings.llm_model_main,
        system=system,
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
    return by_id


def translate(pairs: list[tuple[str, str]], cache: dict) -> None:
    """pairs: (text, direction) where direction is 'zh' or 'en'."""
    for direction, system in (("zh", SYSTEM_TO_ZH), ("en", SYSTEM_TO_EN)):
        todo = [t for t, d in pairs if d == direction and f"{direction}:{t}" not in cache]
        todo = list(dict.fromkeys(todo))
        if not todo:
            continue
        print(f"  -> {direction}: {len(todo)} to translate")
        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            by_id = _call(system, batch)
            for i, t in enumerate(batch):
                cache[f"{direction}:{t}"] = by_id[i]
            save_cache(cache)
            print(f"     {min(start + BATCH, len(todo))}/{len(todo)}")


def direction_for(text: str, style: str) -> str | None:
    """Which way this row goes, or None to leave it alone."""
    has_cjk = bool(CJK.search(text))
    if style == "non_english":
        # Chinese-against-an-English-product becomes English-against-a-Chinese one
        return "en" if has_cjk else None
    return None if has_cjk else "zh"


def apply_to_file(path: Path, cache: dict) -> int:
    """Rewrite each row's text in place, addressing spans in **bytes**.

    ``col_offset`` on an AST node is a UTF-8 byte offset, not a character
    offset. On a line that already contains Chinese the two diverge, and slicing
    the decoded string by those numbers lands mid-literal — it produced
    ``"...价格参考""`` on the first rehearsal. So the edit is done on the encoded
    bytes and decoded once at the end.
    """
    raw = path.read_bytes()
    line_starts = [0]
    for line in raw.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    edits = []
    for node, style in collect(path):
        text = node.value
        direction = direction_for(text, style)
        if direction is None:
            continue
        new = cache.get(f"{direction}:{text}")
        if new is None:
            continue
        start = line_starts[node.lineno - 1] + node.col_offset
        end = line_starts[node.end_lineno - 1] + node.end_col_offset
        edits.append((start, end, new))

    # Apply back-to-front so earlier spans keep their offsets.
    for start, end, new in sorted(edits, reverse=True):
        quoted = json.dumps(new, ensure_ascii=False).encode("utf-8")
        raw = raw[:start] + quoted + raw[end:]

    io.open(path, "w", encoding="utf-8", newline="\n").write(raw.decode("utf-8"))
    return len(edits)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cache = load_cache()

    if args.apply:
        total = 0
        for name in TARGETS:
            n = apply_to_file(EVAL_DIR / name, cache)
            print(f"{name}: {n} strings rewritten")
            total += n
        print(f"total {total}")
        return 0

    all_pairs: list[tuple[str, str]] = []
    for name in TARGETS:
        rows = collect(EVAL_DIR / name)
        pairs = [(node.value, direction_for(node.value, style)) for node, style in rows]
        pairs = [(t, d) for t, d in pairs if d]
        zh = sum(1 for _, d in pairs if d == "zh")
        en = sum(1 for _, d in pairs if d == "en")
        print(f"[{name}] {len(rows)} strings: {zh} -> Chinese, {en} -> English")
        all_pairs.extend(pairs)

    if args.dry_run:
        for t, d in all_pairs[:15]:
            print(f"   {d}: {t[:80]}")
        return 0

    translate(all_pairs, cache)
    save_cache(cache)
    print(f"cache: {len(cache)} entries -> {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

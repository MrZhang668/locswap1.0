"""Translate the seeded English corpus into Chinese (2026-09-15 localization).

The listings, standing wants, demand events and rule book were all generated in
English. Translating them is not cosmetic: the retrieval stack now embeds with
bge-small-zh and tokenizes with jieba, and an English corpus under a Chinese
model scores like noise.

Two phases, deliberately separate:

    python scripts/translate_corpus_to_zh.py --scope all      # translate -> cache
    python scripts/translate_corpus_to_zh.py --scope all --apply   # cache -> DB

The cache (data/_zh_corpus_cache.json) is keyed by source text, so a re-run
costs nothing for rows already done and an interrupted run resumes where it
stopped. Nothing writes to the DB until --apply, so the translation can be read
before it lands.

Canonical attribute values (category / condition / color) are NOT sent to the
model: they map through config's own dictionaries, so the corpus and the
dictionaries cannot disagree about what a category is called.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  path setup

from app.config import (  # noqa: E402
    CATEGORY_DICT,
    CONDITION_DICT,
    PICKUP_DAY_EN_TO_ZH,
    PROJECT_ROOT,
    normalize_category,
    normalize_color,
    normalize_condition,
    settings,
)
from app.services.llm import get_llm_client  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

CACHE_PATH = PROJECT_ROOT / "data" / "_zh_corpus_cache.json"
DB_PATH = PROJECT_ROOT / "data" / "locswap.db"

BATCH_ITEMS = 10
BATCH_PHRASES = 40


# ============================================================
# Schemas
# ============================================================
class ItemTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1200)
    summary: str = Field(min_length=1, max_length=160)


class ItemBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ItemTranslation]


class PhraseTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    text: str = Field(min_length=1, max_length=200)


class PhraseBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PhraseTranslation]


class RuleTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=1000)


class RuleBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RuleTranslation]


# ============================================================
# Shared instructions
# ============================================================
_CATEGORIES = "、".join(CATEGORY_DICT.keys())
_CONDITIONS = "、".join(CONDITION_DICT.keys())

GLOSSARY = f"""\
统一术语（必须使用，不要自创同义词）：
- 商品类别：{_CATEGORIES}
- 成色：{_CONDITIONS}

保持原样、不要翻译的内容：
- 品牌名（Apple、IKEA、Trek、Uniqlo、Instant Pot、Hamilton Beach 等）保留英文原拼写
- 自提地点名（The Marylander、ICON Residences、N Charles St 2800 block townhouses 等）保留英文原文
- 型号、尺寸、功率、容量等参数（900W、0.9 cu ft、26寸、6-cup、TI-84）保留数字，单位可用中文习惯写法
- 课程名、考试名（MCAT、LSAT）保留缩写
"""

SYSTEM_ITEMS = f"""\
你在把一个校园二手交易平台的商品信息从英文翻译成中文。使用者是中国留学生，平台在美国校园内。

{GLOSSARY}

语言要求：
- 用自然的中文二手交易口吻，像同学之间发布闲置，不要广告腔、不要夸张营销词
- name：商品标题，简洁，一般不超过 20 个汉字，突出品类和关键属性
- description：忠实翻译原文的全部信息（成色、使用时长、瑕疵、适用场景、交易说明），不要增删事实，不要扩写
- summary：一句到两句的简短总结，不超过 40 个汉字
- 不要出现"本店""欢迎选购"这类商家用语
- 不要输出英文句子（品牌/型号除外）

只返回 JSON：{{"items": [{{"id": 数字, "name": "...", "description": "...", "summary": "..."}}]}}
每一条输入都必须有对应的输出，id 原样返回。
"""

SYSTEM_PHRASES = f"""\
你在把一个校园二手交易平台上买家的搜索词/求购需求从英文翻译成中文。

{GLOSSARY}

语言要求：
- 翻译成中国学生真实会在搜索框里输入的说法，口语化、简短
- "cheap laptop for notes" -> "记笔记用的便宜笔记本电脑"；"warm jacket for winter" -> "冬天穿的保暖外套"
- 保持需求本身不变，不要补充原文没有的条件
- 不要加标点结尾，不要输出英文（品牌/型号除外）

只返回 JSON：{{"items": [{{"id": 数字, "text": "..."}}]}}
每一条输入都必须有对应的输出，id 原样返回。
"""

SYSTEM_RULES = f"""\
你在把一个校园二手交易平台的《发布规则》从英文翻译成中文。这些规则会展示给卖家，也会作为审核依据。

{GLOSSARY}

语言要求：
- 用平台规则的书面中文，清楚、克制、不带情绪
- title：规则标题，不超过 20 个汉字
- body：忠实翻译，保留所有被禁止/被限制的具体物品和例外情况，不要漏项、不要新增
- 保留原文的语气强度：禁止就是禁止（"不得""禁止"），提醒就是提醒（"建议""请注意"）

只返回 JSON：{{"items": [{{"id": 数字, "title": "...", "body": "..."}}]}}
每一条输入都必须有对应的输出，id 原样返回。
"""


# ============================================================
# Cache
# ============================================================
def _load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
    )


#: Anything already in Chinese has been migrated; re-translating it would spend
#: a call to paraphrase settled copy into something slightly different.
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_translated(*parts: str) -> bool:
    return any(_CJK_RE.search(p or "") for p in parts)


def _key(kind: str, *parts: str) -> str:
    raw = chr(31).join(p or "" for p in parts)  # unit separator: never appears in listing text
    return f"{kind}:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


# ============================================================
# LLM
# ============================================================
def _call(system: str, payload: list[dict], schema, max_tokens: int):
    client = get_llm_client()
    if not client.available:
        raise SystemExit(f"LLM unavailable: {client.unavailable_reason}")
    result = client.complete_json(
        call_site="translate_zh",
        model=settings.llm_model_main,
        system=system,
        content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        schema=schema,
        max_tokens=max_tokens,
        temperature=0.3,
    )
    if not result.ok:
        raise RuntimeError(f"translation failed: {result.error}")
    return result.data


def _batched(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ============================================================
# Pickup times (rules, not the model — they are a closed vocabulary)
# ============================================================
_TIME_RE = re.compile(r"^(?P<day>.*?)\s*(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>AM|PM)$", re.IGNORECASE)


def translate_pickup_time(value: str | None) -> str | None:
    """'Weekdays 6:00 PM' -> '工作日 18:00'. Unknown strings are returned as-is."""
    if not value:
        return value
    raw = value.strip()
    if raw in PICKUP_DAY_EN_TO_ZH:
        return PICKUP_DAY_EN_TO_ZH[raw]
    m = _TIME_RE.match(raw)
    if not m:
        return raw
    day = m.group("day").strip()
    hour = int(m.group("h")) % 12
    if m.group("ap").upper() == "PM":
        hour += 12
    day_zh = PICKUP_DAY_EN_TO_ZH.get(day, day)
    return f"{day_zh} {hour:02d}:{m.group('m')}"


# ============================================================
# Scopes
# ============================================================
def translate_items(conn, cache, limit: int | None) -> None:
    rows = conn.execute(
        "select id, name, description, summary from items order by id"
    ).fetchall()
    done = [r for r in rows if _is_translated(r[1], r[2], r[3])]
    todo = [
        r for r in rows
        if not _is_translated(r[1], r[2], r[3])
        and _key("item", r[1], r[2] or "", r[3] or "") not in cache
    ]
    if limit:
        todo = todo[:limit]
    print(f"[items] {len(rows)} total, {len(done)} already Chinese, {len(todo)} to translate")
    for n, batch in enumerate(_batched(todo, BATCH_ITEMS), start=1):
        payload = [
            {"id": r[0], "name": r[1], "description": r[2] or "", "summary": r[3] or ""}
            for r in batch
        ]
        data = _call(SYSTEM_ITEMS, payload, ItemBatch, max_tokens=4000)
        by_id = {t.id: t for t in data.items}
        missing = [r[0] for r in batch if r[0] not in by_id]
        if missing:
            raise RuntimeError(f"model skipped item ids {missing}")
        for r in batch:
            t = by_id[r[0]]
            cache[_key("item", r[1], r[2] or "", r[3] or "")] = {
                "name": t.name,
                "description": t.description,
                "summary": t.summary,
            }
        _save_cache(cache)
        done = min(n * BATCH_ITEMS, len(todo))
        print(f"  [items] {done}/{len(todo)}")


def _translate_phrases(conn, cache, kind: str, phrases: list[str], limit: int | None) -> None:
    todo = [p for p in phrases
            if p and not _is_translated(p) and _key(kind, p) not in cache]
    if limit:
        todo = todo[:limit]
    print(f"[{kind}] {len(phrases)} unique, {len(todo)} to translate")
    for n, batch in enumerate(_batched(todo, BATCH_PHRASES), start=1):
        payload = [{"id": i, "text": p} for i, p in enumerate(batch)]
        data = _call(SYSTEM_PHRASES, payload, PhraseBatch, max_tokens=4000)
        by_id = {t.id: t.text for t in data.items}
        missing = [i for i in range(len(batch)) if i not in by_id]
        if missing:
            raise RuntimeError(f"model skipped {kind} ids {missing}")
        for i, p in enumerate(batch):
            cache[_key(kind, p)] = by_id[i]
        _save_cache(cache)
        print(f"  [{kind}] {min(n * BATCH_PHRASES, len(todo))}/{len(todo)}")


def translate_wants(conn, cache, limit: int | None) -> None:
    raw = {r[0] for r in conn.execute("select raw_query from standing_want") if r[0]}
    sem = {r[0] for r in conn.execute("select semantic_text from standing_want") if r[0]}
    _translate_phrases(conn, cache, "want", sorted(raw | sem), limit)


def translate_demand(conn, cache, limit: int | None) -> None:
    qs = {r[0] for r in conn.execute("select query from demand_event") if r[0]}
    _translate_phrases(conn, cache, "demand", sorted(qs), limit)


def translate_threads(conn, cache, limit: int | None) -> None:
    ts = {r[0] for r in conn.execute("select title from advisor_threads") if r[0]}
    _translate_phrases(conn, cache, "thread", sorted(ts), limit)


def translate_rules(conn, cache, limit: int | None) -> None:
    rows = conn.execute("select id, title, body from listing_rules order by id").fetchall()
    todo = [r for r in rows
            if not _is_translated(r[1], r[2]) and _key("rule", r[1], r[2]) not in cache]
    if limit:
        todo = todo[:limit]
    print(f"[rules] {len(rows)} total, {len(todo)} to translate")
    for n, batch in enumerate(_batched(todo, 6), start=1):
        payload = [{"id": r[0], "title": r[1], "body": r[2]} for r in batch]
        data = _call(SYSTEM_RULES, payload, RuleBatch, max_tokens=4000)
        by_id = {t.id: t for t in data.items}
        missing = [r[0] for r in batch if r[0] not in by_id]
        if missing:
            raise RuntimeError(f"model skipped rule ids {missing}")
        for r in batch:
            t = by_id[r[0]]
            cache[_key("rule", r[1], r[2])] = {"title": t.title, "body": t.body}
        _save_cache(cache)
        print(f"  [rules] {min(n * 6, len(todo))}/{len(todo)}")


# ============================================================
# Apply
# ============================================================
def apply_all(conn, cache) -> None:
    cur = conn.cursor()

    # ---- items ----
    rows = cur.execute(
        "select id, name, description, summary, attributes, pickup_time_start, pickup_time_end from items"
    ).fetchall()
    translated = skipped = 0
    for item_id, name, desc, summary, attrs_raw, t_start, t_end in rows:
        # Already-Chinese rows keep their text: a cache entry keyed on Chinese
        # source is a re-translation, and applying it would swap settled wording
        # for a paraphrase.
        hit = (None if _is_translated(name, desc, summary)
               else cache.get(_key("item", name, desc or "", summary or "")))
        try:
            attrs = json.loads(attrs_raw or "{}")
        except json.JSONDecodeError:
            attrs = {}
        attrs["category"] = normalize_category(attrs.get("category"))
        attrs["condition"] = normalize_condition(attrs.get("condition"))
        attrs["color"] = normalize_color(attrs.get("color"))
        if hit:
            translated += 1
            cur.execute(
                "update items set name=?, description=?, summary=?, attributes=?, "
                "pickup_time_start=?, pickup_time_end=? where id=?",
                (
                    hit["name"], hit["description"], hit["summary"],
                    json.dumps(attrs, ensure_ascii=False),
                    translate_pickup_time(t_start), translate_pickup_time(t_end),
                    item_id,
                ),
            )
        else:
            skipped += 1
            cur.execute(
                "update items set attributes=?, pickup_time_start=?, pickup_time_end=? where id=?",
                (
                    json.dumps(attrs, ensure_ascii=False),
                    translate_pickup_time(t_start), translate_pickup_time(t_end),
                    item_id,
                ),
            )
    print(f"[apply] items: {translated} translated, {skipped} attribute-only (no cached translation)")

    # ---- standing wants ----
    n = 0
    for want_id, raw_q, sem, filters_raw, category in cur.execute(
        "select id, raw_query, semantic_text, filters, category from standing_want"
    ).fetchall():
        new_raw = cache.get(_key("want", raw_q), raw_q) if raw_q else raw_q
        new_sem = cache.get(_key("want", sem), sem) if sem else sem
        try:
            filters = json.loads(filters_raw or "{}")
        except json.JSONDecodeError:
            filters = {}
        if filters.get("category"):
            filters["category"] = normalize_category(filters["category"])
        cur.execute(
            "update standing_want set raw_query=?, semantic_text=?, filters=?, category=? where id=?",
            (
                new_raw, new_sem, json.dumps(filters, ensure_ascii=False),
                normalize_category(category), want_id,
            ),
        )
        n += 1
    print(f"[apply] standing_want: {n} rows")

    # ---- demand events ----
    n = 0
    for ev_id, q, category in cur.execute(
        "select id, query, category from demand_event"
    ).fetchall():
        new_q = cache.get(_key("demand", q), q) if q else q
        cur.execute(
            "update demand_event set query=?, category=? where id=?",
            (new_q, normalize_category(category), ev_id),
        )
        n += 1
    print(f"[apply] demand_event: {n} rows")

    # ---- notifications (payload snapshots of item names / pickup times) ----
    name_by_id = {r[0]: r[1] for r in cur.execute("select id, name from items")}
    n = 0
    for note_id, payload_raw in cur.execute("select id, payload from notifications").fetchall():
        try:
            payload = json.loads(payload_raw or "{}")
        except json.JSONDecodeError:
            continue
        changed = False
        if payload.get("item_id") in name_by_id and "item_name" in payload:
            payload["item_name"] = name_by_id[payload["item_id"]]
            changed = True
        if isinstance(payload.get("pickup_time_range"), str) and "~" in payload["pickup_time_range"]:
            start, _, end = payload["pickup_time_range"].partition("~")
            payload["pickup_time_range"] = (
                f"{translate_pickup_time(start.strip())} ~ {translate_pickup_time(end.strip())}"
            )
            changed = True
        if changed:
            cur.execute(
                "update notifications set payload=? where id=?",
                (json.dumps(payload, ensure_ascii=False), note_id),
            )
            n += 1
    print(f"[apply] notifications: {n} payloads refreshed")

    # ---- listing rules ----
    n = 0
    for rule_id, title, body in cur.execute("select id, title, body from listing_rules").fetchall():
        hit = (None if _is_translated(title, body)
               else cache.get(_key("rule", title, body)))
        if hit:
            cur.execute(
                "update listing_rules set title=?, body=? where id=?",
                (hit["title"], hit["body"], rule_id),
            )
            n += 1
    print(f"[apply] listing_rules: {n} rows")

    # ---- advisor thread titles ----
    n = 0
    for thread_id, title in cur.execute("select id, title from advisor_threads").fetchall():
        hit = cache.get(_key("thread", title)) if title else None
        if hit:
            cur.execute("update advisor_threads set title=? where id=?", (hit, thread_id))
            n += 1
    print(f"[apply] advisor_threads: {n} rows")

    conn.commit()
    print("[apply] committed")


SCOPES = {
    "items": translate_items,
    "wants": translate_wants,
    "demand": translate_demand,
    "rules": translate_rules,
    "threads": translate_threads,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", default="all", choices=[*SCOPES, "all"])
    ap.add_argument("--limit", type=int, default=None, help="translate at most N rows (smoke test)")
    ap.add_argument("--apply", action="store_true", help="write the cached translation into the DB")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cache = _load_cache()

    if args.apply:
        apply_all(conn, cache)
        return 0

    scopes = list(SCOPES) if args.scope == "all" else [args.scope]
    for scope in scopes:
        SCOPES[scope](conn, cache, args.limit)
    _save_cache(cache)
    print(f"cache: {len(cache)} entries -> {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

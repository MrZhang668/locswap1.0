"""Translate item names frozen inside notifications whose listing is gone.

A notification stores a snapshot of the listing it is about — name, price,
pickup — because the listing itself may be deleted by the time the buyer reads
it (expired, delisted, sold). The corpus migration refreshes those snapshots
from the `items` table, which works for every listing that still exists and
leaves the rest in English: 71 rows on 2026-09-15, all pointing at items that
had already been deleted.

They are buyer-facing — an English product name sitting inside a Chinese
notification — so they get translated directly from the snapshot.

    python scripts/translate_orphan_notifications.py            # translate -> cache
    python scripts/translate_orphan_notifications.py --apply    # write payloads
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

import _bootstrap  # noqa: F401  path setup

from app.config import PROJECT_ROOT, settings  # noqa: E402
from app.services.llm import get_llm_client  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

CACHE_PATH = PROJECT_ROOT / "data" / "_zh_orphan_names_cache.json"
DB_PATH = PROJECT_ROOT / "data" / "locswap.db"
CJK = re.compile(r"[一-鿿]")
BATCH = 30


class Phrase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    text: str = Field(min_length=1, max_length=80)


class PhraseBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Phrase]


SYSTEM = """\
你在把一个校园二手交易平台的**商品标题**从英文翻译成中文。这些标题会出现在买家的消息通知里。

要求：
- 品牌名和型号保留英文原拼写（Nike、IKEA、HP、Casio、TI-84、Instant Pot）
- 尺寸、容量、功率等参数保留数字，单位用中文习惯写法（27-inch -> 27 寸）
- 简洁，一般不超过 20 个汉字，突出品类和关键属性
- 不要加引号、不要加句号

只返回 JSON：{"items": [{"id": 数字, "text": "译文"}]}
每一条输入都必须有对应的输出，id 原样返回。
"""


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def english_names(conn) -> list[str]:
    names = []
    for (payload,) in conn.execute("select payload from notifications"):
        try:
            d = json.loads(payload or "{}")
        except json.JSONDecodeError:
            continue
        n = d.get("item_name") or ""
        if n and not CJK.search(n):
            names.append(n)
    return sorted(set(names))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cache = load_cache()
    names = english_names(conn)
    print(f"{len(names)} distinct English item names in notification payloads")

    if args.apply:
        cur = conn.cursor()
        n = 0
        for note_id, payload in cur.execute("select id, payload from notifications").fetchall():
            try:
                d = json.loads(payload or "{}")
            except json.JSONDecodeError:
                continue
            name = d.get("item_name") or ""
            zh = cache.get(name)
            if not name or CJK.search(name) or not zh:
                continue
            d["item_name"] = zh
            cur.execute("update notifications set payload=? where id=?",
                        (json.dumps(d, ensure_ascii=False), note_id))
            n += 1
        conn.commit()
        print(f"rewrote {n} notification payloads")
        return 0

    todo = [n for n in names if n not in cache]
    print(f"{len(todo)} to translate")
    if not todo:
        return 0

    client = get_llm_client()
    if not client.available:
        raise SystemExit(f"LLM unavailable: {client.unavailable_reason}")

    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        result = client.complete_json(
            call_site="translate_orphan_names",
            model=settings.llm_model_main,
            system=SYSTEM,
            content=[{"type": "text", "text": json.dumps(
                [{"id": i, "text": t} for i, t in enumerate(batch)], ensure_ascii=False)}],
            schema=PhraseBatch,
            max_tokens=3000,
            temperature=0.3,
        )
        if not result.ok:
            raise RuntimeError(f"translation failed: {result.error}")
        by_id = {p.id: p.text for p in result.data.items}
        missing = [i for i in range(len(batch)) if i not in by_id]
        if missing:
            raise RuntimeError(f"model skipped ids {missing}")
        for i, t in enumerate(batch):
            cache[t] = by_id[i]
        save_cache(cache)
        print(f"  {min(start + BATCH, len(todo))}/{len(todo)}")

    save_cache(cache)
    print(f"cache: {len(cache)} entries -> {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

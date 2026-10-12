"""调用点 I：卖家点名的那件东西，是他在售的哪一件。

以前这件事只有一种判法：把卖家的说法和商品标题各自分词，有一个词重合就算。
它同时在两个方向上出错——

* **漏**：「台灯」和「LED护眼书桌灯」一个词都不重合，于是一件在售的东西被说成
  没有，卖家被劝去重新上架；
* **错**：「自行车反光灯」和「Giant白色通勤自行车」重合了「自行车」，于是一个
  问反光灯的卖家拿到了一份自行车的分析。

现在分两层，后一层只在前一层判不出来时才跑：

1. **中心词后缀关系**（不花模型调用）。商品上存着中心词（`attributes.head_noun`，
   上架时抽取），卖家的说法是路由器抽出来的一个名词短语，中文复合名词的中心在
   后面，所以「白色台灯」→ 台灯、「那辆自行车」→ 自行车 都能直接认出来，而「自行
   车反光灯」的中心是反光灯，不会被自行车认领。没有中心词的旧商品退回按标题
   分词比对，但只算在卖家说法里**不是修饰语**的那些词。
2. **模型判定 + 子类目过滤**。叫法不同的同一种东西（台灯 / 护眼灯）只有模型认得出。
   模型同时说出卖家说的这件东西属于哪个子类目；它给的商品里，子类目对不上的由
   **服务端**丢掉——判断换了来源，后果仍然在这里定。

**为什么不用向量相似度当门槛。** 这是第一个想到的办法，量过之后放弃了：在
bge-small-zh 上，`自行车反光灯↔自行车` 是 0.705，`台灯↔护眼灯` 只有 0.577，
`台灯↔串灯` 是 0.658，`耳机↔耳机架` 是 0.812。短词的向量分不开「同一种东西」
和「字面相近的另一种东西」，任何一个门槛都会同时放进错的、挡掉对的。

模型调用失败时返回 `unavailable`，调用方退回原来的判据，而不是当成「没有」：
把一件在售的东西说成没有，卖家会被劝去重复上架。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...config import SUBCATEGORY_PARENT, subcategory_prompt_block
from ..llm.langchain_client import (
    CALL_SITE_ITEM_LOOKUP,
    build_chat_model,
    record_usage,
    router_model_name,
)
from ..nlp import intent_rules
from ..prompts import load_prompt
from ..text import match_position, normalize, raw_tokens

logger = logging.getLogger(__name__)

SOURCE_HEAD = "head"                # 中心词后缀关系命中
SOURCE_NAME = "name"                # 没有中心词的商品，按标题分词命中
SOURCE_MODEL = "model"              # 模型判定，过了子类目过滤
SOURCE_NONE = "none"                # 判过了，确实没有
SOURCE_UNAVAILABLE = "unavailable"  # 没判成（模型调用失败），调用方退回旧判据

#: 一次判定最多给模型看这么多件在售商品。再多就不是「查一件东西」了。
MAX_LISTINGS_IN_PROMPT = 80

#: 卖家说法开头那些不是商品名的部分：「我的」「那辆」「这个」。
_LEAD_TOKENS = frozenset({
    "我", "我的", "的", "咱", "那", "这", "那个", "这个", "那件", "这件", "那台", "这台",
    "那辆", "这辆", "那把", "这把", "那本", "这本", "那双", "这双", "那条", "这条",
    "那张", "这张", "那只", "这只", "一个", "一件", "一台", "一辆", "个", "件", "台", "辆",
    "它", "它们", "这些", "那些", "这几件", "那几件", "这俩", "那俩", "这两件", "那两件",
    "几件", "几个", "两件", "两个", "俩", "几",
})
#: 结尾那些不是商品名的部分：「Giant 那辆」的「那辆」，「台灯吗」的「吗」。
_TRAIL_TOKENS = _LEAD_TOKENS | frozenset({"了", "吗", "呢", "吧", "啊", "呀"})


@dataclass
class Resolution:
    """一个说法的判定结果。`matches` 是 `{"item_id", "name"}` 的列表。"""

    ref: str
    matches: list[dict[str, Any]] = field(default_factory=list)
    source: str = SOURCE_NONE
    #: 模型给的、卖家说的这件东西本身的子类目。只有走到第二层才有。
    subcategory: str | None = None

    @property
    def decided(self) -> bool:
        return self.source != SOURCE_UNAVAILABLE


class _LookupVerdict(BaseModel):
    """模型的出参。三个字段，只回答一个问题。"""

    model_config = ConfigDict(extra="forbid")

    subcategory: str = Field(default="", max_length=40)
    head_noun: str | None = Field(default=None, max_length=20)
    item_ids: list[int] = Field(default_factory=list, max_length=20)


# ============================================================
# 第一层：中心词
# ============================================================
def _core_tokens(ref: str) -> list[str]:
    toks = [t for t in raw_tokens(ref or "") if t.strip()]
    while toks and toks[0] in _LEAD_TOKENS:
        toks.pop(0)
    while toks and toks[-1] in _TRAIL_TOKENS:
        toks.pop()
    return toks


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", normalize(text))


def head_hit(ref: str, head_noun: str) -> bool:
    """卖家的说法和这个中心词是不是同一种东西（只看字面的后缀关系）。

    * 说法以中心词结尾：「白色台灯」「那辆自行车」→ 是；
    * 中心词以说法的最后一个词结尾：「台灯」之于「护眼台灯」，泛称「灯」之于
      「台灯」→ 是。最后一个词只有一个字时，只在整个说法就是这一个字时才算——
      否则「耳机架」的「架」会认领一个书架。
    """
    h = _squash(head_noun)
    toks = _core_tokens(ref)
    if not h or not toks:
        return False
    core = "".join(toks)
    if core.endswith(h):
        return True
    tail = toks[-1]
    if len(toks) == 1 or len(tail) >= 2:
        return h.endswith(tail)
    return False


def _name_hit(ref: str, name: str) -> int:
    """没有中心词的商品：标题和说法重合、且在说法里不是修饰语的词有几个。"""
    overlap = intent_rules.content_tokens(ref) & intent_rules.content_tokens(name)
    if not overlap:
        return 0
    toks = _core_tokens(ref)
    joined = "".join(toks)
    return sum(1 for t in overlap if match_position(toks, joined, t) != "modifier")


def _modifiers(ref: str, head_noun: str) -> set[str]:
    """说法里中心词以外的实词：「西班牙语教材」的西班牙语，「白色台灯」的白色。"""
    h = _squash(head_noun)
    return {t for t in intent_rules.content_tokens(" ".join(_core_tokens(ref))) if t not in h}


def lexical_matches(ref: str, listings: list[dict[str, Any]]) -> tuple[list[dict], str]:
    """第一层。返回 (命中, 来源)；没有命中时来源是 SOURCE_NONE。

    中心词对上只是必要条件。卖家的说法里还带了修饰词时（「西班牙语教材」「白色
    台灯」），至少要有一个修饰词也出现在标题里，并且只留重合最多的那几件——否则
    一摞中心词都是「教材」的书会全部被「西班牙语教材」认领，而卖家只有一本物理
    教材时，他会拿到一份物理教材的分析。一个修饰词都对不上的，这一层不判，交给
    模型：「白色台灯」和「黑色台灯」算不算一件，字面上说不清。
    """
    scored: list[tuple[int, dict]] = []
    named: list[tuple[int, dict]] = []
    for row in listings or []:
        head = str(row.get("head_noun") or "")
        name = str(row.get("name") or "")
        hit = {"item_id": row.get("item_id"), "name": name[:60]}
        if head:
            if head_hit(ref, head):
                mods = _modifiers(ref, head)
                if not mods:
                    scored.append((0, hit))
                else:
                    overlap = len(mods & intent_rules.content_tokens(name))
                    if overlap:
                        scored.append((overlap, hit))
        else:
            n = _name_hit(ref, name)
            if n:
                named.append((n, hit))
    # 没有中心词的老商品和 `intent_rules.best_match` 同一个口径：只留重合最多的。
    by_name = [hit for n, hit in named if n == max(n for n, _ in named)] if named else []
    if scored:
        best = max(s for s, _ in scored)
        by_head = [hit for s, hit in scored if s == best]
        return by_head + by_name, SOURCE_HEAD
    if by_name:
        return by_name, SOURCE_NAME
    return [], SOURCE_NONE


# ============================================================
# 第二层：模型 + 子类目过滤
# ============================================================
def _listing_rows(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"item_id": r.get("item_id"), "name": str(r.get("name") or "")[:60],
         "subcategory": r.get("subcategory") or r.get("category") or ""}
        for r in listings[:MAX_LISTINGS_IN_PROMPT]
    ]


def ask_model(ref: str, message: str, listings: list[dict[str, Any]]) -> _LookupVerdict | None:
    """一次调用点 I。失败返回 None，从不抛。

    写成模块级函数是为了让离线验收脚本能把它换成桩。
    """
    model = router_model_name()
    try:
        chat = build_chat_model(model=model, temperature=0.0, max_tokens=300)
        structured = chat.with_structured_output(_LookupVerdict, include_raw=True)
        result = structured.invoke(
            [
                ("system", load_prompt("item_lookup").replace(
                    "{{SUBCATEGORY_TREE}}", subcategory_prompt_block())),
                (
                    "human",
                    f"<message>{(message or '')[:300]}</message>\n"
                    f"<ref>{ref[:120]}</ref>\n"
                    f"<listings>{json.dumps(_listing_rows(listings), ensure_ascii=False)}</listings>",
                ),
            ]
        )
    except Exception as exc:  # noqa: BLE001  网络 / 鉴权 / provider 的各种脾气
        logger.warning("[item-lookup] call failed (%s)", type(exc).__name__)
        return None
    raw = result.get("raw") if isinstance(result, dict) else None
    if raw is not None:
        record_usage(CALL_SITE_ITEM_LOOKUP, model, raw)
    verdict = result.get("parsed") if isinstance(result, dict) else None
    if verdict is None:
        logger.warning("[item-lookup] unparseable verdict: %s",
                       str(result.get("parsing_error") if isinstance(result, dict) else "")[:160])
    return verdict


def filter_by_subcategory(
    verdict: _LookupVerdict, listings: list[dict[str, Any]]
) -> tuple[list[dict], list[str]]:
    """模型给的商品里，留下子类目和说法一致的那些。返回 (留下的, 丢掉的原因)。

    这是服务端的那一半：模型可以说「自行车反光灯就是那辆自行车」，但它同时说了
    反光灯属于骑行配件，而那辆自行车不是——两句话里信后一句，因为它能被核对。
    还没有子类目的旧商品退一级，按大类核对。
    """
    sub = verdict.subcategory.strip()
    parent = SUBCATEGORY_PARENT[sub]
    by_id = {int(r["item_id"]): r for r in listings if r.get("item_id") is not None}
    kept: list[dict] = []
    dropped: list[str] = []
    for iid in dict.fromkeys(verdict.item_ids):
        row = by_id.get(int(iid))
        if row is None:
            dropped.append(f"{iid}: not a listing of this seller")
            continue
        row_sub = row.get("subcategory")
        row_cat = row.get("category")
        if row_sub and row_sub != sub:
            dropped.append(f"{iid}: subcategory {row_sub} != {sub}")
            continue
        if not row_sub and row_cat and row_cat != parent:
            dropped.append(f"{iid}: category {row_cat} != {parent}")
            continue
        kept.append({"item_id": row.get("item_id"), "name": str(row.get("name") or "")[:60]})
    return kept, dropped


# ============================================================
# 入口
# ============================================================
_CACHE: OrderedDict[tuple, Resolution] = OrderedDict()
_CACHE_MAX = 256
_CACHE_LOCK = threading.Lock()


def _cache_key(ref: str, listings: list[dict[str, Any]]) -> tuple:
    return (
        _squash(ref),
        tuple(sorted(
            (int(r.get("item_id") or 0), str(r.get("name") or ""),
             str(r.get("subcategory") or ""), str(r.get("head_noun") or ""))
            for r in listings
        )),
    )


def resolve_ref(ref: str, listings: list[dict[str, Any]], *, message: str = "") -> Resolution:
    ref = (ref or "").strip()
    if not ref or not listings:
        return Resolution(ref=ref, source=SOURCE_NONE)
    if not _core_tokens(ref):
        # 「它」「这个」「那件」——代词不是商品名，这一层判不了。交回调用方的指代
        # 逻辑（上一轮的商品、只有一件时就是它），而不是拿一个代词去问模型，再把
        # 「没对上」当成「没有」。
        return Resolution(ref=ref, source=SOURCE_UNAVAILABLE)

    matches, source = lexical_matches(ref, listings)
    if matches:
        return Resolution(ref=ref, matches=matches, source=source)

    key = _cache_key(ref, listings)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached

    verdict = ask_model(ref, message, listings)
    if verdict is None or verdict.subcategory.strip() not in SUBCATEGORY_PARENT:
        if verdict is not None:
            logger.warning("[item-lookup] %r: unknown subcategory %r", ref, verdict.subcategory)
        # 不缓存：下一轮也许就调通了。
        return Resolution(ref=ref, source=SOURCE_UNAVAILABLE)

    kept, dropped = filter_by_subcategory(verdict, listings)
    if dropped:
        logger.info("[item-lookup] %r (%s): dropped %s", ref, verdict.subcategory, dropped)
    out = Resolution(
        ref=ref, matches=kept, source=SOURCE_MODEL if kept else SOURCE_NONE,
        subcategory=verdict.subcategory.strip(),
    )
    logger.info("[item-lookup] %r -> %s %s", ref, out.subcategory, [m["name"] for m in kept])
    with _CACHE_LOCK:
        _CACHE[key] = out
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return out


def resolve_refs(
    refs: list[str], listings: list[dict[str, Any]], *, message: str = ""
) -> dict[str, Resolution]:
    return {r: resolve_ref(r, listings, message=message) for r in dict.fromkeys(refs) if r}


#: 名词短语里要剔掉的时间词。jieba 把「下学期」标成名词，不剔的话「自行车下学期
#: 再卖」会切出「自行车下学期」这么一个短语，中心词就成了下学期。
_TIME_TOKEN_RE = re.compile(
    r"(学期|期末|期中|假期|寒假|暑假|学年|月份?|年底|年初|周末|星期.?|下周|本周|以后|之后)$"
)


def noun_phrases(text: str) -> list[str]:
    """整条消息里连续的名词串：「把自行车反光灯下架」→ ["自行车反光灯"]。

    给**没有** `item_ref` 的那些轮次用（路由器没点名、或者根本没调到路由器）。
    整条消息直接分词比对的毛病是分不清修饰语——「自行车」在「自行车反光灯」里只是
    修饰语；先切成名词短语，中心词规则就又能用了。

    jieba 加载不了时返回空列表：调用方于是什么都匹配不上，结果是多问一句，而不是
    绑错一件。
    """
    try:
        import jieba.posseg as pseg
    except Exception:  # noqa: BLE001  jieba 缺失是线上出过的坑，见部署记忆
        return []
    phrases: list[str] = []
    cur: list[str] = []
    for word, flag in pseg.cut(text or ""):
        w = word.strip()
        # nr（人名）不算：jieba 会把「多少钱」标成 nr，「库里」标成 nrt。
        noun = (flag.startswith("n") and not flag.startswith("nr")) or flag in ("eng", "vn")
        if w and noun and not _TIME_TOKEN_RE.search(w):
            cur.append(w)
            continue
        if cur:
            phrases.append("".join(cur))
            cur = []
    if cur:
        phrases.append("".join(cur))
    return phrases


def message_matches(message: str, listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """整条消息提到了哪几件在售商品——逐个名词短语走第一层，不调模型。"""
    hits: dict[Any, dict[str, Any]] = {}
    for phrase in noun_phrases(message):
        for m in lexical_matches(phrase, listings)[0]:
            hits.setdefault(m.get("item_id"), m)
    return list(hits.values())


def explained_away(
    name_matches: list[dict[str, Any]],
    refs: list[str],
    resolution: dict[str, Resolution],
) -> list[dict[str, Any]]:
    """整条消息的分词命中里，**没有**被这一轮的判定否掉的那些。

    `hints.name_matches` 是对整条消息做的分词比对，「有没有自行车反光灯」也会命中
    那辆自行车。如果某个说法和这件商品有重合的词、而那个说法刚被判过且没有认领
    它，这条命中就已经被解释掉了——它就是那个修饰语造成的。和任何说法都没有
    重合词的命中（「蓝牙音箱那个杯子」里的蓝牙音箱）不受影响：那是卖家在消息
    别处提到的另一件东西。
    """
    out: list[dict[str, Any]] = []
    for m in name_matches or []:
        name = str(m.get("name") or "")
        iid = m.get("item_id")
        rejected = False
        for r in refs:
            res = resolution.get(r)
            if res is None or not res.decided:
                continue
            if not (intent_rules.content_tokens(r) & intent_rules.content_tokens(name)):
                continue
            if all(x.get("item_id") != iid for x in res.matches):
                rejected = True
                break
        if not rejected:
            out.append(m)
    return out


def bind(refs: list[str], resolution: dict[str, Resolution]) -> tuple[list[str], list[dict]]:
    """把卖家的叫法换成他在售商品的标题，好让顾问拿到的是一件确定的商品。

    只在**唯一命中**时换：一个说法对上两件，是一个真问题，留给顾问按原样处理。
    返回 (新的 refs, 别名对)。别名对只记模型那一层认出来的——卖家说「台灯」、
    标题写的是「LED护眼书桌灯」，回复里得说一句是按哪件看的；「白色台灯」对上
    「白色台灯」就不用说。
    """
    out: list[str] = []
    aliases: list[dict] = []
    for r in refs:
        res = resolution.get(r)
        if res is not None and len(res.matches) == 1:
            name = str(res.matches[0].get("name") or "") or r
            out.append(name)
            if res.source == SOURCE_MODEL and name != r:
                aliases.append({"said": r, "item": name})
        else:
            out.append(r)
    return list(dict.fromkeys(out)), aliases

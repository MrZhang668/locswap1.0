"""Retrieval pipeline (spec §4, the core).

    query parsing (rules first) → hard filter → 3-way recall → RRF fusion
    → confidence check → template rendering

The whole flow costs zero LLM tokens (embeddings run on a local model); only
"rule parsing failed / low confidence" touches the LIGHT model (call site B).
Every intermediate result goes into the debug structure + backend logs to make
recall tuning easy.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..models import ItemStatus, User
from .geo import haversine_km
from .item_index import ItemIndexService
from .query_ai import understand_query
from .query_parser import ParsedQuery, merge_llm_filters, parse_query
from .text import MODIFIER_PENALTY, SUBSTRING_PENALTY, match_position

logger = logging.getLogger(__name__)

PATH_VECTOR = "vector"
PATH_BM25 = "bm25"
PATH_STRUCTURED = "structured"


@dataclass
class PathResult:
    name: str
    enabled: bool
    hits: list[tuple[int, float]] = field(default_factory=list)

    @property
    def ids(self) -> list[int]:
        return [i for i, _ in self.hits]


@dataclass
class RetrievalOutcome:
    #: hit | structured_fallback | low_confidence | clarify | empty
    decision: str
    item_ids: list[int]
    confidence: float
    clarify_question: str | None
    parsed: ParsedQuery
    debug: dict[str, Any]
    #: Candidate set after hard filtering. Used for item-traffic metrics
    #: (making the candidate pool but missing top-N counts as "almost seen").
    #: Retrieval itself does not use it.
    candidate_ids: set[int] = field(default_factory=set)


# ---------------- Privacy guard (§7) ----------------
# Both languages: the guard has to fire on the sentence a buyer actually types,
# and on this campus that is as often "卖家微信多少" as "what's the seller's
# WeChat". The Chinese half cannot use \b — it never fires between two hanzi —
# so those alternatives are plain substrings.
_CONTACT_REQUEST_RE = re.compile(
    r"(\bcontact\s*(info(rmation)?|details?)?\b|\bphone\s*number\b|\bcell\s*(phone\s*)?number\b|"
    r"\be-?mail\b|\bwhatsapp\b|\bwechat\b|\btelegram\b|\bdiscord\b|\binstagram\b|\bsnapchat\b|"
    r"\bseller'?s?\s+(name|number|email|account|id|info(rmation)?|contact)\b|"
    r"\bwho\s+is\s+(the\s+)?seller\b|\buser\s*id\b|\baccount\s*(name|id)\b|"
    r"\bhow\s+(do|can)\s+i\s+(reach|contact|message|text|call)\b|"
    r"联系方式|联系电话|手机号|电话号|微信号|微信|威信|vx|qq号|扣扣|邮箱|"
    r"怎么联系|如何联系|怎么找到(他|她|卖家)|加个?微信|留个?联系|"
    r"卖家(是谁|叫什么|的名字|账号|电话|邮箱)|用户id|账号id)",
    re.IGNORECASE,
)

CONTACT_REFUSAL_TEXT = (
    "抱歉，我不能提供任何用户的联系方式、账号或 ID。"
    "等你预订成功之后，卖家的联系方式会自动出现在「我的购物车」里，到时候在那里查看就行。"
)


def is_contact_request(text: str) -> bool:
    return bool(_CONTACT_REQUEST_RE.search(text or ""))


# ---------------- RRF ----------------
def rrf_fuse(paths: list[PathResult], k: int, top_n: int) -> tuple[list[tuple[int, float]], dict[int, list[str]]]:
    scores: dict[int, float] = {}
    sources: dict[int, list[str]] = {}
    for path in paths:
        for rank, (item_id, _score) in enumerate(path.hits, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
            sources.setdefault(item_id, []).append(path.name)
    fused = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:top_n]
    return fused, sources


def _max_possible_rrf(active_paths: int, k: int) -> float:
    """RRF score if every active path ranked the item #1 — normalizes confidence to 0~1."""
    return max(active_paths, 1) * (1.0 / (k + 1))


# ---------------- Head-word rerank ----------------
#: Head-word hits get **no boost** (stays 1.0); only modifier / literal-substring
#: hits are penalized.
#
# We tried boosting head hits and it backfired: in the query "bag that can hold
# a laptop", "laptop" is only a modifier of what the user wants, but it IS the
# head of the item "gaming laptop" — with a boost the laptop jumps to #1 and
# buries the actual backpacks. Root cause: at this point we can't reliably tell
# what the head of the *query itself* is. Penalizing has no such problem — it
# only punishes the one certain mismatch, "this word is clearly just a modifier
# in the item name".
HEAD_MATCH_BOOST = 1.0


def head_rerank(
    fused: list[tuple[int, float]],
    keywords: list[str],
    index: ItemIndexService,
) -> tuple[list[tuple[int, float]], list[dict[str, Any]]]:
    """Rerank fused results by "is the query word the head noun of the item name".

    Motivation: BM25's document-length normalization naturally favors short
    documents, so a search for "bike" lets "bike lock" (short name, literally
    contains "bike") beat "mountain bike". RRF fusion can't fix that because
    all three paths are misled by the same literal signal. The head-word test
    is an orthogonal signal: the head of a lock is "lock", not "bike".

    Rerank only — never add or drop entries; items that match no keyword stay
    neutral, so conversational queries aren't hurt ("something for a hot dorm
    summer" contains no words that would ever appear in an item name).
    """
    if not keywords or not fused:
        return fused, []

    trace: list[dict[str, Any]] = []
    adjusted: list[tuple[int, float, float]] = []
    for rank, (item_id, score) in enumerate(fused):
        rec = index.get_record(item_id)
        factor = 1.0
        position = "none"
        if rec:
            positions = [
                match_position(rec.name_tokens, rec.name_norm, kw) for kw in keywords
            ]
            if "head" in positions:
                factor, position = HEAD_MATCH_BOOST, "head"
            elif "modifier" in positions:
                factor, position = MODIFIER_PENALTY, "modifier"
            elif "substring" in positions:
                factor, position = SUBSTRING_PENALTY, "substring"
        adjusted.append((item_id, score * factor, score))
        if position != "none":
            trace.append({
                "id": item_id,
                "name": rec.name[:24] if rec else "",
                "position": position,
                "factor": factor,
                "from_rank": rank + 1,
            })

    adjusted.sort(key=lambda x: (-x[1], x[0]))
    for new_rank, (item_id, _, _) in enumerate(adjusted):
        for t in trace:
            if t["id"] == item_id:
                t["to_rank"] = new_rank + 1
    return [(i, s) for i, s, _ in adjusted], trace


class RetrievalService:
    def __init__(self, index: ItemIndexService):
        self.index = index

    # ---------------- Main entry ----------------
    def search(
        self,
        query: str,
        user: User | None = None,
        history: list[dict[str, str]] | None = None,
        clarify_round: int = 0,
        allow_llm: bool = True,
    ) -> RetrievalOutcome:
        debug: dict[str, Any] = {"query": query, "clarify_round": clarify_round}

        # ---- 1. Query parsing (rules first) ----
        parsed = parse_query(query)
        debug["rule_parse"] = {
            "filters": parsed.filters_dict(),
            "semantic_text": parsed.semantic_text,
            "keywords": parsed.keywords,
            "rule_confident": parsed.rule_confident,
        }

        llm_clarify_question: str | None = None
        if allow_llm and not parsed.rule_confident:
            output, llm_result = understand_query(query, history, reason="rule_parse_failed")
            debug["llm_query_understanding"] = {
                "called": True,
                "ok": llm_result.ok,
                "degraded": llm_result.degraded,
                "error": llm_result.error,
                "input_tokens": llm_result.input_tokens,
                "output_tokens": llm_result.output_tokens,
            }
            if output is not None:
                parsed = merge_llm_filters(
                    parsed, output.filters.model_dump(exclude_none=True), output.semantic_text
                )
                llm_clarify_question = output.clarify_question
                debug["llm_query_understanding"]["filters"] = output.filters.model_dump(exclude_none=True)
                debug["llm_query_understanding"]["semantic_text"] = output.semantic_text
                debug["llm_query_understanding"]["clarify_question"] = output.clarify_question
        else:
            debug["llm_query_understanding"] = {"called": False, "reason": "rule parsing was sufficient"}

        # ---- 1'. "Does this sentence contain anything searchable at all?" ----
        # No filters extracted AND no content words → ask a follow-up instead of
        # recalling a pile of noise (and never make items up).
        under_specified = not parsed.filters_dict() and not parsed.content_keywords
        debug["under_specified"] = under_specified
        if under_specified and clarify_round < settings.max_clarify_rounds:
            return RetrievalOutcome(
                decision="clarify",
                item_ids=[],
                confidence=0.0,
                clarify_question=llm_clarify_question or self._rule_clarify(parsed),
                parsed=parsed,
                debug=self._finish_debug(debug, [], 0.0, "clarify"),
            )

        # ---- 2. Hard filter ----
        candidates, filter_debug = self._hard_filter(parsed, user)
        debug["hard_filter"] = filter_debug

        if not candidates:
            return RetrievalOutcome(
                decision="empty",
                item_ids=[],
                confidence=0.0,
                clarify_question=None,
                parsed=parsed,
                debug=self._finish_debug(debug, [], 0.0, "empty"),
            )

        # ---- 3. Three-way recall ----
        query_text = parsed.semantic_text or query
        paths = self._recall(query_text, parsed, candidates)
        debug["recall"] = {
            p.name: {
                "enabled": p.enabled,
                "count": len(p.hits),
                "top5": [{"id": i, "score": round(s, 4)} for i, s in p.hits[:5]],
            }
            for p in paths
        }

        vector_path = next(p for p in paths if p.name == PATH_VECTOR)
        bm25_path = next(p for p in paths if p.name == PATH_BM25)
        structured_path = next(p for p in paths if p.name == PATH_STRUCTURED)

        # ---- 4'. Pure-constraint query ("anything under $50"): all three paths
        # have nothing to say, but the hard filter did return results. The user
        # gave real constraints that genuinely narrowed the pool, so "nothing
        # found" would be wrong — return the pool sorted by price ascending.
        # Must ALSO require "no content words at all" — otherwise "something
        # for gaming under $500" would get backfilled with a pile of the
        # cheapest junk.
        if (
            not vector_path.hits
            and not bm25_path.hits
            and not structured_path.hits
            and not parsed.content_keywords
            and (parsed.max_price or parsed.min_price or parsed.max_distance_km)
        ):
            ranked = sorted(
                (self.index.get_record(i) for i in candidates),
                key=lambda r: (r.price, r.item_id),
            )
            item_ids = [r.item_id for r in ranked][: settings.final_top_k]
            debug["fusion"] = {"skipped": True, "reason": "no path hits; returning hard-filter results directly (price ascending)"}
            return RetrievalOutcome(
                decision="filter_fallback",
                item_ids=item_ids,
                confidence=0.0,
                clarify_question=None,
                parsed=parsed,
                debug=self._finish_debug(debug, item_ids, 0.0, "filter_fallback"),
                candidate_ids=candidates,
            )

        # ---- 4. Neither a nor b has valid hits → fall back to c directly ----
        if not vector_path.hits and not bm25_path.hits and structured_path.hits:
            item_ids = structured_path.ids[: settings.final_top_k]
            debug["fusion"] = {"skipped": True, "reason": "semantic and keyword paths had no valid hits; using structured fallback"}
            return RetrievalOutcome(
                decision="structured_fallback",
                item_ids=item_ids,
                confidence=0.0,
                clarify_question=None,
                parsed=parsed,
                debug=self._finish_debug(debug, item_ids, 0.0, "structured_fallback"),
                candidate_ids=candidates,
            )

        # ---- 4'. RRF fusion ----
        active = [p for p in paths if p.enabled and p.hits]
        # Fuse a pool wider than final_top_k first, truncate after reranking —
        # otherwise penalized items can only move around within the top 8 and
        # the truly relevant #9 can never get in.
        fused_pool, sources = rrf_fuse(active, settings.rrf_k, settings.final_top_k * 3)

        # Confidence uses the **pre-rerank** raw RRF score: the head-word factor
        # only expresses a ranking preference and must not inflate confidence.
        confidence = 0.0
        if fused_pool:
            confidence = round(
                fused_pool[0][1] / _max_possible_rrf(len(active), settings.rrf_k), 4
            )

        reranked, rerank_trace = head_rerank(fused_pool, parsed.content_keywords, self.index)
        fused = reranked[: settings.final_top_k]
        debug["fusion"] = {
            "rrf_k": settings.rrf_k,
            "active_paths": [p.name for p in active],
            "pool_size": len(fused_pool),
            "top8": [
                {"id": i, "rrf": round(s, 5), "from": sources.get(i, [])} for i, s in fused
            ],
        }
        debug["head_rerank"] = {
            "keywords": parsed.content_keywords,
            "adjusted": [t for t in rerank_trace if t.get("from_rank") != t.get("to_rank")],
        }

        # ---- 5. Confidence check ----
        item_ids = [i for i, _ in fused]
        debug["confidence"] = {
            "value": confidence,
            "threshold": settings.retrieval_min_confidence,
        }

        if not item_ids:
            # Clearly stated but genuinely out of stock → say so honestly;
            # only ask a follow-up when the query itself was unclear.
            decision = (
                "clarify"
                if under_specified and clarify_round < settings.max_clarify_rounds
                else "empty"
            )
        elif confidence < settings.retrieval_min_confidence:
            decision = "clarify" if clarify_round < settings.max_clarify_rounds else "low_confidence"
        else:
            decision = "hit"

        if decision == "clarify":
            item_ids = []
            if llm_clarify_question is None and allow_llm:
                output, llm_result = understand_query(query, history, reason="low_confidence")
                debug.setdefault("llm_query_understanding_2", {})
                debug["llm_query_understanding_2"] = {
                    "called": True,
                    "ok": llm_result.ok,
                    "degraded": llm_result.degraded,
                    "input_tokens": llm_result.input_tokens,
                    "output_tokens": llm_result.output_tokens,
                }
                if output is not None:
                    llm_clarify_question = output.clarify_question

        return RetrievalOutcome(
            decision=decision,
            item_ids=item_ids,
            confidence=confidence,
            clarify_question=llm_clarify_question or (self._rule_clarify(parsed) if decision == "clarify" else None),
            parsed=parsed,
            debug=self._finish_debug(debug, item_ids, confidence, decision),
            candidate_ids=candidates,
        )

    # ---------------- Steps ----------------
    def _hard_filter(self, parsed: ParsedQuery, user: User | None) -> tuple[set[int], dict[str, Any]]:
        """status ∈ {selling, reserved}; distance filter when the user enabled a pickup range; price bounds."""
        origin = None
        max_distance = parsed.max_distance_km
        range_source = []
        if user and user.lat is not None and user.lng is not None:
            origin = (user.lat, user.lng)
            if user.pickup_range_enabled and user.pickup_range_km:
                range_source.append("user setting")
                max_distance = (
                    min(max_distance, user.pickup_range_km) if max_distance else user.pickup_range_km
                )
        if parsed.max_distance_km:
            range_source.append("query text")
        if origin is None:
            max_distance = None  # can't compute distance without a user location

        exclude = self._own_item_ids(user)

        # Hard filtering only takes "objective, high-cost-if-wrong" conditions:
        # status, distance, price. Category is **not** hard-filtered — the
        # dictionary is keyword matching, so "bag that can hold a laptop" would
        # be misread as Electronics and the backpacks the user actually wants
        # would be locked out of the candidate set. Category is only a scoring
        # signal for the structured path (path c).
        candidates = self.index.candidate_ids(
            statuses=ItemStatus.SEARCHABLE,
            max_price=parsed.max_price,
            min_price=parsed.min_price,
            category=None,
            origin=origin,
            max_distance_km=max_distance,
            exclude_seller_item_ids=exclude,
        )
        category_applied = False

        return candidates, {
            "status_in": list(ItemStatus.SEARCHABLE),
            "max_price": parsed.max_price,
            "min_price": parsed.min_price,
            "category": parsed.category,
            "category_applied": category_applied,  # always false: category only scores, never hard-filters
            "max_distance_km": max_distance,
            "distance_from": range_source or None,
            "excluded_own_items": len(exclude),
            "candidate_count": len(candidates),
            "index_total": len(self.index.records),
        }

    def _own_item_ids(self, user: User | None) -> set[int]:
        """Sellers shouldn't see their own items on the search page (nor reserve them)."""
        if not user:
            return set()
        from ..db import SessionLocal
        from ..models import Item

        with SessionLocal() as db:
            rows = db.query(Item.id).filter(Item.seller_id == user.id).all()
        return {int(r[0]) for r in rows}

    def _recall(self, query_text: str, parsed: ParsedQuery, candidates: set[int]) -> list[PathResult]:
        top_k = settings.recall_top_k

        vector = PathResult(PATH_VECTOR, settings.enable_vector_recall)
        if vector.enabled and query_text.strip():
            raw = self.index.vector_search(query_text, top_k, candidates)
            if raw:
                # Absolute floor + relative window: bge-family cosine baselines
                # run high, an absolute threshold alone lets noise through.
                floor = max(
                    settings.vector_min_similarity, raw[0][1] - settings.vector_relative_window
                )
                vector.hits = [(i, s) for i, s in raw if s >= floor]

        bm25 = PathResult(PATH_BM25, settings.enable_bm25_recall)
        if bm25.enabled and query_text.strip():
            raw_bm25 = self.index.bm25_search(query_text, top_k, candidates)
            if raw_bm25:
                floor = max(
                    settings.bm25_min_score, raw_bm25[0][1] * settings.bm25_relative_cutoff
                )
                bm25.hits = [(i, s) for i, s in raw_bm25 if s >= floor]

        structured = PathResult(PATH_STRUCTURED, settings.enable_structured_recall)
        if structured.enabled:
            structured.hits = self.index.structured_search(
                parsed.filters_dict(), parsed.keywords, top_k, candidates
            )

        return [vector, bm25, structured]

    def _rule_clarify(self, parsed: ParsedQuery) -> str:
        """Template follow-up when no LLM is available — only ask for what's missing, never invent items."""
        missing = []
        if not parsed.category:
            missing.append("你想找哪一类东西")
        if parsed.max_price is None and parsed.min_price is None:
            missing.append("你的价格范围")
        if not missing:
            return "我没太理解你的需求，能再补充一点吗？比如品牌、成色，或者打算拿来做什么。"
        return (
            "为了缩小范围，能告诉我"
            + "和".join(missing)
            + "吗？比如「200 美元以内的二手自行车」。"
        )

    def _finish_debug(
        self, debug: dict[str, Any], item_ids: list[int], confidence: float, decision: str
    ) -> dict[str, Any]:
        debug["result"] = {"decision": decision, "item_ids": item_ids, "confidence": confidence}
        if settings.retrieval_debug_log:
            logger.info(
                "[retrieval] query=%r | filters=%s | candidates=%s | recall=%s | fused=%s | conf=%.4f | decision=%s",
                debug.get("query"),
                debug.get("rule_parse", {}).get("filters"),
                debug.get("hard_filter", {}).get("candidate_count"),
                {
                    name: [h["id"] for h in info.get("top5", [])]
                    for name, info in (debug.get("recall") or {}).items()
                },
                [f["id"] for f in (debug.get("fusion", {}) or {}).get("top8", [])],
                confidence,
                decision,
            )
        return debug


# ---------------- Reply templates (no LLM involved) ----------------
def render_reply(outcome: RetrievalOutcome, count: int) -> str:
    # Chinese has no plural agreement, so the English singular/plural branches
    # collapse — the count alone carries it.
    if outcome.decision == "hit":
        return f"帮你找到 {count} 件符合的商品："
    if outcome.decision == "structured_fallback":
        return f"没有完全匹配的，不过有 {count} 件符合你给的条件："
    if outcome.decision == "filter_fallback":
        return f"这是符合你条件的 {count} 件商品，按价格从低到高排列："
    if outcome.decision == "low_confidence":
        return f"不太确定是不是你要的，这 {count} 件比较接近："
    if outcome.decision == "clarify":
        return outcome.clarify_question or "能再说得具体一点吗？类别、预算或者成色，哪个都行。"
    return (
        "没有找到符合的商品。可以换个说法，或者放宽一个条件"
        "（比如去掉价格上限，或者把自提范围调大一点）。"
    )


def distance_for(user: User | None, lat: float, lng: float) -> float | None:
    if not user or user.lat is None or user.lng is None:
        return None
    return round(haversine_km(user.lat, user.lng, lat, lng), 2)

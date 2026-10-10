"""Query parsing (rules first) — step 1 of the retrieval pipeline.

Regex + dictionaries pull out hard filters, producing "structured filters +
residual semantic text". Only when rule parsing fails AND the query is clearly
vague does the retrieval layer call the LIGHT model (call site B).

Both languages are parsed. The product is Chinese as of 2026-09-15, but a
campus crowd types "under $300" as readily as "300以内", and the English
patterns cost nothing to keep: they run on the same text and consume the same
spans. Chinese patterns are tried first — "300以内" would otherwise be left for
the bare-number fallback, which reads it as a cap only by luck.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..config import BRAND_ALIAS_ZH, BRAND_DICT, CATEGORY_DICT, COLOR_DICT, CONDITION_DICT
from .text import normalize, words

# ---------- English number words ----------
_WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90,
}

# ---------- Chinese numerals ----------
_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}
#: Every character a Chinese numeral may contain, for the regex alternation.
_CN_NUM_CHARS = "".join(_CN_DIGIT) + "".join(_CN_UNIT)


def _cn_num_to_int(text: str) -> float | None:
    """Parse a Chinese numeral: 三百 -> 300, 一千二百五十 -> 1250, 两百五 -> 250.

    Handles the spoken shorthand where a trailing digit means "that many of the
    next unit down": 两百五 is 250, not 205, and 一千二 is 1200. Everyone says
    prices this way, and reading 两百五 as 205 would silently shift a budget.
    """
    if not text:
        return None
    total, section, last_unit = 0, 0, 0
    trailing_digit = 0
    for ch in text:
        if ch in _CN_DIGIT:
            trailing_digit = _CN_DIGIT[ch]
            section = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if unit == 10000:
                total = (total + section) * unit
                section = 0
            else:
                # bare 十 ("十块") means 10, not 0
                section = (section or 1) * unit
                total += section
                section = 0
            last_unit = unit
            trailing_digit = 0
        else:
            return None
    if trailing_digit and last_unit >= 100:
        # 两百五 -> 250: the dangling digit scales to one unit below the last.
        total += trailing_digit * (last_unit // 10)
    else:
        total += section
    return float(total) if total else None


def _words_to_int(text: str) -> float | None:
    """Handle "fifty", "two hundred", "a hundred and twenty" — enough for a demo."""
    tokens = re.findall(r"[a-z]+", text.lower())
    if not tokens:
        return None
    total, current = 0, 0
    seen_any = False
    for tok in tokens:
        if tok in ("a", "and"):
            continue
        if tok in _WORD_NUM:
            current += _WORD_NUM[tok]
            seen_any = True
        elif tok == "hundred":
            current = (current or 1) * 100
            seen_any = True
        elif tok == "thousand":
            total += (current or 1) * 1000
            current = 0
            seen_any = True
        else:
            return None
    value = total + current
    return float(value) if seen_any and value else None


def _parse_number(raw: str) -> float | None:
    raw = (raw or "").strip().lstrip("$").lstrip("￥").lstrip("¥").replace(",", "").strip()
    if not raw:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(k|千)?", raw, re.IGNORECASE)
    if m:
        value = float(m.group(1))
        return value * 1000 if m.group(2) else value
    if all(ch in _CN_NUM_CHARS for ch in raw):
        return _cn_num_to_int(raw)
    return _words_to_int(raw)


#: Number words, longest first so "sixteen" is tried before "six"
_NUM_WORDS = sorted([*_WORD_NUM, "hundred", "thousand"], key=len, reverse=True)
_WORD_TOKEN = r"(?:" + "|".join(_NUM_WORDS) + r")"
#: A number spelled out: "fifty", "two hundred", "a hundred and twenty".
#: It must *start* with a real number word (optionally preceded by "a"), so the
#: pattern can never match bare whitespace or filler such as "a bike" — which
#: is what used to make "between 100 and 200 dollars" silently parse as a
#: range of two empty strings.
NUM_WORD = rf"\b(?:a\s+)?{_WORD_TOKEN}\b(?:\s+(?:a|and|{_WORD_TOKEN})\b)*"
NUM_DIGIT = r"[$￥¥]?\s*\d+(?:,\d{3})*(?:\.\d+)?[k千]?"
#: A Chinese numeral: 三百, 一千二, 两百五
NUM_CN = rf"[{_CN_NUM_CHARS}]+"
#: A number: "$120", "120", "1.5k", 三百, or number words ("two hundred")
NUM = rf"({NUM_DIGIT}|{NUM_CN}|{NUM_WORD})"
#: Optional currency tail, both languages: "dollars", "bucks", 块, 元, 块钱
CUR = r"(?:\s*(?:dollars?|bucks?|usd|rmb|块钱|块|元|刀|软妹币))?"
#: Guard so price patterns never swallow a distance ("within 2 km", 2公里以内)
NODIST = r"(?!\s*(?:km|kilometers?|mi|miles?|公里|千米|米)\b)"
#: Same guard for Chinese, where \b does not fire between a digit and a hanzi.
NODIST_CN = r"(?!\s*(?:km|公里|千米|米))"


@dataclass
class ParsedQuery:
    #: hard filters
    max_price: float | None = None
    min_price: float | None = None
    category: str | None = None
    brand: str | None = None
    color: str | None = None
    condition: str | None = None
    max_distance_km: float | None = None
    #: residual semantic text (with the rule-consumed spans removed)
    semantic_text: str = ""
    #: keywords for structured recall
    keywords: list[str] = field(default_factory=list)
    #: content words (after dropping fillers like 东西 / 随便 / 便宜),
    #: used to judge whether the sentence actually says anything
    content_keywords: list[str] = field(default_factory=list)
    #: parse source: rule | llm | rule+llm
    source: str = "rule"
    #: did the rules extract anything meaningful
    rule_confident: bool = True

    def filters_dict(self) -> dict:
        return {
            k: v
            for k, v in asdict(self).items()
            if k in ("max_price", "min_price", "category", "brand", "color", "condition", "max_distance_km")
            and v is not None
        }


def _extract_price(text: str, parsed: ParsedQuery) -> str:
    """Extract price constraints; return the text with the matched spans removed."""

    def _first_valid(pattern: str, hay: str):
        """First match whose captured number(s) actually parse.

        A plain ``re.search`` is not enough: the first span a pattern matches
        can hold a number group that ``_parse_number`` rejects, and that dud
        match would shadow the real price later in the sentence.
        """
        for m in re.finditer(pattern, hay, re.IGNORECASE):
            values = [_parse_number(g) for g in m.groups()]
            if values and all(v is not None for v in values):
                return m, values
        return None, None

    def _cut(hay: str, m: re.Match) -> str:
        return hay[: m.start()] + " " + hay[m.end() :]

    # ---- ranges ----
    # Chinese: 100到200, 100-200块, 两百至五百
    # English: "between 100 and 200 dollars", "$100-$200", "from 100 to 200 bucks".
    # A bare "and" only separates a range when "between" is present; otherwise
    # "a hundred and twenty" would be split into a 20~100 range.
    patterns_range = [
        rf"{NUM}\s*(?:到|至|-|~|—)\s*{NUM}{NODIST_CN}\s*(?:块钱|块|元|刀)?(?:之间|左右|以内)?",
        rf"\bbetween\s+{NUM}\s*(?:-|~|to|and)\s*{NUM}{NODIST}{CUR}",
        rf"(?:\bfrom\s+)?{NUM}\s*(?:-|~|to)\s*{NUM}{NODIST}{CUR}",
    ]
    for pat in patterns_range:
        m, values = _first_valid(pat, text)
        if m:
            lo, hi = values
            parsed.min_price, parsed.max_price = min(lo, hi), max(lo, hi)
            return _cut(text, m)

    # ---- caps ----
    patterns_max = [
        # Chinese prefix forms: 不超过300, 最多300, 预算300, 低于300
        rf"(?:不超过|不高于|不多于|最多|最高|至多|低于|少于|便宜过|预算(?:是|为|在)?|价格(?:在|是)?)\s*{NUM}{NODIST_CN}\s*(?:块钱|块|元|刀)?",
        # Chinese suffix forms: 300以内, 300块以下, 300封顶, 300左右
        rf"{NUM}\s*(?:块钱|块|元|刀)?\s*(?:以内|以下|之内|之下|以里|封顶|左右|上下|出头)",
        rf"\b(?:under|below|less\s+than|no\s+more\s+than|not\s+more\s+than|at\s+most|max(?:imum)?(?:\s+of)?|up\s+to|within|cheaper\s+than)\s+{NUM}{NODIST}{CUR}",
        rf"{NUM}{NODIST}{CUR}\s*(?:or\s+less|or\s+under|or\s+below|max|tops|and\s+under|or\s+cheaper)\b",
        rf"\b(?:budget|price)\s*(?:is|of|:|around|about)?\s*{NUM}{NODIST}{CUR}",
        rf"\b(?:around|about|roughly|approximately)\s+{NUM}{NODIST}{CUR}",
    ]
    for pat in patterns_max:
        m, values = _first_valid(pat, text)
        if m:
            parsed.max_price = values[0]
            text = _cut(text, m)
            break

    # ---- floors ----
    patterns_min = [
        rf"(?:不低于|不少于|至少|起码|最少|高于|超过|大于|贵过)\s*{NUM}{NODIST_CN}\s*(?:块钱|块|元|刀)?",
        rf"{NUM}\s*(?:块钱|块|元|刀)?\s*(?:以上|往上|起步|起|开外)",
        rf"\b(?:over|above|beyond|more\s+than|greater\s+than|higher\s+than|at\s+least|no\s+less\s+than|not\s+less\s+than|min(?:imum)?(?:\s+of)?|starting\s+(?:at|from)|upwards\s+of|north\s+of)\s+{NUM}{NODIST}{CUR}",
        rf"{NUM}{NODIST}{CUR}\s*(?:or\s+more|or\s+higher|or\s+above|and\s+up|and\s+above|\+)",
    ]
    for pat in patterns_min:
        m, values = _first_valid(pat, text)
        if m:
            parsed.min_price = values[0]
            text = _cut(text, m)
            break

    # A bare "$120" / "120块" with no qualifier reads as a budget cap
    if parsed.max_price is None and parsed.min_price is None:
        for pat in (
            r"[$￥¥]\s*(\d+(?:,\d{3})*(?:\.\d+)?[k千]?)",
            rf"({NUM_DIGIT}|{NUM_CN})\s*(?:块钱|块|元|刀)",
        ):
            m = re.search(pat, text)
            if m:
                value = _parse_number(m.group(1))
                if value is not None:
                    parsed.max_price = value
                    return _cut(text, m)
    return text


_MILE_KM = 1.609344


def _extract_distance(text: str, parsed: ParsedQuery) -> str:
    # Chinese first: 2公里以内, 800米内, 1.5km
    m = re.search(
        rf"(?:方圆|半径|距离)?\s*{NUM}\s*(公里|千米|km|米|m)\s*(?:以内|之内|以下|内|范围内|左右)?",
        text,
        re.IGNORECASE,
    )
    if m:
        value = _parse_number(m.group(1))
        if value is not None:
            unit = m.group(2).lower()
            if unit in ("米", "m"):
                value /= 1000
            parsed.max_distance_km = round(value, 2)
            return text[: m.start()] + " " + text[m.end() :]

    m = re.search(
        rf"(?:within|inside|less\s+than|under|no\s+more\s+than)?\s*{NUM}\s*(kilometers?|miles?|mi)\b(?:\s*(?:away|radius|of\s+me|from\s+me))?",
        text,
        re.IGNORECASE,
    )
    if m:
        value = _parse_number(m.group(1))
        if value is not None:
            unit = m.group(2).lower()
            if unit.startswith("mi"):
                value *= _MILE_KM
            parsed.max_distance_km = round(value, 2)
            return text[: m.start()] + " " + text[m.end() :]

    if re.search(
        r"(附近|近一点|近点|离我近|不想跑远|走路能到|步行可达|走得到|楼下|同小区|同一个公寓)",
        text,
    ) or re.search(
        r"(nearby|close\s+by|close\s+to\s+me|near\s+me|walking\s+distance|don'?t\s+want\s+to\s+travel)",
        text,
        re.IGNORECASE,
    ):
        parsed.max_distance_km = parsed.max_distance_km or 1.5
    return text


#: Spelling/word aliases folded into canonical dictionary values. Single-character
#: colour words are the common case in Chinese ("黑耳机"), and the canonical
#: values are all two characters, so containment alone would miss them.
_COLOR_ALIASES = {
    "grey": "灰色", "gray": "灰色", "black": "黑色", "white": "白色",
    "silver": "银色", "red": "红色", "blue": "蓝色", "green": "绿色",
    "yellow": "黄色", "pink": "粉色", "purple": "紫色", "beige": "米色",
    "brown": "棕色", "gold": "金色", "orange": "橙色",
    "黑": "黑色", "白": "白色", "灰": "灰色", "银": "银色", "红": "红色",
    "蓝": "蓝色", "绿": "绿色", "黄": "黄色", "粉": "粉色", "紫": "紫色",
    "棕": "棕色", "咖": "棕色", "金": "金色", "橙": "橙色", "米白": "米色",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _word_match(term: str, norm: str, tokens: list[str] | None = None) -> bool:
    """Whole-word match for Latin terms, token-boundary match for Chinese ones.

    English needs the word boundary so 'bike' hits 'bikes' but 'art' does not
    hit 'smart'. Chinese has no such anchor — \\b between two hanzi never fires —
    and plain containment is too loose for a *hard* filter: the single character
    书 sits inside 看书, which filed "晚上看书用的灯" under 图书与教材 and then
    removed every lamp from the results. So a CJK term has to line up with
    jieba's own token boundaries; a multi-token term matches a contiguous run,
    so it works whichever way the segmenter split it.
    """
    t = normalize(term)
    if not t:
        return False
    if _CJK_RE.search(t):
        if tokens is None:
            return t in norm
        term_tokens = words(t) or [t]
        span = len(term_tokens)
        for i in range(len(tokens) - span + 1):
            if tokens[i : i + span] == term_tokens:
                return True
        return False
    return re.search(r"\b" + re.escape(t) + r"s?\b", norm) is not None


def _extract_dicts(text: str, parsed: ParsedQuery) -> None:
    norm = normalize(text)
    tokens = words(text)

    for category, terms in CATEGORY_DICT.items():
        if any(_word_match(w, norm, tokens) for w in terms) or _word_match(category, norm, tokens):
            parsed.category = category
            break

    for brand in BRAND_DICT:
        if _word_match(brand, norm, tokens):
            parsed.brand = brand
            break
    else:
        # Chinese brand names resolve to the Latin spelling stored on items.
        for alias, canonical in BRAND_ALIAS_ZH.items():
            if _word_match(alias, norm, tokens):
                parsed.brand = canonical
                break

    for color in COLOR_DICT:
        if _word_match(color, norm, tokens):
            parsed.color = color
            break
    else:
        for alias, canonical in _COLOR_ALIASES.items():
            if _word_match(alias, norm, tokens):
                parsed.color = canonical
                break

    # Condition words are matched by containment on purpose: they are written
    # into free text ("九成新的") far more often than they stand alone, and a
    # wrong condition only narrows the result set rather than emptying it.
    for condition, terms in CONDITION_DICT.items():
        if any(_word_match(w, norm) for w in terms):
            parsed.condition = condition
            break


#: Words that don't tell us what the user wants — not "content"
_MEANINGLESS = {
    # Chinese
    "东西", "物品", "玩意", "二手", "闲置", "便宜", "实惠", "划算", "好用",
    "不错", "推荐", "随便", "看看", "合适", "适合", "校园", "同学", "学校",
    "宿舍用", "急需", "出个", "求个", "谁有", "有人",
    # jieba keeps these colloquial verb phrases whole, so filtering the bare
    # verb in text.py's stopword list does not catch them.
    "想买个", "想买", "买个", "求购个", "用", "点", "有没有",
    # Single-character leftovers from a browsing question. jieba cuts
    # 有啥好东西吗 into 有啥 / 好 / 东西 / 吗: 东西 and 吗 are filtered, but a bare
    # 好 or 给 survived and counted as content — which made `rule_confident`
    # true and cost the clarify. They carry no product meaning here; condition
    # words like 成色很好 are extracted by CONDITION_DICT before this runs.
    "好", "给", "啥", "有啥", "弄", "整", "搞", "来点", "看有",
    # English
    "stuff", "thing", "things", "item", "items", "secondhand", "used", "cheap",
    "good", "nice", "great", "anything", "whatever", "recommend", "recommendation",
    "browse", "buy", "sell", "selling", "available", "suitable", "deal", "deals",
    "affordable", "quality", "campus", "someone",
}


def _content_words(text: str) -> list[str]:
    seen: set[str] = set()
    out = []
    for token in words(text):
        if token in _MEANINGLESS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def parse_query(text: str) -> ParsedQuery:
    parsed = ParsedQuery()
    residual = text or ""
    residual = _extract_price(residual, parsed)
    residual = _extract_distance(residual, parsed)
    _extract_dicts(residual, parsed)

    parsed.semantic_text = re.sub(r"\s+", " ", residual).strip() or (text or "").strip()
    parsed.content_keywords = _content_words(parsed.semantic_text)[:12]
    parsed.keywords = list(parsed.content_keywords)

    #: "Rules failed" test: nothing extracted and no content words -> let the LIGHT model look
    parsed.rule_confident = bool(parsed.filters_dict()) or bool(parsed.content_keywords)
    return parsed


def merge_llm_filters(parsed: ParsedQuery, llm_filters: dict, semantic_text: str | None) -> ParsedQuery:
    """Merge fields the LIGHT model filled in (rule-extracted fields win, never overwritten)."""
    mapping = {
        "max_price": float,
        "min_price": float,
        "category": str,
        "brand": str,
        "color": str,
        "condition": str,
        "max_distance_km": float,
    }
    for key, caster in mapping.items():
        value = llm_filters.get(key)
        if value in (None, "", "null"):
            continue
        if getattr(parsed, key) is None:
            try:
                setattr(parsed, key, caster(value))
            except (TypeError, ValueError):
                continue
    if semantic_text:
        parsed.semantic_text = semantic_text.strip()
        merged = parsed.content_keywords + _content_words(parsed.semantic_text)
        seen: set[str] = set()
        parsed.content_keywords = [t for t in merged if not (t in seen or seen.add(t))][:12]
        parsed.keywords = list(parsed.content_keywords)
    parsed.source = "rule+llm"
    return parsed

"""Chinese (CJK) tokenization and text utilities, shared by BM25 / structured matching.

The corpus became Chinese on 2026-09-15. Chinese has no spaces, so the English
pipeline this replaces — split on whitespace, strip plurals — returns the whole
sentence as one token and BM25 degenerates to exact-string matching. Words come
from jieba instead.

The text is mixed in practice: brand names and model numbers stay in Latin
script ("IKEA 书桌", "900W 微波炉"), so every function splits the input into CJK
runs and ASCII runs and applies the right treatment to each. The ASCII half is
the old English path, kept intact rather than rewritten, because it still has
to handle exactly what it handled before.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: One ASCII word (optionally hyphenated/apostrophised) or one number.
_ASCII_TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?")
#: A run of CJK characters.
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
#: Anything CJK, used for per-character tests.
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

_ASCII_STOPWORDS = {
    "a", "an", "the", "i", "im", "i'm", "me", "my", "we", "you", "your", "it",
    "is", "are", "was", "be", "do", "does", "did", "have", "has", "had",
    "want", "need", "looking", "look", "for", "to", "of", "in", "on", "at",
    "with", "and", "or", "any", "some", "that", "this", "these", "those",
    "please", "help", "find", "get", "got", "there", "here", "also", "just",
    "would", "like", "can", "could", "anyone", "someone", "something",
}

#: Chinese function words and shopping filler. Kept deliberately short: an
#: aggressive list eats the words that carry the need ("要" in "要考研的书" is
#: filler, but "包" is a bag). Anything ambiguous stays in.
_CJK_STOPWORDS = {
    "的", "了", "呢", "吗", "吧", "啊", "呀", "嘛", "哦", "噢", "喔",
    "我", "你", "您", "他", "她", "它", "我们", "你们", "他们", "咱", "咱们",
    "是", "在", "有", "和", "跟", "与", "或", "还是", "以及", "还有",
    "想", "想要", "要", "需要", "求", "求购", "找", "寻找", "看看", "看下",
    "帮", "帮忙", "帮我", "请", "请问", "麻烦", "谢谢",
    "一个", "一下", "一些", "个", "些", "这", "那", "这个", "那个", "这些", "那些",
    "什么", "啥", "怎么", "怎样", "如何", "哪", "哪个", "哪些", "谁",
    "可以", "能", "能不能", "可不可以", "行不行", "有没有", "有木有",
    "就", "都", "也", "还", "再", "很", "太", "挺", "比较", "稍微",
    "现在", "目前", "最近", "然后", "另外", "而且", "但是", "不过",
    "出", "出手", "转", "转让", "出售", "卖", "买", "购买", "收",
}

#: Function words that end a noun compound — used by match_position() to decide
#: whether the matched token is the head of the name or just a modifier.
_ASCII_BOUNDARY_WORDS = {
    "with", "for", "in", "and", "or", "plus", "w", "of", "from", "by",
}

#: Chinese equivalents: particles and conjunctions that close a noun compound.
#: "自行车 和 头盔" — the 和 ends the compound, so 自行车 is a head, not a modifier.
_CJK_BOUNDARY_WORDS = {
    "和", "与", "跟", "及", "或", "还有", "加", "配", "带", "送", "的",
    "包邮", "自提", "面交", "出", "卖", "转",
}

_BOUNDARY_WORDS = _ASCII_BOUNDARY_WORDS | _CJK_BOUNDARY_WORDS


# ------------------------------------------------------------
# jieba, loaded lazily and degraded gracefully
# ------------------------------------------------------------
_jieba_mod = None
_jieba_ready = False


def _get_jieba():
    """Return the jieba module, or None if it isn't installed.

    Loaded on first use, not at import: jieba builds its prefix dictionary on
    the first cut (~1s), and paying that inside a web worker's first request is
    better than paying it in every script that merely imports this module.

    The project vocabulary is registered as user words. Without it jieba splits
    domain compounds by general-corpus frequency — 羽绒服 survives, but
    电饭煲 comes out as 电饭 + 煲 and 尤克里里 shatters — and a term that never
    appears whole can never be matched whole.
    """
    global _jieba_mod, _jieba_ready
    if _jieba_ready:
        return _jieba_mod
    _jieba_ready = True
    try:
        import jieba  # deferred import; only fails when the dep is missing

        jieba.setLogLevel(logging.WARNING)
        for word in _domain_vocabulary():
            # freq high enough to beat the general dictionary's own segmentation
            jieba.add_word(word, freq=10000)
        _jieba_mod = jieba
        logger.info("[text] jieba loaded with %d domain words", len(_domain_vocabulary()))
    except Exception as exc:  # noqa: BLE001
        _jieba_mod = None
        logger.warning(
            "[text] jieba unavailable (%s); falling back to character bigrams. "
            "BM25 and structured matching still work, with coarser word "
            "boundaries — install jieba and restart to recover.",
            exc,
        )
    return _jieba_mod


_domain_words: tuple[str, ...] | None = None


def _domain_vocabulary() -> tuple[str, ...]:
    """Chinese terms from the business dictionaries, longest first.

    Imported lazily to keep this module free of an import-time dependency on
    config (which several scripts load before the settings file exists).
    """
    global _domain_words
    if _domain_words is not None:
        return _domain_words
    words_set: set[str] = set()
    try:
        from ..config import (
            BRAND_ALIAS_ZH,
            CATEGORY_DICT,
            COLOR_DICT,
            CONDITION_DICT,
        )

        for key, synonyms in CATEGORY_DICT.items():
            words_set.add(key)
            words_set.update(synonyms)
        for key, synonyms in CONDITION_DICT.items():
            words_set.add(key)
            words_set.update(synonyms)
        words_set.update(COLOR_DICT)
        words_set.update(BRAND_ALIAS_ZH.keys())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[text] could not load domain vocabulary (%s)", exc)
    # Only multi-character CJK terms are worth registering: single characters
    # are already in jieba's dictionary, and ASCII is not jieba's business.
    _domain_words = tuple(
        sorted((w for w in words_set if len(w) > 1 and _CJK_CHAR_RE.search(w)), key=len, reverse=True)
    )
    return _domain_words


def _cut_cjk(run: str) -> list[str]:
    """Segment one run of Chinese characters into words."""
    jieba = _get_jieba()
    if jieba is None:
        # Character bigrams: coarse, but every multi-character term still has a
        # chance of matching, and single characters keep unigram recall alive.
        if len(run) == 1:
            return [run]
        return [run[i : i + 2] for i in range(len(run) - 1)]
    return [w for w in jieba.lcut(run) if w.strip()]


def _stem(token: str) -> str:
    """Poor-man's singularizer for the ASCII half of the text.

    bikes -> bike, boxes -> box, batteries -> battery. Deliberately conservative:
    short tokens and -ss words are left alone (glass, chess).
    """
    if len(token) <= 3 or not token.isalpha():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _segment(text: str, drop_stopwords: bool) -> list[str]:
    """Split mixed Chinese/ASCII text into tokens, in source order.

    CJK runs go through jieba, ASCII runs through the word regex + singularizer.
    Order is preserved across the two, because match_position() reads the
    sequence to find compound heads.
    """
    if not text:
        return []
    lowered = text.lower()
    out: list[str] = []
    pos = 0
    for m in _CJK_RUN_RE.finditer(lowered):
        if m.start() > pos:
            out.extend(
                _stem(t)
                for t in _ASCII_TOKEN_RE.findall(lowered[pos : m.start()])
                if not (drop_stopwords and t in _ASCII_STOPWORDS)
            )
        out.extend(
            w
            for w in _cut_cjk(m.group())
            if not (drop_stopwords and w in _CJK_STOPWORDS)
        )
        pos = m.end()
    if pos < len(lowered):
        out.extend(
            _stem(t)
            for t in _ASCII_TOKEN_RE.findall(lowered[pos:])
            if not (drop_stopwords and t in _ASCII_STOPWORDS)
        )
    return out


def tokenize(text: str) -> list[str]:
    """Return the token list used for BM25: words + adjacent-word bigrams.

    Bigrams ("机械_键盘") reward phrase matches without hurting single-word
    queries — the BM25 score floors are calibrated with them in. Chinese needs
    them more than English did: jieba splits 机械键盘 into two common words, and
    without the bigram a query for it scores no better than one for any keyboard.
    """
    if not text:
        return []
    toks = _segment(text, drop_stopwords=True)
    bigrams = [toks[i] + "_" + toks[i + 1] for i in range(len(toks) - 1)]
    return toks + bigrams


def words(text: str) -> list[str]:
    """Return only content words: stopwords removed, ASCII plurals folded.

    Difference from `tokenize()`: no bigrams. Used for structured matching and
    for judging "does this sentence actually say anything".
    """
    return _segment(text, drop_stopwords=True)


def raw_tokens(text: str) -> list[str]:
    """Plain word split with no stopword filtering — the head-word test needs
    the untouched sequence to tell 自行车锁 (bike lock) from 自行车 26 寸."""
    return _segment(text, drop_stopwords=False)


#: Penalty when the query word is only a modifier in the name (自行车 in 自行车锁)
MODIFIER_PENALTY = 0.45
#: Penalty when the query word only matches as a substring (卡 in 卡通)
SUBSTRING_PENALTY = 0.6


def match_position(name_tokens: list[str], name_norm: str, keyword: str) -> str:
    """Decide whether `keyword` is the head noun of the item name or a modifier.

    Chinese noun compounds are head-final, same as English ones: in 自行车锁 the
    head is 锁 and 自行车 merely modifies it, so a search for 自行车 should rank
    山地自行车 (head hit) above 自行车锁 (modifier hit). The test looks at the
    next token: if it is another content word, the two form a larger compound
    and the match is a modifier; if it is a number, a boundary word (和, with,
    for...) or the end of the name, the keyword is the head.

    Returns head | modifier | substring | none
    """
    kw_tokens = _segment(keyword, drop_stopwords=False)
    if not kw_tokens:
        return "none"
    # A multi-token keyword (机械 键盘) is matched as a contiguous run; the
    # head test then looks at what follows the run's last token.
    span = len(kw_tokens)

    found_modifier = False
    lowered = [t.lower() for t in name_tokens]
    for i in range(len(lowered) - span + 1):
        if lowered[i : i + span] != kw_tokens:
            continue
        nxt = lowered[i + span] if i + span < len(lowered) else ""
        if nxt and nxt not in _BOUNDARY_WORDS and not _is_numeric(nxt):
            found_modifier = True  # another content word follows -> modifier; keep scanning
            continue
        return "head"

    # Compounds jieba never split. 机械键盘 is one token (it is in the domain
    # vocabulary), so the token-run test above cannot see the 键盘 inside it —
    # yet 键盘 *is* the head of that compound, and a buyer searching 键盘 should
    # get it ahead of 键盘保护膜. Chinese compounds are head-final, so position
    # inside the token decides: a keyword at the end is the head, one at the
    # start or in the middle is a modifier. Only for CJK keywords; the ASCII
    # half already has whitespace to segment on and "cardigan" must not count
    # as a head hit for "card".
    kw_joined = "".join(kw_tokens)
    if _CJK_CHAR_RE.search(kw_joined):
        for i, token in enumerate(lowered):
            if token == kw_joined or kw_joined not in token:
                continue
            if token.endswith(kw_joined):
                nxt = lowered[i + 1] if i + 1 < len(lowered) else ""
                if not nxt or nxt in _BOUNDARY_WORDS or _is_numeric(nxt):
                    return "head"
            found_modifier = True

    if found_modifier:
        return "modifier"
    # Substring fallback. English anchors at a word start ("bike" in "bikes",
    # but not "art" in "smart"); Chinese has no word boundary to anchor on, so a
    # plain containment test is the only option — which is exactly why a
    # substring hit is scored lower than a real one.
    kw_norm = normalize(keyword)
    if not kw_norm:
        return "none"
    if _CJK_CHAR_RE.search(kw_norm):
        if kw_norm in name_norm:
            return "substring"
    elif re.search(r"\b" + re.escape(kw_norm), name_norm):
        return "substring"
    return "none"


def _is_numeric(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def normalize(text: str) -> str:
    """Lowercase and collapse whitespace.

    Whitespace is collapsed rather than stripped: the text is mixed, and
    removing every space would fuse "power bank" into "powerbank" and lose the
    ASCII word boundaries that match_position() still relies on. Chinese
    matching does not need boundaries, so it is unaffected either way.
    """
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def clip_sentences(text: str, max_sentences: int = 2) -> str:
    """Safety clip: keep summary ≤ N sentences (re-clip when the LLM overshoots).

    Joined without a separator when the split was on Chinese punctuation — 。
    already ends the sentence, and adding a space after it reads as a typo.
    """
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?。！？])\s*", text.strip())
    parts = [p for p in parts if p.strip()]
    if not parts:
        return text.strip()
    kept = parts[:max_sentences]
    joiner = "" if _CJK_CHAR_RE.search(text) else " "
    return joiner.join(kept).strip()


# Same family of patterns as llm_schemas._PII_PATTERNS, but here we **scrub**
# instead of raising: tool output text comes from buyers' own demand text, and
# one buyer typing a phone number must not fail the whole query.
_PII_SCRUB_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # email
    # US phone number (separator or parentheses required, so IDs don't match)
    re.compile(r"(?<!\d)(?:\+?1[\s.-])?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]?\d{4}(?!\d)"),
    # Mainland mobile number, with or without the +86 prefix. 11 digits starting
    # with 1 — bare, because that is how people type it.
    re.compile(r"(?<!\d)(?:\+?86[\s.-]?)?1[3-9]\d{9}(?!\d)"),
    # social / messaging handles, Latin and Chinese spellings alike
    re.compile(
        r"\b(?:wechat|whatsapp|snapchat|instagram|insta|telegram|discord|venmo|ig|qq)\b\s*[:#]?\s*@?[\w.-]{4,}",
        re.IGNORECASE,
    ),
    re.compile(r"(?:微信|威信|vx|薇信|加我|手机号|电话|联系方式|扣扣)\s*[:：#]?\s*@?[\w.-]{4,}"),
    re.compile(r"(?<!\d)\d{1,3}\.\d{4,}(?!\d)"),  # high-precision decimal (likely a coordinate)
]


def scrub_pii(text: str) -> str:
    for pattern in _PII_SCRUB_PATTERNS:
        text = pattern.sub("", text or "")
    return text.strip()

"""The listing-rule book: seeding, retrieval, and the vector index behind it.

Rules live in the database (`listing_rules`) rather than inside a prompt for
two reasons: policy changes without a deploy, and a verdict can name the exact
rule it hit — "not allowed" that cannot be traced to a rule is not something a
seller can act on or appeal.

**Retrieval is not allowed to decide what is prohibited.** Semantic retrieval
has recall, and recall below 100% on a safety check means a prohibited item
gets listed because its wording happened not to match. So the rule set handed
to the reviewer is:

    every active BLOCK rule  +  the top-k semantically closest WARN rules

Retrieval does the job it is actually good at — picking which *advice* is
relevant to this particular item out of a long tail of quality rules — while
the hard prohibitions are always present regardless of phrasing. If the block
list ever grows past what fits comfortably in a prompt, the answer is a
cheaper pre-filter, not sampling it.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import ListingRule, RuleSeverity
from ..embedding import get_embedding_provider
from ..vector_store import build_vector_store

logger = logging.getLogger(__name__)

# ============================================================
# Seed rule book
# ============================================================
# Written as instructions to a reviewer, not as user-facing copy: each says
# what to look for and what the boundary is, because a vague rule produces a
# vague verdict.
SEED_RULES: list[tuple[str, str, str, str]] = [
    # ---------------- Hard prohibitions ----------------
    ("BAN-WEAPONS", RuleSeverity.BLOCK, "武器与自卫器械",
     "枪支、仿真枪或玩具枪、弹药、作为武器出售的刀具、电击器、甩棍和防狼喷雾，一律不得发布。"
     "普通厨具刀、工具刀和体育器材按其本来用途描述时可以发布。"),
    ("BAN-DRUGS", RuleSeverity.BLOCK, "毒品与吸食器具",
     "任何种类的娱乐性毒品，以及主要用途是吸食它们的器具——包括电子烟、雾化器、水烟和烟斗"
     "——一律不得发布。"),
    ("BAN-ALCOHOL-TOBACCO", RuleSeverity.BLOCK, "酒类与烟草",
     "酒精饮料和烟草制品不得发布。空的收藏酒瓶、未开封的玻璃器皿，在明确不是出售可饮用内容物的"
     "前提下可以发布。"),
    ("BAN-RX", RuleSeverity.BLOCK, "药品与医疗宣称",
     "处方药、非处方药，以及宣称能治疗某种疾病的保健品，不得发布。"
     "未开封、且不带任何医疗宣称的急救用品可以发布。"),
    ("BAN-ADULT", RuleSeverity.BLOCK, "色情内容",
     "色情制品、性服务和成人用品不得发布。含有裸露或性内容的照片一律拦截，与所售物品是什么无关。"),
    ("BAN-VIOLENCE", RuleSeverity.BLOCK, "暴力或令人不适的画面",
     "描绘血腥暴力、伤害、虐待动物或自残的照片与文字，不得发布，与所售物品是什么无关。"),
    ("BAN-HATE", RuleSeverity.BLOCK, "仇恨内容",
     "带有仇恨符号的物品，或基于种族、民族、宗教、性别、性取向或身心障碍攻击他人的文字，"
     "不得发布。"),
    ("BAN-LIVE-ANIMALS", RuleSeverity.BLOCK, "活体动物",
     "任何活体动物，包括宠物、鱼类和昆虫，不得发布。宠物用品、空的鱼缸或笼子可以发布。"),
    ("BAN-STOLEN", RuleSeverity.BLOCK, "非卖家所有的物品",
     "卖家并不拥有、或无权出售的物品——包括学校、图书馆和宿舍的财产——不得发布。"),
    ("BAN-COUNTERFEIT", RuleSeverity.BLOCK, "仿冒品",
     "以正品品牌商品名义出售的仿制品或山寨货，不得发布。不标品牌的相似款，只要商品信息里"
     "没有声称是某个品牌，可以发布。"),
    ("BAN-ACADEMIC-FRAUD", RuleSeverity.BLOCK, "学术不诚信",
     "写好的作业、试卷或答案，代写论文、代考服务，以及付费课程材料的访问权限，不得发布。"
     "二手教材和本人手写的笔记可以发布。"),
    ("BAN-ID-DOCS", RuleSeverity.BLOCK, "身份与门禁类物品",
     "学生证、实名绑定的交通卡、钥匙、门禁卡和楼宇门禁设备，不得发布。"),
    ("BAN-ACCOUNTS", RuleSeverity.BLOCK, "账号、授权与订阅",
     "网络账号、流媒体或软件订阅、激活码，以及可转让的授权许可，不得发布。"),
    ("BAN-HAZMAT", RuleSeverity.BLOCK, "危险品",
     "实验室或工业化学品、烟花爆竹、加压燃料罐，以及破损、鼓包或被刺穿的锂电池，不得发布。"),
    ("BAN-HOMEMADE-FOOD", RuleSeverity.BLOCK, "自制食品与生鲜",
     "自制食品、生鲜食材和已开封的食品包装，不得发布。密封且在保质期内的包装食品可以发布。"),
    ("BAN-RECALLED-SAFETY", RuleSeverity.BLOCK, "召回品与一次性防护装备",
     "被召回的产品，以及受过一次冲击后即失效的防护装备——自行车和摩托车头盔、儿童安全座椅"
     "——不得作为二手商品发布。"),
    # ---------------- Advisory ----------------
    ("WARN-CONTACT", RuleSeverity.WARN, "商品信息里不要留联系方式",
     "手机号、邮箱、社交账号、宿舍或房间号，不应出现在标题、摘要或描述里。"
     "买卖双方在预订成功之后才交换联系方式。"),
    ("WARN-PRICE-IN-TEXT", RuleSeverity.WARN, "价格写在价格字段里",
     "描述里不要重复标价、不要写折扣、也不要提其他平台的价格；价格字段是唯一准确的来源，"
     "写在文字里的副本迟早会过时。"),
    ("WARN-HYPE", RuleSeverity.WARN, "不要营销话术",
     "避免最高级和制造紧迫感的说法——"
     "「超值」「捡漏」「今天必出」。描述这件东西本身，而不是这笔买卖。"),
    ("WARN-WARRANTY", RuleSeverity.WARN, "不要做出保证",
     "不要承诺保修、正品、使用寿命或退款。改为陈述关于成色的可观察事实。"),
    ("WARN-VAGUE", RuleSeverity.WARN, "描述过于单薄",
     "描述应当让买家不用私信就能决定：这是什么、什么成色、有没有明显的磨损、缺件或损坏。"
     "只写一句「成色良好」是不够的。"),
    ("WARN-CONDITION-UNCLEAR", RuleSeverity.WARN, "没有说明成色",
     "商品信息应当说明用了多久，并主动披露瑕疵——划痕、磕碰、缺件、电池衰减。"),
    ("WARN-BRAND-UNVERIFIED", RuleSeverity.WARN, "品牌或型号未经确认",
     "只写照片上可见、或卖家自己声明过的品牌与型号。不要凭外观推断一个出来。"),
    ("WARN-BUNDLE", RuleSeverity.WARN, "一条商品只放一件东西",
     "把不相关的东西打包成一条商品，既不好搜也不好定价。建议拆开发布；成套的东西可以放一起。"),
    ("WARN-PHOTO-MISMATCH", RuleSeverity.WARN, "照片与文字对不上",
     "当描述里提到的配件、部件或成色与照片相矛盾、或照片里根本看不到时，标记出来。"),
    ("WARN-PHOTO-QUALITY", RuleSeverity.WARN, "照片无法使用",
     "标记那些太模糊、太暗或裁切过度以致看不清商品的照片，以及用官方图、产品目录图代替实物的照片。"),
    ("WARN-PHOTO-PRIVACY", RuleSeverity.WARN, "照片里有私人信息",
     "标记那些拍到了证件、住址、含个人数据的屏幕，或者其他人面部的照片。"),
    ("WARN-SHIPPING", RuleSeverity.WARN, "仅限当面自提",
     "本平台只支持在校园自提点当面交易。商品信息里不应承诺快递、配送或邮寄。"),
]


def seed_rules(db: Session) -> int:
    """Insert any missing seed rules. Idempotent, keyed by `code`.

    Existing rows are left alone on purpose: once someone edits a rule in the
    database, re-running startup must not quietly overwrite their wording.
    """
    have = set(db.scalars(select(ListingRule.code)))
    added = 0
    for code, severity, title, body in SEED_RULES:
        if code in have:
            continue
        db.add(ListingRule(code=code, severity=severity, title=title, body=body, active=True))
        added += 1
    if added:
        db.commit()
        logger.info("[rules] seeded %d listing rules", added)
    return added


def active_rules(db: Session) -> list[ListingRule]:
    return list(
        db.scalars(select(ListingRule).where(ListingRule.active.is_(True)).order_by(ListingRule.code))
    )


# ============================================================
# Vector index over the advisory rules
# ============================================================
_store = None
_lock = threading.Lock()


def _get_store():
    global _store
    with _lock:
        if _store is None:
            _store = build_vector_store(settings.rules_vector_collection)
        return _store


def rebuild_index(db: Session) -> dict:
    """Re-embed every active rule. Cheap (tens of rows) — always a full rebuild,
    so the index cannot drift from the table."""
    rules = active_rules(db)
    store = _get_store()
    store.reset()
    if rules:
        provider = get_embedding_provider()
        vectors = provider.embed_documents([f"{r.title}. {r.body}" for r in rules])
        store.upsert(
            ids=[str(r.id) for r in rules],
            embeddings=vectors,
            metadatas=[{"item_id": r.id, "code": r.code, "severity": r.severity} for r in rules],
        )
    stats = {"rules": len(rules), "vector_count": store.count()}
    logger.info("[rules] index rebuilt: %s", stats)
    return stats


def reset_index() -> None:
    """Test hook: drop the cached store so the next call rebuilds it."""
    global _store
    with _lock:
        _store = None


# ============================================================
# 不进上架流程的那两条规则
# ============================================================
#: 这两条判的不是**这件东西能不能卖**，而是**商品信息写得够不够**。在本产品里，
#: 后一个问题有一个**不靠对话的答案**：草稿卡下面那张表。
#:
#: 上架的完整路径是「传一张照片 → 系统把认得出来的都填进表 → 卖家把剩下的选完
#: （成色、类别、价格、自提点）→ 点发布」。中间没有任何一步要求他在对话框里再把
#: 商品描述打一遍——他真要补充，直接改表里那个「描述」框就是了。
#:
#: 所以这两条规则放在这条链路上只会产生一种效果：把一个**表单里两秒钟就能选完的
#: 事**，伪装成一条他必须在聊天里满足的**发布条件**。而且它们判的还是错的那份
#: 文本——合规检查跑在抽取之前，那一刻手上只有卖家打的字，描述还不存在，于是
#: 「描述过于单薄」必然命中，卖家却在下面那张表里看着一段写好的描述。
#:
#: 规则本身留在规则书里（别处还用得到，比如人工审核），只是上架 agent 不再把它们
#: 发给卖家。要恢复成提示，把这个集合清空即可。
SUPPRESSED_IN_PUBLISHING: frozenset[str] = frozenset(
    {"WARN-VAGUE", "WARN-CONDITION-UNCLEAR"}
)


def publishable_findings(findings: list) -> list:
    """上架流程要展示的那几条。过滤掉 `SUPPRESSED_IN_PUBLISHING`。

    过滤放在**发给卖家之前**而不是规则检索时：检索那一层不知道自己是在为哪条
    链路服务，而这两条规则在别的链路上仍然成立。
    """
    return [f for f in findings if getattr(f, "code", None) not in SUPPRESSED_IN_PUBLISHING]


def retrieve_rules(db: Session, text: str, top_k: int | None = None) -> list[ListingRule]:
    """The rule set for one submission: all blocks, plus the closest advisories.

    See the module docstring for why the block half is not retrieved. A
    retrieval failure degrades to "all rules" rather than "fewer rules" — the
    prompt gets longer, never laxer.
    """
    top_k = top_k or settings.publishing_rule_top_k
    rules = active_rules(db)
    by_id = {r.id: r for r in rules}
    blocks = [r for r in rules if r.severity == RuleSeverity.BLOCK]
    warns = [r for r in rules if r.severity != RuleSeverity.BLOCK]

    if not warns or not text.strip():
        return blocks + warns

    try:
        vector = get_embedding_provider().embed_query(text)
        hits = _get_store().query(
            vector, top_k=top_k, where={"severity": RuleSeverity.WARN}
        )
        picked = [by_id[int(rid)] for rid, _ in hits if int(rid) in by_id]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[rules] retrieval failed (%s); falling back to the full rule book", exc)
        return blocks + warns

    if not picked:
        return blocks + warns
    return blocks + picked


def render_rules(rules: list[ListingRule]) -> str:
    """Rule book as the reviewer sees it. The code is included so the verdict
    can cite it back and the citation can be validated."""
    return "\n".join(
        f"- [{r.code}] ({r.severity}) {r.title}: {r.body}" for r in rules
    )

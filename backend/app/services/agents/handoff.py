"""会话级任务状态：让一次 agent 交接不清空卖家原本要做的事。

这个模块修的是本总类里最大的一块失败（归因一，14 条 / 43.8%）。典型剧本只有三步：

    卖家：帮我卖小白板
    系统：这件还没上架，先传一下
    卖家：上传好了
    系统：你想做什么？          ← 原始诉求在这一跳蒸发了

蒸发的不是对话记忆——`turns` 一直在——而是**待办**。「帮我卖小白板」在系统里
展开成三件事（需求分析 / 定价分析 / 找买家）加一个实体（小白板），而这两样东西
以前只存在于路由器那一次调用的返回值里：交给 publishing 之后它们没有落点，下一
轮重新路由时，路由器看到的只有「上传好了」四个字，于是只能重新澄清。

所以这里加的不是「更好的提示词」，而是一个**状态对象**：

* `original_intent` —— 卖家最初那句话，原样保存。回复里要把它复述出来，卖家
  才能确认接力接对了；
* `pending_tasks` —— 展开后的分析队列（当前恒为一项 `scenario`，写成列表是因为
  队列的语义从一开始就该是列表）；
* `pending_items` —— 待处理实体列表。**列表，不是一个值**：3.2.2 的 5 条全部
  是多商品场景下只承接了最后一件，而根因就是交接载体里只放得下一个实体；
* `handoff_at` —— 挂起的时刻，接力时用来找「这之后新上架的商品」。

**绑定靠数据库，不靠记名字。** 接力那一轮真正要回答的是「卖家刚才传上来的是哪
几件」，而这个问题有一个确定的答案：`handoff_at` 之后创建的在售商品。用它而不
是用卖家原话去做名称匹配，是因为卖家嘴里的「小白板」和他填进表单的「白色磁性
写字板 60x90」经常不是一个词——按名字找会找不到，按时间找一定找得到。名字匹配
保留为兜底，处理「挂起前就已经传好了」这种顺序颠倒的情况。

**接力不重新调路由器。** 一条「上传好了」里没有任何可路由的信息，让模型去读它
只会得到一次掷硬币。任务队列在，实体在，场景在——这一轮该做什么是**算得出来**的。

**挂账看的是「这一轮办不办得成」，不是「路由器打算去哪」。** 上面那个剧本是路由器
自己判定要先上架的版本，而实测里更常见的是另一个形状：卖家说「分析一下水杯的需求
和价格」，路由去了 `sales_agent`，由顾问在回复正文里写一句「我没找到你在售的水杯，
请先上架」。对卖家来说这两件事一模一样，但在系统里，后者的路由结果是
`sales_agent`，于是这个模块一次都没被叫醒。`item_not_found`（见 `HANDOFF_ROUTES`）
就是把那一半补回来：能不能办成是后端算出来的事实，不是模型写出来的一段话。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models import ItemStatus
from ...repositories import ItemRepository
from . import intent_plan
from .router import MISSING_ITEM_ROUTE

logger = logging.getLogger(__name__)

#: 挂起的任务最多保留这么多轮。超过之后卖家显然已经在做别的事，再把一个三轮前
#: 的计划接上来，接的就不是他现在要的东西了。
MAX_HANDOFF_AGE_TURNS = 8

#: 一次交接最多记这么多件待处理商品。
MAX_PENDING_ITEMS = 5

#: 泛卖出意图默认展开成的任务。和 `router.Scenario` 里的 `sell_through` 是同一个
#: 东西——交接前后卖家要的是同一件事，不该因为中间隔了一次上架就换一套计划。
DEFAULT_TASK = "sell_through"

#: 哪些路由结果意味着「卖家的真实诉求还没被满足」。
#:
#: `publishing_agent` 是显然的那个：卖家要卖，系统先让他去上架。
#:
#: `clarify` 是实测补上的。「这两件都帮我卖了」在库里没有对应商品时，路由器会问
#: 「是哪两件」——这个提问本身没有错，但它和 publishing 一样把原始诉求悬在了半空：
#: 卖家转头去上架，回来说「都弄好了」，而那一跳没有留下任何东西。挂起的条件是
#: **意图明确、只是实体不明**（`broad_sell_intent` 命中），不是「凡澄清必挂起」。
#:
#: `item_not_found` 是第三个，也是实测里**最常走的那一个**。它和 publishing 的
#: 区别只在于谁开的口：路由器判定要先上架时走 publishing，而卖家点名了一件库里
#: 没有的东西时，以前是 sales_agent 自己在回复正文里说「请先上架」——同一件事，
#: 同一个被悬空的诉求，但因为路由结果是 `sales_agent`，这个钩子一次都没响过。
#: 挂账的触发点因此从「路由意图」下移到了「这一轮办不办得成」。
#:
#: `sales_agent` 本身仍然不在这里：那是已经在办了，没有什么被打断。
HANDOFF_ROUTES: frozenset[str] = frozenset(
    {"publishing_agent", "clarify", MISSING_ITEM_ROUTE}
)


#: 「以后再卖」被打断时挂起的任务。和销售类任务并列，因为它回来之后要去的是
#: `defer_sale` 而不是 `sales_agent`——接力恢复的是**同一个**诉求，不是一个近似的。
DEFER_TASK = "defer_sale"


def _sales_intent(scenario: str, hints: dict[str, Any]) -> str:
    """这条被送去上架的消息，背后有没有一个销售任务。

    两个来源：路由器读出来的 `scenario`（卖家点名了某种分析），以及确定性的
    `broad_sell_intent`（卖家只说了「帮我卖」）。都没有的话，这就是一次纯粹的
    上架请求——「帮我把这个挂出去」后面没有藏着任何分析，不该凭空给他排队。
    """
    # 「以后再卖」优先：它比泛卖出意图更具体，回来之后该去的地方也不一样。
    if hints.get("defer_sale_intent"):
        return DEFER_TASK
    if scenario in ("price", "demand", "both", "find_buyers", "sell_through"):
        return scenario
    if hints.get("broad_sell_intent"):
        return DEFAULT_TASK
    return ""


def capture(
    *,
    route: str,
    scenario: str,
    slots: dict[str, Any],
    hints: dict[str, Any],
    message: str,
    turn_index: int,
    item_refs: list[str] | None = None,
) -> dict[str, Any]:
    """这一轮办不成卖家要的事时，把他原本要办的事记下来。

    返回一个状态补丁（可能为空）。不改路由——这一轮该去上架仍然去上架，这里只是
    在旁边留了一张条子。

    `item_refs` 是这一轮涉及的**全部**实体。给了就用它，因为 `slots` 只放得下
    一个 `item_ref`，而「这两件都帮我卖了」的正确答案从一开始就是一个列表。
    """
    if route not in HANDOFF_ROUTES:
        return {}
    task = _sales_intent(scenario, hints)
    if not task and route == MISSING_ITEM_ROUTE:
        # 这条路只有一个成因：卖家点名要一件不在库里的东西。诉求一定存在——他
        # 刚刚说出口了——哪怕路由器没能把它归进某一种具体的分析。这里兜底成
        # `sell_through`，因为「帮我把它弄出去」是这句话最小的、也一定成立的读法。
        task = DEFAULT_TASK
    if not task:
        return {}
    if route == "clarify" and not hints.get("broad_sell_intent"):
        # 一次普通的澄清（「你是想看需求还是看价格」）不该在后台排一个队列。
        # 只有卖家已经明说了要卖、缺的仅仅是哪一件时，才值得记账。
        return {}

    items: list[dict[str, Any]] = []
    names = [str(n).strip() for n in (item_refs or []) if str(n).strip()]
    if not names:
        ref = str(slots.get("item_ref") or "").strip()
        names = [ref] if ref else []
    for name in names[:MAX_PENDING_ITEMS]:
        items.append({"name": name[:60]})
    logger.info("[handoff] parking %s for %d item(s) behind %s", task, len(items), route)
    return {
        "original_intent": message[:300],
        "pending_tasks": [task],
        "pending_items": items[:MAX_PENDING_ITEMS],
        "handoff_at": datetime.utcnow().isoformat(timespec="seconds"),
        "handoff_turn": turn_index,
        # 多商品提示在挂起这一刻就记下来：卖家说「这两件都帮我卖」的时候说了，
        # 回来那一轮说的是「都弄好了」，两句话里只有前一句知道有几件。
        "handoff_multi": bool(hints.get("multi_item_cue")),
    }


def _newly_listed(db: Session, seller_id: int, since: str) -> list[dict[str, Any]]:
    """挂起之后新上架的在售商品，旧的在前。

    这是接力绑定的**主依据**。卖家嘴里的名字和他填进表单的名字经常不是一个词，
    而「刚刚创建的」是一条不依赖措辞的事实。
    """
    try:
        cutoff = datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return []
    # 用 PLANNABLE 而不是 SEARCHABLE：走「以后再卖」那条链路上传的东西是**隐身**
    # 建出来的（PAUSED），它按定义不在搜索里。只看 SEARCHABLE 的话，卖家刚传完
    # 的那件东西对接力是隐形的，挂起的诉求就永远接不回来。
    rows = [
        i for i in ItemRepository(db).list_by_seller(int(seller_id))
        if i.status in ItemStatus.PLANNABLE and i.created_at and i.created_at >= cutoff
    ]
    rows.sort(key=lambda i: i.created_at)
    return [{"item_id": i.id, "name": (i.name or "")[:60]} for i in rows]


def _by_name(db: Session, seller_id: int, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """兜底：按卖家自己的说法在当前在售商品里找。

    用在时间戳靠不住的时候——卖家在系统开口之前就已经把东西传上去了，于是
    「新建于 handoff_at 之后」一件也匹配不到。
    """
    listings = [
        {"item_id": i.id, "name": (i.name or "")[:60]}
        for i in ItemRepository(db).list_by_seller(int(seller_id))
        if i.status in ItemStatus.PLANNABLE
    ]
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in pending:
        winner = intent_plan.best_match(
            intent_plan.match_listings(str(item.get("name") or ""), listings)
        )
        if winner and int(winner["item_id"]) not in seen:
            seen.add(int(winner["item_id"]))
            found.append(winner)
    return found


def resume_target(
    db: Session, seller_id: int, state: dict[str, Any], message: str, hints: dict[str, Any]
) -> dict[str, Any] | None:
    """「上传好了」——该接着办什么，办在哪几件东西上。

    返回 `{"scenario", "items", "original_intent", "unbound"}`，或者 None 表示这
    一轮不是接力（没有挂起的任务，或者这句话不是一句「做完了」）。

    `unbound` 是**没能绑上商品的那几件**。它不为空时接力照常进行，只是回复里要
    说清楚哪几件没找到——多商品场景下静默少办一件，比办错一件更难被发现。
    """
    tasks = list(state.get("pending_tasks") or [])
    if not tasks:
        return None
    if state.get("photo_path"):
        # 这一轮带了照片。照片说的是「东西在这儿」，不是「我弄完了」——它是上架
        # 流程的**入口**，而接力等的是上架**完成**。不挡住的话，「上传了」+ 一张图
        # 这种最常见的写法会在这里被接管：`_newly_listed` 还是空的（东西正要传），
        # 于是 `_by_name` 兜底上场，只要撞上一个名字近似的旧商品，这次上架就被
        # 换成了一次分析，而卖家手上那张照片谁也没处理。
        return None
    if not (hints.get("handoff_done") or hints.get("is_confirmation")):
        return None

    turn_index = len(state.get("turns") or []) // 2
    parked_at = int(state.get("handoff_turn") or 0)
    if turn_index - parked_at > MAX_HANDOFF_AGE_TURNS:
        logger.info("[handoff] parked task expired after %d turns", turn_index - parked_at)
        return {"expired": True, "scenario": "", "items": [], "unbound": [], "original_intent": ""}

    pending = list(state.get("pending_items") or [])
    items = _newly_listed(db, seller_id, str(state.get("handoff_at") or ""))
    if not items:
        items = _by_name(db, seller_id, pending)
    if not items:
        # 说了「传好了」，但库里什么新东西都没有。不接力，也不清空任务——
        # 卖家可能是传到一半、或者草稿还没点发布，下一轮再来一次就接上了。
        logger.info("[handoff] nothing newly listed; not resuming")
        return None

    # 卖家说了「都弄好了」却只找到一件，或者挂起时记了两件只回来一件——两种情况
    # 都要说出来。不说的话，3.2.2 那 5 条就只是从「一件都没接」变成「悄悄少接一件」。
    unbound: list[str] = []
    if len(pending) > len(items):
        bound_names = {str(i.get("name") or "") for i in items}
        unbound = [
            str(p.get("name") or "") for p in pending
            if str(p.get("name") or "") and str(p.get("name") or "") not in bound_names
        ]
    elif state.get("handoff_multi") and len(items) == 1:
        # 卖家当时说的是「这两件」，回来只找到一件。名字对不上号（他嘴里的「小白板」
        # 和表单里的「白色磁性写字板」经常不是一个词），所以说不出缺的是哪一件——
        # 但「可能少了一件」这件事必须说出来，否则 3.2.2 只是从「一件都没接」
        # 变成了「悄悄少接一件」，而后者更难被发现。
        unbound = ["（还有一件没找到）"]

    return {
        "expired": False,
        "scenario": tasks[0],
        "items": items[:MAX_PENDING_ITEMS],
        "unbound": unbound,
        "original_intent": str(state.get("original_intent") or ""),
    }


def cleared() -> dict[str, Any]:
    """接力完成（或作废）之后把条子撕掉。

    状态是跨轮持久化的，一个没被清掉的队列会在三轮之后突然接管一次无关的对话。
    """
    return {
        "pending_tasks": [],
        "pending_items": [],
        "original_intent": "",
        "hold_after_publish": False,
        "handoff_at": "",
        "handoff_turn": 0,
        "handoff_multi": False,
    }

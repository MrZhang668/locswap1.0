"""出售计划 agent 的工具实现。六个工具：一读、四写、一读。

## 这一层为什么比销售顾问那一层严

销售顾问的四个工具全是只读的——模型说错了，卖家看到一个不对的分析，下一句就能
纠正。这里的四个写工具改的是「几周后会不会有一封邮件发出去」，而那件事**卖家在
当下看不见**：他看到的只是一句「记下了」。所以下面每一个写操作都遵守三条：

1. **plan_id 一律回解**（`resolve_owned_plan`）。模型可以说出任何整数，只有属于
   这位卖家的才解析得到行，和 `sales.tools.resolve_owned_item` 同一条闸门。
2. **日期由后端换算**。工具收的是卖家的原话，`intent_rules.parse_target_date` 把它
   变成日期；模型自己算的那个值只在词法规则没覆盖时兜底，并且要过未来性校验。
3. **回执的承诺文案由服务端生成**。系统会做什么、不会做什么，是常量拼出来的，
   不是模型现写的。一旦口头答应了一个不存在的行为，卖家会按它安排自己的时间。

## 为什么没有 interrupt 闸门

推送给买家（`push_item_to_want`）和改上架状态（`update_item_status`）都挂了
`interrupt()` 二次确认。这里**故意没有**，因为那两个闸门守的是两类特定风险：
一个动作会碰到**别人**（陌生人的收件箱），或者会改变**一件正在卖的商品**的状态。
一条出售计划两样都不沾——它是私有的、可逆的、对外不可见的，而且回执里写着怎么撤。
给它加一道确认，等于教会卖家把确认当成噪音。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....config import settings
from ....models import Item, ItemStatus, NotificationType, SalePlan, SalePlanStatus, User
from ....repositories import ItemRepository
from ... import sale_plans as plan_service
from ...nlp import intent_rules
from .. import item_lookup
from ..sales.tools import ToolError
from .schemas import (
    CancelSalePlanInput,
    CreateSalePlanInput,
    ListSalePlansInput,
    ListSalePlansOutput,
    MAX_ACTIVE_PLANS_PER_SELLER,
    MAX_PLAN_DAYS,
    PeekPlanDemandInput,
    PeekPlanDemandOutput,
    PlanActionResult,
    PlanTrigger,
    SalePlanView,
    SetPlanWatchInput,
    UpdateSalePlanInput,
    WeekReading,
)

logger = logging.getLogger(__name__)

#: `peek_plan_demand` 往回看几周。四周足够看出方向，又不至于把一屏刷满。
PEEK_WEEKS = 4


# ============================================================
# 闸门与视图
# ============================================================
def resolve_owned_plan(db: Session, seller_id: int, plan_id: int) -> SalePlan:
    """模型给的 plan_id 和一行真实数据之间唯一的关口。

    「不存在」和「不是你的」**故意报同一句话**——区分开来就等于告诉调用方别的
    卖家有哪些计划。和 `resolve_owned_item` 是同一条规矩。
    """
    plan = db.get(SalePlan, int(plan_id))
    if plan is None or int(plan.seller_id) != int(seller_id):
        raise ToolError(
            f"plan_id={plan_id} 不是你的出售计划。"
            "先调用 list_sale_plans 看看实际可用的 plan_id。"
        )
    return plan


def _view(plan: SalePlan, master_enabled: bool, today: date | None = None) -> SalePlanView:
    today = today or date.today()
    watching = (
        plan.status == SalePlanStatus.ACTIVE
        and bool(plan.watch_enabled)
        and master_enabled
        and plan.watch_from <= today <= plan.target_date
    )
    return SalePlanView(
        plan_id=plan.id,
        item_name=plan.item_name,
        category=plan.category,
        item_id=plan.item_id,
        target_date=plan.target_date.isoformat(),
        watch_from=plan.watch_from.isoformat(),
        date_basis=plan.date_basis or "",
        status=plan.status,
        watch_enabled=bool(plan.watch_enabled),
        hold_item=bool(plan.hold_item),
        notified_count=int(plan.notified_count or 0),
        last_checked_at=(
            f"{plan.last_checked_at:%Y-%m-%d %H:%M}" if plan.last_checked_at else ""
        ),
        days_until_watch=(plan.watch_from - today).days,
        days_until_target=(plan.target_date - today).days,
        watching_now=watching,
    )


def _master(db: Session, seller_id: int) -> bool:
    user = db.get(User, int(seller_id))
    return bool(getattr(user, "sale_watch_enabled", True)) if user else True


def _hide_for_plan(db: Session, seller_id: int, item_id: int) -> tuple[bool, str]:
    """把一件在售商品收进「隐身」——在库中，但不曝光。返回 (成功了吗, 要告诉卖家的话)。

    「黑色笔记本量大了再卖」对一件**正在卖**的东西是自相矛盾的：它现在就在卖。
    这句话的落地就是把它从搜索里收起来，等需求起来再放回去。

    隐身用的是现成的 `ItemStatus.PAUSED`，不是新状态。那个状态已经不在
    `SEARCHABLE` 里，于是搜索索引、求购匹配、顾问工具三处**一行都不用改**就会跳过
    它——这正是当初把 PAUSED 设计成「唯一一个可恢复的下架态」的用处。再造一个
    HIDDEN，等于要把所有枚举状态的地方重新审一遍，漏一处就是一件「隐身」商品还能
    被搜到。

    有活跃预订时**不收**：买家手里攥着一个倒计时，把东西从他脚下抽走比让卖家先把
    预订处理完更糟。这时候如实报告，计划照记。
    """
    from ...item_index import get_item_index
    from ...lifecycle import LifecycleError, LifecycleService

    item = db.get(Item, int(item_id))
    seller = db.get(User, int(seller_id))
    if item is None or seller is None:
        return False, ""
    if item.status == ItemStatus.PAUSED:
        return True, "这件本来就已经是暂停状态，保持不变。"
    try:
        LifecycleService(db, get_item_index()).pause(item, seller)
    except LifecycleError as exc:
        logger.info("[plan_agent] cannot hide item %s: %s", item_id, exc)
        return False, f"没能把「{item.name}」收起来：{exc} 计划照样记下了，但它现在仍然在卖。"
    logger.info("[plan_agent] item %s hidden for a sale plan", item_id)
    return True, ""


def _commitment(plan: SalePlan, master_enabled: bool) -> list[str]:
    """系统对这条计划的承诺，逐条。**服务端生成，模型不得改写。**

    条数随事实变化：类别推断不出来时，「一周预订超过 N 单」那条判据根本不会触发
    （预订事件带的是 item_id，不是措辞），所以它**不会出现在这份清单里**。把一条
    不会触发的条件写成承诺，比少写一条更伤人。
    """
    # 日期手写而不是 strftime：`%d` 会给出 03，没有人这么说日期，而 `%-d` 在
    # Windows 上不可用。
    start = f"{plan.watch_from.year} 年 {plan.watch_from.month} 月 {plan.watch_from.day} 日"
    end = f"{plan.target_date.month} 月 {plan.target_date.day} 日"
    lines = [f"从 {start}起，每周替你看一次行情，到 {end}为止。"]
    if plan.category:
        lines.append(
            f"「{plan.category}」这一类一周的预订超过 "
            f"{settings.sale_plan_reserve_threshold} 单，我就在站内通知你并发一封邮件"
            "（每周最多一次）。"
        )
        lines.append(
            f"或者这一类的搜索量比上一周涨了 "
            f"{int(settings.sale_plan_growth_threshold * 100)}% 以上，"
            "我发一封邮件提醒你可以上架了。"
        )
    else:
        lines.append(
            f"和「{plan.item_name}」措辞对得上的搜索量比上一周涨了 "
            f"{int(settings.sale_plan_growth_threshold * 100)}% 以上，"
            "我发一封邮件提醒你可以上架了。"
        )
    if plan.hold_item:
        # 状态变更绝不能静默发生。这一行是它唯一被说出来的地方，所以它排在
        # 承诺清单的**最前面**，而不是夹在中间。
        lines.insert(
            0,
            f"「{plan.item_name}」我已经从搜索里收起来了（还在你的库里，买家搜不到）。"
            "想现在就卖，在「我的橱窗」里点恢复上架，或者跟我说一声——"
            "恢复之后这条计划就作废了。",
        )
    lines.append("我不会替你自动上架，也不会在这些条件之外主动找你。")
    lines.append(
        "想提前挂出来随时去「我的橱窗」恢复上架；不想等了跟我说一声，我把这条撤掉。"
        if plan.hold_item
        else "想提前挂出来随时去「手动上架」；不想等了跟我说一声，我把这条撤掉。"
    )
    if not master_enabled:
        # 总开关是关的：上面那几条现在一条都不会发生。不说出来，这份承诺就是假的。
        lines.append(
            "⚠ 你的出售计划监控总开关现在是关的，所以上面这些暂时都不会发生。"
            "说一句「把监控打开」就能恢复。"
        )
    return lines


# ============================================================
# Tool 1: list_sale_plans（只读）
# ============================================================
def list_sale_plans(
    db: Session, seller_id: int, params: ListSalePlansInput
) -> ListSalePlansOutput:
    master = _master(db, seller_id)
    today = date.today()
    rows = plan_service.list_plans(db, seller_id, active_only=not params.include_inactive)
    views = [_view(p, master, today) for p in rows]
    active = sum(1 for p in rows if p.status == SalePlanStatus.ACTIVE)

    caveats: list[str] = []
    if not master and active:
        caveats.append("总开关是关的，下面这些计划现在一条都不会触发提醒。")
    paused = [v for v in views if v.status == SalePlanStatus.ACTIVE and not v.watch_enabled]
    if paused:
        caveats.append(
            "这几条被单独停掉了：" + "、".join(f"「{v.item_name}」" for v in paused[:5])
        )
    return ListSalePlansOutput(
        total_active=active, master_enabled=master, plans=views, caveats=caveats
    )


# ============================================================
# 日期：后端换算，模型兜底
# ============================================================
def _resolve_date(phrase: str, raw_date: str) -> tuple[date, str]:
    """(日期, 依据)。两个来源都不可用时抛 ToolError，让模型去问卖家。

    顺序是**卖家原话优先**。模型算日期不是不会，是每次不一定一样，而这个值决定了
    几周后有没有一封邮件发出去；后端那套词法规则至少每次都一样，错了也错得可复现。
    """
    if phrase.strip():
        parsed, basis = intent_rules.parse_target_date(phrase.strip())
        if parsed is not None:
            return parsed, basis

    if raw_date.strip():
        try:
            parsed = date.fromisoformat(raw_date.strip())
        except ValueError:
            raise ToolError(
                f"target_date={raw_date!r} 不是一个合法日期，要写成 YYYY-MM-DD。"
            ) from None
        today = date.today()
        if parsed <= today:
            raise ToolError(
                f"target_date={raw_date!r} 已经过去了。出售计划只能排在将来——"
                "跟卖家确认一下他说的是哪一年。"
            )
        if parsed > today + timedelta(days=MAX_PLAN_DAYS):
            raise ToolError(
                f"target_date={raw_date!r} 在一年多以后，这通常是把月份读错了年份。"
                "跟卖家确认一下具体日期。"
            )
        return parsed, "model"

    raise ToolError(
        "没有可用的日期。把卖家自己的说法原样填进 target_phrase（「12 月放假前」"
        "「等学期末」「下个月」都认得）；他要是压根没说时间，就问他一句，不要自己定。"
    )


def _date_caveats(basis: str, target: date, plan: SalePlan) -> list[str]:
    """回执里要不要提一句这个日期是怎么来的。

    `explicit_date` 不提——卖家自己说了确切日期，复述一遍是废话。其余都提，因为
    一个猜错的日期和一个猜对的日期在回复里长得一模一样。
    """
    notes: list[str] = []
    md = f"{target.month} 月 {target.day} 日"
    if basis == "explicit_month":
        notes.append(f"他只说了月份，我按 {md} 记的——不对的话让他给个具体日期。")
    elif basis == "semester":
        notes.append(f"「学期末 / 放假前」我按 {md} 记的——不对的话让他给个具体日期。")
    elif basis == "relative":
        notes.append(f"我把他的说法换算成了 {md}——不对的话让他给个具体日期。")
    elif basis == "fuzzy":
        notes.append(f"他没说具体时间，我先按 {md} 记着——请他确认一下。")
    elif basis == "model":
        notes.append(f"这个日期是你自己算的（{md}），**一定要在回复里说出来让卖家确认**。")
    if not plan.category:
        notes.append(
            f"「{plan.item_name}」没能归进平台的任何类别，所以只能按名字匹配买家的"
            "搜索措辞——数字会偏小但是真的，而「一周预订超过多少单」那条判据用不上。"
            "这一点要在回复里说明。"
        )
    return notes


# ============================================================
# Tool 2: create_sale_plan（写）
# ============================================================
def create_sale_plan(
    db: Session, seller_id: int, params: CreateSalePlanInput
) -> PlanActionResult:
    target, basis = _resolve_date(params.target_phrase, params.target_date)
    master = _master(db, seller_id)

    name = params.item_name.strip()[:120]
    existing = db.scalar(
        select(SalePlan).where(
            SalePlan.seller_id == int(seller_id),
            SalePlan.item_name == name,
            SalePlan.status == SalePlanStatus.ACTIVE,
        )
    )
    if existing is None and plan_service.active_plan_count(db, seller_id) >= MAX_ACTIVE_PLANS_PER_SELLER:
        raise ToolError(
            f"这位卖家已经有 {MAX_ACTIVE_PLANS_PER_SELLER} 条在盯的计划了，这是上限。"
            "先让他撤掉一条（cancel_sale_plan），再加新的。"
        )

    # 名字对得上一件在售商品时，把计划挂到它身上——卖家想先下架、到点再上，
    # 这条路走得通，而且挂上之后周扫描能用 analyze_demand 这个真正的 item 级分析。
    item_id = _match_listing(db, seller_id, name)

    plan = plan_service.create_plan(
        db, seller_id,
        item_name=name, target_date=target, date_basis=basis,
        note=params.note[:300], item_id=item_id,
    )
    # create_plan 对同名活跃计划是更新而不是新增，所以这里要把它重新打开——
    # 卖家重新说了一遍「转换器 12 月卖」，意思显然是要它生效。
    if not plan.watch_enabled:
        plan.watch_enabled = True
        db.commit()

    # ---- 链路 1：东西已经在库里 → 收进隐身 ----
    hold_note = ""
    if item_id:
        held, hold_note = _hide_for_plan(db, seller_id, item_id)
        if held != bool(plan.hold_item):
            plan.hold_item = held
            db.commit()

    caveats = _date_caveats(basis, target, plan)
    if existing is not None:
        caveats.insert(0, "这条计划之前就有，这次是**改了日期**，不是新增一条。")
    if hold_note:
        caveats.append(hold_note)
    elif plan.hold_item:
        caveats.append(
            "这件商品已经被收进隐身（在库中、买家搜不到）。**回复里必须说出这件事**——"
            "状态变更绝不能静默发生。"
        )
    return PlanActionResult(
        action="updated" if existing is not None else "created",
        plan=_view(plan, master),
        commitment=_commitment(plan, master),
        caveats=caveats,
        master_enabled=master,
    )


def _match_listing(db: Session, seller_id: int, name: str) -> int | None:
    """这个名字是不是指向一件已经在售的商品——**只在唯一命中时**返回它。

    这个返回值后面跟着一次写：绑上了，`_hide_for_plan` 就把那件商品收进隐身，中间
    没有确认这一步。所以它用的是 `item_lookup`（中心词 → 模型 + 子类目过滤），不再
    是分词重合：「自行车反光灯期末再卖」和「Giant白色通勤自行车」重合了「自行车」，
    旧判据会把那辆自行车从搜索里收掉。

    判不出来（模型调不通）或对上不止一件，一律不绑：最坏是计划只按名字盯着，
    而绑错的代价是一件卖家没说要收的东西消失了。
    """
    # PLANNABLE 而不是 SEARCHABLE：一件**已经被收起来**的商品仍然是计划的对象——
    # 那正是「等需求起来再卖」的常态。只看在售的，走链路 2 上传的隐身商品就绑不上，
    # 于是计划没有 item_id，既走不了 item 级需求分析，也不知道自己收起过谁。
    listings = [
        {"item_id": i.id, "name": (i.name or "")[:60],
         "category": (i.attributes or {}).get("category"),
         "subcategory": (i.attributes or {}).get("subcategory"),
         "head_noun": (i.attributes or {}).get("head_noun")}
        for i in ItemRepository(db).list_by_seller(int(seller_id))
        if i.status in ItemStatus.PLANNABLE
    ]
    res = item_lookup.resolve_ref(name, listings)
    if not res.decided or len(res.matches) != 1:
        return None
    return int(res.matches[0]["item_id"])


# ============================================================
# Tool 3: update_sale_plan（写）
# ============================================================
def update_sale_plan(
    db: Session, seller_id: int, params: UpdateSalePlanInput
) -> PlanActionResult:
    plan = resolve_owned_plan(db, seller_id, params.plan_id)
    if plan.status != SalePlanStatus.ACTIVE:
        raise ToolError(
            f"plan_id={plan.id}（{plan.item_name}）状态是 {plan.status}，不是在盯的计划，"
            "改不了日期。要重新开始盯就用 create_sale_plan 新建一条。"
        )
    target, basis = _resolve_date(params.target_phrase, params.target_date)
    master = _master(db, seller_id)

    plan.target_date = target
    plan.watch_from = target - timedelta(weeks=settings.sale_plan_watch_weeks)
    plan.date_basis = basis[:20]
    db.commit()
    logger.info("[plan_agent] plan #%s moved to %s", plan.id, target)

    return PlanActionResult(
        action="updated",
        plan=_view(plan, master),
        commitment=_commitment(plan, master),
        caveats=_date_caveats(basis, target, plan),
        master_enabled=master,
    )


# ============================================================
# Tool 4: cancel_sale_plan（写）
# ============================================================
def cancel_sale_plan(
    db: Session, seller_id: int, params: CancelSalePlanInput
) -> PlanActionResult:
    plan = resolve_owned_plan(db, seller_id, params.plan_id)
    master = _master(db, seller_id)
    if plan.status != SalePlanStatus.ACTIVE:
        # 已经撤掉的再撤一次不是错误，但也不该报告成「撤掉了」——那会让卖家以为
        # 刚才有什么东西被改动了。
        return PlanActionResult(
            action="noop",
            plan=_view(plan, master),
            caveats=[f"「{plan.item_name}」本来就不在盯了（状态 {plan.status}），什么都没改。"],
            master_enabled=master,
        )
    plan.status = SalePlanStatus.CANCELLED
    db.commit()
    logger.info("[plan_agent] plan #%s cancelled", plan.id)
    return PlanActionResult(
        action="cancelled",
        plan=_view(plan, master),
        commitment=[
            f"「{plan.item_name}」这条计划撤掉了，不会再有任何提醒。",
            "想重新盯的话说一声，我再排一条——之前攒的每周读数还在，不用从头开始。",
        ],
        master_enabled=master,
    )


# ============================================================
# Tool 5: set_plan_watch（写）—— 开关的自然语言那一路
# ============================================================
def set_plan_watch(
    db: Session, seller_id: int, params: SetPlanWatchInput
) -> PlanActionResult:
    if params.plan_id is not None:
        plan = resolve_owned_plan(db, seller_id, params.plan_id)
        if bool(plan.watch_enabled) == params.enabled:
            master = _master(db, seller_id)
            return PlanActionResult(
                action="noop",
                plan=_view(plan, master),
                caveats=[
                    f"「{plan.item_name}」本来就是"
                    f"{'开着' if params.enabled else '停着'}的，什么都没改。"
                ],
                master_enabled=master,
            )
        plan.watch_enabled = params.enabled
        db.commit()
        master = _master(db, seller_id)
        logger.info(
            "[plan_agent] plan #%s watch -> %s", plan.id, params.enabled
        )
        return PlanActionResult(
            action="watch_changed",
            plan=_view(plan, master),
            commitment=(
                _commitment(plan, master) if params.enabled
                else [
                    f"「{plan.item_name}」暂停盯了，不会再有它的提醒。",
                    "其他计划不受影响；想让它继续盯，说一句就行。",
                ]
            ),
            master_enabled=master,
        )

    # ---- 总开关 ----
    user = db.get(User, int(seller_id))
    if user is None:
        raise ToolError("找不到这位卖家。")
    if bool(user.sale_watch_enabled) == params.enabled:
        return PlanActionResult(
            action="noop",
            caveats=[f"总开关本来就是{'开着' if params.enabled else '关着'}的，什么都没改。"],
            master_enabled=bool(user.sale_watch_enabled),
        )
    user.sale_watch_enabled = params.enabled
    db.commit()
    logger.info("[plan_agent] seller %s master watch -> %s", seller_id, params.enabled)

    rows = plan_service.list_plans(db, seller_id, active_only=True)
    views = [_view(p, params.enabled) for p in rows]
    if params.enabled:
        commitment = [f"出售计划监控打开了，{len(rows)} 条计划恢复生效。"]
        commitment += [
            f"「{v.item_name}」——{v.watch_from} 起每周看，到 {v.target_date} 为止。"
            for v in views[:5]
        ]
    else:
        commitment = [
            "出售计划监控关掉了。所有计划都还在，但不会再有任何站内通知或邮件。",
            "说一句「把监控打开」就能恢复，计划一条都不用重排。",
        ]
    return PlanActionResult(
        action="watch_changed",
        affected=views,
        commitment=commitment,
        master_enabled=params.enabled,
    )


# ============================================================
# Tool 6: peek_plan_demand（只读）
# ============================================================
def peek_plan_demand(
    db: Session, seller_id: int, params: PeekPlanDemandInput, *, week: date | None = None
) -> PeekPlanDemandOutput:
    """「转换器现在什么情况」——把已经攒下的周读数和这一周的实时读数一起给出来。

    两个调用方，同一个函数：

    * **对话里**，模型调它，`week` 不传 → `current` 是这一周到目前为止的读数。
    * **周扫描**（`sale_plans.run_weekly_pass`）每周调一次，`week` 传刚走完的那一周的
      周一 → `current` 是那一整周的读数，`triggers` 就是这一周要发的通知。

    `week` 是纯 Python 关键字参数，**不进工具 schema**，和 `analyze_demand` 的
    `allow_held` 同一个做法：模型没有渠道去量任意一周。

    `would_notify` 和 `triggers` 都是**服务端算的**，而且出自同一个
    `sale_plans.evaluate`：按现在的数字，两条判据分别处在什么位置。让模型自己比
    大小，它迟早会在某一轮把 9 说成「快到 10 了，应该快通知了」，而那是一句系统
    不会兑现的话。
    """
    plan = resolve_owned_plan(db, seller_id, params.plan_id)
    today = date.today()
    this_week = week or plan_service.week_start_of(today)

    # 只取 `this_week` 之前的快照：周扫描补跑同一周时，那一周自己的快照已经在库里，
    # 拿它当「上一周」，增长率就是自己比自己。
    rows = list(db.scalars(
        select(plan_service.DemandSnapshot)
        .where(
            plan_service.DemandSnapshot.plan_id == plan.id,
            plan_service.DemandSnapshot.week_start < this_week,
        )
        .order_by(plan_service.DemandSnapshot.week_start.desc())
        .limit(PEEK_WEEKS)
    ))
    weeks = [
        WeekReading(
            week_start=r.week_start.isoformat(),
            reserve_count=r.reserve_count, people=r.people, events=r.events,
            matching_people=r.matching_people,
            source=r.source or "category_keyword", trend=r.trend or "",
        )
        for r in reversed(rows)
    ]

    reading = plan_service.weekly_reading(db, plan, this_week)
    current = WeekReading(week_start=this_week.isoformat(), **reading)

    caveats: list[str] = []
    if not weeks:
        if plan.watch_from > today:
            caveats.append(
                f"还没到观察窗口（{plan.watch_from} 才开始），所以没有历史周读数。"
                "下面那个是**现在**的实时读数，不是攒下来的记录。"
            )
        else:
            caveats.append("观察窗口已经开始，但还没跑过周扫描，所以没有历史读数。")
    if not plan.category:
        caveats.append(
            "这条计划没有类别，只能按名字措辞匹配——预订数恒为 0，别把它读成「没人买」。"
        )
    if current.source == "analyze_demand":
        caveats.append("这条计划挂在一件在售商品上，所以读数走的是 analyze_demand 本身。")
    if week is None:
        caveats.append(
            "current 是这一周到目前为止的读数，还没走完；真正决定发不发的是下一次"
            "周扫描，它按走完的整周算。"
        )

    # WeekReading 和 DemandSnapshot 字段同名，`evaluate` 两个都吃。
    prior = weeks[-1] if weeks else None
    hits = plan_service.evaluate(current, prior)
    triggers = [
        PlanTrigger(
            kind=str(kind),
            channels=list(plan_service.NOTIFY_CHANNELS[kind]),
            detail=detail,
        )
        for kind, detail in hits
    ]

    return PeekPlanDemandOutput(
        plan_id=plan.id,
        item_name=plan.item_name,
        category=plan.category,
        weeks=weeks,
        current=current,
        would_notify=_threshold_state(prior, current, {str(k) for k, _ in hits}),
        triggers=triggers,
        caveats=caveats,
    )


def _threshold_state(
    prior: WeekReading | None, current: WeekReading, hit: set[str]
) -> list[str]:
    """两条判据现在离触发有多远。过没过线看 `evaluate` 的结果（`hit`），这里只
    负责措辞——判定只有一份，不在这里再比一遍大小。"""
    out: list[str] = []
    reserve_gate = settings.sale_plan_reserve_threshold
    if str(NotificationType.SALE_PLAN_HOT) in hit:
        out.append(
            f"预订这条已经过线了（{current.reserve_count} > {reserve_gate}），"
            "会发站内通知 + 邮件。"
        )
    else:
        out.append(
            f"预订这条还没过线（{current.reserve_count}，线是 {reserve_gate}）。"
        )
    if prior is not None:
        if prior.events >= settings.sale_plan_growth_min_base:
            growth = (current.events - prior.events) / float(prior.events)
            gate = settings.sale_plan_growth_threshold
            if str(NotificationType.SALE_PLAN_SURGE) in hit:
                verdict, then = "已经过线", "，会发一封邮件提醒可以上架了"
            else:
                verdict, then = "还没过线", ""
            out.append(
                f"增长这条{verdict}（{prior.events} → {current.events}，"
                f"{growth * 100:+.0f}%，线是 {gate * 100:.0f}%）{then}。"
            )
        else:
            out.append(
                f"上一周基数太小（{prior.events} 次，下限 "
                f"{settings.sale_plan_growth_min_base}），增长这条这周不参与判断。"
            )
    else:
        out.append("还没有上一周的记录，增长这条暂时无法判断。")
    return out

"""延后出售计划：记下「等学期末再挂」，然后到点替卖家盯着市场。

## 这个模块补的是哪一块

归因二的 10 条 badcase 有一个共同形状：卖家说了「以后再卖」，系统读成「现在要
卖」。「转换器我打算 12 月放假前再卖」被送去催上传，「分线器还得用一阵」被送去
做需求分析——两个回答都文不对题，而且错在同一个地方：路由层没有「这件事要等」
这个出口，只能把消息塞给已有的某一个 agent。

更贵的是 3.3.4：「需求涨起来了你能提醒我吗」。系统当时既没答应也没拒绝，只是继
续澄清。**口头答应一个不存在的能力，损伤远大于一次答错**——卖家会按这个承诺安
排自己的时间，而那个提醒永远不会来。所以这里的做法不是加一段更委婉的话术，而是
把这个能力真的做出来：有计划表、有周扫描、有触发条件、有通知和邮件。

## 两条触发规则，以及它们为什么长这样

* **热度**：一周内该类别的预订数超过阈值 → 站内通知 + 邮件，一周一次，直到目标
  日期。用预订而不是搜索，是因为预订是**掏过手的**行为：搜索说明有人在看，预订
  说明有人在买。
* **增长**：相邻两周的需求事件数增长超过阈值 → 邮件提示可以提前上架。这条**必须**
  有一个基数下限（`sale_plan_growth_min_base`），否则 1 涨到 2 就是 100%，卖家会
  收到一封关于两个人的市场的信。

## 口径

商品还没上架，所以 `analyze_demand` 那套 item 级分析在这里跑不了。取而代之的是
**类别级 + 商品名关键词**两个口径，分开报，不合成分数——和 `analyze_demand` 的
三层口径是同一条规矩：合成之后卖家就看不出该对哪一个采取行动。类别口径回答「这
个市场热不热」，关键词口径回答「其中有多少人真的在找你这件东西」，后者总是更小，
也总是更真。

## 一周是怎么判的

每条计划从 `watch_from` 那天起，每个自然周查一次**刚走完的那一整周**，查的是 plan
agent 的 `peek_plan_demand`（见 `run_weekly_pass`）。去重看「这一周的快照写过没
有」，不看星期几：进程哪天起来都能把这一周补上，同一周也不会发两遍信。以前还有
一道「只在周一跑」的闸门，进程在某个周一没起来，那一周就永远丢了。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    DemandEvent,
    DemandEventKind,
    DemandSnapshot,
    NotificationType,
    SalePlan,
    SalePlanStatus,
    User,
    utcnow,
)
from ..repositories import NotificationRepository
from . import email_outbox
from .query_parser import parse_query
from .text import raw_tokens

logger = logging.getLogger(__name__)

#: 一位卖家最多同时有这么多条在盯的计划。上限的意义不是省资源，是让「我全部的
#: 库存都等学期末再卖」这种输入不会变成三十封邮件。
MAX_ACTIVE_PLANS_PER_SELLER = 10

#: 快照里参与关键词比对的 token 至少要这么长。和 `intent_plan._content_tokens`
#: 同一个理由：单字会把「桌」匹配到「桌面麦克风」。
_MIN_TOKEN_LEN = 2


def infer_category(item_name: str) -> str | None:
    """从卖家自己的说法猜类别。猜不出来就是 None，不硬塞一个。

    复用搜索侧的 `parse_query`——买家搜「转换器」和卖家说「转换器」走同一套词表，
    是这个数字后面能对得上的前提。两边各写一套词表，就会出现「系统说这个类别有
    14 个人在找，搜索页却一个都搜不到」。
    """
    try:
        return parse_query(item_name or "").category or None
    except Exception:  # noqa: BLE001  分类失败不该让记录计划这件事失败
        logger.warning("[sale_plan] category inference failed for %r", item_name[:40])
        return None


def create_plan(
    db: Session,
    seller_id: int,
    *,
    item_name: str,
    target_date: date,
    date_basis: str = "",
    note: str = "",
    item_id: int | None = None,
) -> SalePlan:
    """记下一条计划。同名同卖家的活跃计划会被更新而不是新增。

    重复记录是这类功能最常见的毛病：卖家说两遍「转换器等 12 月」，然后每周收两封
    一样的信。按 (卖家, 商品名) 去重，改日期就是改日期。
    """
    name = (item_name or "").strip()[:120]
    watch_from = target_date - timedelta(weeks=settings.sale_plan_watch_weeks)
    existing = db.scalar(
        select(SalePlan).where(
            SalePlan.seller_id == int(seller_id),
            SalePlan.item_name == name,
            SalePlan.status == SalePlanStatus.ACTIVE,
        )
    )
    if existing is not None:
        existing.target_date = target_date
        existing.watch_from = watch_from
        existing.date_basis = (date_basis or "")[:20]
        existing.note = (note or "")[:300]
        existing.category = infer_category(name)
        db.commit()
        logger.info("[sale_plan] updated #%s -> %s", existing.id, target_date)
        return existing

    plan = SalePlan(
        seller_id=int(seller_id),
        item_name=name,
        category=infer_category(name),
        item_id=item_id,
        target_date=target_date,
        date_basis=(date_basis or "")[:20],
        watch_from=watch_from,
        status=SalePlanStatus.ACTIVE,
        note=(note or "")[:300],
    )
    db.add(plan)
    db.commit()
    logger.info(
        "[sale_plan] #%s %r -> %s (watch from %s)", plan.id, name, target_date, watch_from
    )
    return plan


def list_plans(db: Session, seller_id: int, *, active_only: bool = True) -> list[SalePlan]:
    q = select(SalePlan).where(SalePlan.seller_id == int(seller_id))
    if active_only:
        q = q.where(SalePlan.status == SalePlanStatus.ACTIVE)
    return list(db.scalars(q.order_by(SalePlan.target_date)))


def cancel_plan(db: Session, seller_id: int, plan_id: int) -> bool:
    plan = db.get(SalePlan, int(plan_id))
    if plan is None or int(plan.seller_id) != int(seller_id):
        return False
    plan.status = SalePlanStatus.CANCELLED
    db.commit()
    return True


def active_plan_count(db: Session, seller_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(SalePlan.id)).where(
                SalePlan.seller_id == int(seller_id),
                SalePlan.status == SalePlanStatus.ACTIVE,
            )
        )
        or 0
    )


# ---------------- 周快照 ----------------
def week_start_of(day: date) -> date:
    """这一天所在自然周的周一。快照按周一对齐，两条快照才可比。"""
    return day - timedelta(days=day.weekday())


def _item_tokens(item_name: str) -> set[str]:
    return {t for t in raw_tokens(item_name or "") if len(t) >= _MIN_TOKEN_LEN}


def measure_week(db: Session, plan: SalePlan, week_start: date) -> dict[str, int]:
    """一条计划在某一周的四个读数。纯 SQL，没有模型参与。

    返回 `reserve_count` / `people` / `events` / `matching_people`。前三个是类别
    口径（这个市场怎么样），最后一个是关键词口径（其中有多少人真的在找这件东西）。
    """
    start = datetime.combine(week_start, datetime.min.time())
    end = start + timedelta(days=7)

    if not plan.category:
        # 没推断出类别（「转换器」这种词表里没有的说法）。
        #
        # 这里**不能**报三个零就完事：`evaluate` 的两条判据都读这几个字段，全零
        # 意味着这条计划会被静静地盯到过期、一封信都不发——一个从不触发的提醒，
        # 比没有这个功能更糟，因为卖家以为它在替他看着。
        #
        # 所以退到**纯关键词口径**：全平台范围内，措辞和这件东西对得上的搜索。
        # 数字更小、也更窄，但它是真的。预订那一条确实用不上（预订事件带的是
        # item_id 而不是措辞），所以它保持为 0，并且**这件事会写进回执告诉卖家**，
        # 而不是让他以为那条判据还在生效。
        people, events = _keyword_stats(db, plan, start, end, category=None)
        return {
            "reserve_count": 0,
            "people": people,
            "events": events,
            "matching_people": people,
        }

    reserve_count = int(
        db.scalar(
            select(func.count(DemandEvent.id)).where(
                DemandEvent.kind == DemandEventKind.RESERVE,
                DemandEvent.category == plan.category,
                DemandEvent.at >= start,
                DemandEvent.at < end,
            )
        )
        or 0
    )
    events = int(
        db.scalar(
            select(func.count(DemandEvent.id)).where(
                DemandEvent.kind == DemandEventKind.SEARCH,
                DemandEvent.category == plan.category,
                DemandEvent.at >= start,
                DemandEvent.at < end,
            )
        )
        or 0
    )
    people = int(
        db.scalar(
            select(func.count(func.distinct(DemandEvent.buyer_id))).where(
                DemandEvent.kind == DemandEventKind.SEARCH,
                DemandEvent.category == plan.category,
                DemandEvent.at >= start,
                DemandEvent.at < end,
            )
        )
        or 0
    )
    matching, _ = _keyword_stats(db, plan, start, end, category=plan.category)
    return {
        "reserve_count": reserve_count,
        "people": people,
        "events": events,
        "matching_people": matching,
    }


def _keyword_stats(
    db: Session, plan: SalePlan, start: datetime, end: datetime, category: str | None
) -> tuple[int, int]:
    """措辞真的和这件东西重合的 (去重买家数, 搜索次数)。

    和 `analyze_demand` 用的是同一条规则（`raw_tokens` 重合），刻意没有另起一套：
    一条可解释的规则好过两条会互相打架的。

    `category` 为 None 时在全平台范围内比对——那是这条计划仅有的信号源。
    """
    tokens = _item_tokens(plan.item_name)
    if not tokens:
        return 0, 0
    q = select(DemandEvent.buyer_id, DemandEvent.query).where(
        DemandEvent.kind == DemandEventKind.SEARCH,
        DemandEvent.at >= start,
        DemandEvent.at < end,
    )
    if category:
        q = q.where(DemandEvent.category == category)
    buyers: set[int] = set()
    events = 0
    for buyer_id, phrase in db.execute(q):
        if tokens & set(raw_tokens(phrase or "")):
            buyers.add(int(buyer_id))
            events += 1
    return len(buyers), events


def weekly_reading(db: Session, plan: SalePlan, week_start: date) -> dict:
    """一条计划某一周的读数，**优先走销售顾问自己的需求分析工具**。

    两条路，选哪条由一个事实决定——这条计划有没有挂在一件在售商品上：

    * 挂上了（`plan.item_id` 有值且商品还在售）→ 直接调 `seller_advisor.tools
      .analyze_demand`。那是销售顾问回答「有多少人在找这类东西」时用的**同一个
      函数**，不是它的复刻版。这么做不是为了省代码，是为了让两个地方报出来的数
      永远一致：卖家在对话里问一次、在邮件里收一次，两个数字对不上的时候他不会
      认为是口径不同，只会认为有一个在骗他。
    * 没挂上（东西还没上架，这是绝大多数情况）→ 退到 `measure_week` 的类别 +
      关键词口径。`analyze_demand` 是 item 级的，没有 item_id 它根本跑不了。

    返回值里带 `source`，因为**两种口径的数不可直接比**：换了口径的那一周，增长率
    会凭空跳一下，而那不是市场变了。存下来才能在事后看出来。
    """
    if plan.item_id:
        reading = _reading_via_analyze_demand(db, plan)
        if reading is not None:
            return reading
    values = measure_week(db, plan, week_start)
    values["source"] = "category_keyword"
    values["trend"] = ""
    return values


def _reading_via_analyze_demand(db: Session, plan: SalePlan) -> dict | None:
    """走销售顾问那个工具。跑不了就返回 None，让调用方退回类别口径。

    窗口用 `last_7d`：周扫描在周一跑，往回七天正好是刚走完的那一周。这不是和
    `week_start` 严格对齐的同一区间（差几个小时），但对「这一周热不热」这个问题
    来说，差几个小时不会改变结论，而为了对齐去重写一个 item 级分析才是真的代价。

    预订数仍然按类别数：`analyze_demand` 报的是搜索和曝光，没有预订这一层，而
    预订是两条判据里更硬的那个信号（搜索说明有人在看，预订说明有人在买）。
    """
    from .seller_advisor.schemas import AnalyzeDemandInput
    from .seller_advisor.tools import ToolError, analyze_demand

    try:
        # `allow_held=True`：这条计划多半**就是把商品收起来等**的，而「收起来」
        # 恰好让那道闸门默认拒绝分析它。不开这个开关，链路 1 会静默退回类别口径
        # ——数字还在，但不再是这件商品自己的了。
        out = analyze_demand(
            db, int(plan.seller_id),
            AnalyzeDemandInput(item_id=int(plan.item_id), window="last_7d"),
            allow_held=True,
        )
    except ToolError as exc:
        # 商品被下架、删掉或者卖掉了。计划本身还有效（卖家可能又收了一个），
        # 所以不报错，退回类别口径。
        logger.info("[sale_plan] plan #%s can no longer use analyze_demand: %s", plan.id, exc)
        return None
    except Exception:  # noqa: BLE001  一条计划量不出来不能拖垮整批
        logger.exception("[sale_plan] analyze_demand failed for plan #%s", plan.id)
        return None

    start = datetime.utcnow() - timedelta(days=7)
    reserve_count = int(
        db.scalar(
            select(func.count(DemandEvent.id)).where(
                DemandEvent.kind == DemandEventKind.RESERVE,
                DemandEvent.category == out.category,
                DemandEvent.at >= start,
            )
        )
        or 0
    ) if out.category else 0

    return {
        "reserve_count": reserve_count,
        "people": int(out.total_people or 0),
        "events": int(out.total_events or 0),
        "matching_people": int(out.people_matching_item or 0),
        "source": "analyze_demand",
        "trend": str(getattr(out, "trend", "") or ""),
    }


def snapshot_for(db: Session, plan: SalePlan, week_start: date) -> DemandSnapshot | None:
    return db.scalar(
        select(DemandSnapshot).where(
            DemandSnapshot.plan_id == plan.id, DemandSnapshot.week_start == week_start
        )
    )


def record_snapshot(
    db: Session, plan: SalePlan, week_start: date, values: dict | None = None
) -> DemandSnapshot:
    """写下（或覆盖）某一周的快照。重复跑同一周不会产生两行。

    `values` 给了就直接落这一份：周扫描已经经 `peek_plan_demand` 量过一遍，
    再量一次不只是浪费——两次之间来了一条新搜索，落库的数就和判定用的数不是同一份。
    """
    existing = snapshot_for(db, plan, week_start)
    if values is None:
        values = weekly_reading(db, plan, week_start)
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        db.flush()
        return existing
    row = DemandSnapshot(plan_id=plan.id, week_start=week_start, **values)
    db.add(row)
    db.flush()
    return row


def previous_snapshot(db: Session, plan: SalePlan, week_start: date) -> DemandSnapshot | None:
    return db.scalar(
        select(DemandSnapshot)
        .where(DemandSnapshot.plan_id == plan.id, DemandSnapshot.week_start < week_start)
        .order_by(DemandSnapshot.week_start.desc())
        .limit(1)
    )


# ---------------- 触发判定 ----------------
def evaluate(
    current: DemandSnapshot, prior: DemandSnapshot | None
) -> list[tuple[str, dict]]:
    """这一周该不该告诉卖家点什么。返回 [(触发类型, 细节)]。

    纯函数，不碰数据库也不发东西——判据能被单独跑一遍，是它能被信任的前提。
    """
    hits: list[tuple[str, dict]] = []

    if current.reserve_count > settings.sale_plan_reserve_threshold:
        hits.append((
            NotificationType.SALE_PLAN_HOT,
            {
                "reserve_count": current.reserve_count,
                "threshold": settings.sale_plan_reserve_threshold,
                "people": current.people,
                "matching_people": current.matching_people,
            },
        ))

    if prior is not None and prior.events >= settings.sale_plan_growth_min_base:
        growth = (current.events - prior.events) / float(prior.events)
        if growth > settings.sale_plan_growth_threshold:
            hits.append((
                NotificationType.SALE_PLAN_SURGE,
                {
                    "growth": round(growth, 3),
                    "threshold": settings.sale_plan_growth_threshold,
                    "events": current.events,
                    "prior_events": prior.events,
                    "people": current.people,
                    "matching_people": current.matching_people,
                },
            ))
    return hits


#: 每种触发走哪几个渠道。热度是「这周真有人在买」，值得在站内打断一下卖家；
#: 增长只是「可以考虑提前上架了」的提示，一封邮件就够，不占站内通知。
NOTIFY_CHANNELS: dict[str, tuple[str, ...]] = {
    NotificationType.SALE_PLAN_HOT: ("in_app", "email"),
    NotificationType.SALE_PLAN_SURGE: ("email",),
}


# ---------------- 文案 ----------------
def _hot_text(plan: SalePlan, detail: dict, week_start: date) -> tuple[str, str]:
    subject = f"「{plan.item_name}」这一周有 {detail['reserve_count']} 单预订"
    body = (
        f"你之前说过打算 {plan.target_date:%Y 年 %m 月 %d 日} 再卖「{plan.item_name}」，"
        f"我一直在替你盯着。\n\n"
        f"{week_start.month} 月 {week_start.day} 日那一周，"
        f"{plan.category or '这一类东西'}下面成交了 "
        f"{detail['reserve_count']} 单预订，超过了 {detail['threshold']} 单这个线。\n"
        f"同期有 {detail['people']} 个人搜过这一类；其中 {detail['matching_people']} "
        f"个人的措辞和「{plan.item_name}」是对得上的。\n\n"
        f"这两个数字要分开看：前一个是这个市场有多热，后一个才是有多少人在找你这件东西。\n\n"
        f"要提前挂出来的话，去「手动上架」就行。不想改计划也没关系，"
        f"我会继续每周看一次，到 {plan.target_date:%m 月 %d 日} 为止。"
    )
    return subject, body


def _surge_text(plan: SalePlan, detail: dict, week_start: date) -> tuple[str, str]:
    subject = (
        f"「{plan.item_name}」的需求比上周涨了 {detail['growth'] * 100:.0f}%，可以上架了"
    )
    body = (
        f"你之前说过打算 {plan.target_date:%Y 年 %m 月 %d 日} 再卖「{plan.item_name}」。\n\n"
        f"{week_start.month} 月 {week_start.day} 日那一周，"
        f"{plan.category or '和你这件东西措辞对得上'}的搜索量从上周的 "
        f"{detail['prior_events']} 次涨到 {detail['events']} 次，涨幅 "
        f"{detail['growth'] * 100:.0f}%，超过了 {detail['threshold'] * 100:.0f}% 这个线。\n"
        f"这一周有 {detail['people']} 个人在搜，其中 {detail['matching_people']} "
        f"个人的措辞和你这件东西对得上。\n\n"
        f"涨得快通常意味着现在挂比等到计划日期更容易出手，可以上架了——去「手动上架」"
        f"就行。要不要提前由你定，我只是把数字摆出来。"
    )
    return subject, body


_TEXT_BY_KIND = {
    NotificationType.SALE_PLAN_HOT: _hot_text,
    NotificationType.SALE_PLAN_SURGE: _surge_text,
}


def _notify(db: Session, plan: SalePlan, kind: str, detail: dict, week_start: date) -> None:
    """按 `NOTIFY_CHANNELS` 发：热度是站内一条 + 邮件一封，增长只有邮件。"""
    channels = NOTIFY_CHANNELS[kind]
    subject, body = _TEXT_BY_KIND[kind](plan, detail, week_start)
    if "in_app" in channels:
        NotificationRepository(db).create(
            int(plan.seller_id),
            kind,
            {
                "plan_id": plan.id,
                "item_name": plan.item_name,
                "category": plan.category,
                "target_date": plan.target_date.isoformat(),
                "week_start": week_start.isoformat(),
                "summary": subject,
                **detail,
            },
        )
    seller = db.get(User, int(plan.seller_id))
    if seller is not None and "email" in channels:
        email_outbox.queue(
            db,
            to_email=seller.email,
            subject=subject,
            body=body,
            kind=kind,
            user_id=seller.id,
        )
    plan.last_notified_at = utcnow()
    plan.notified_count += 1


# ---------------- 周扫描 ----------------
def due_plans(db: Session, today: date) -> list[SalePlan]:
    """今天该看哪几条计划。

    五个条件：还活着、已经进入观察窗口、目标日期还没过、**这一条自己开着**、
    **这位卖家的总开关开着**。

    目标日期那一条不是可有可无的——一条过期计划每周继续发信，是这个功能从「有用」
    变成「垃圾邮件」的那一步。

    两个开关都在这里判，而不是在发信那一步判：关掉监控的人不该在数据库里留下
    「本来要发但没发」的痕迹，也不该让周扫描白算一遍。`SalePlan.watch_enabled`
    走 SQL，总开关走一次按卖家的查询——卖家数远少于计划数，一次性取出来比每条
    计划回查一次划算。
    """
    rows = list(db.scalars(
        select(SalePlan).where(
            SalePlan.status == SalePlanStatus.ACTIVE,
            SalePlan.watch_from <= today,
            SalePlan.watch_enabled.is_(True),
        )
    ))
    rows = [p for p in rows if p.target_date >= today]
    if not rows:
        return []

    seller_ids = {int(p.seller_id) for p in rows}
    muted = {
        int(uid) for uid in db.scalars(
            select(User.id).where(
                User.id.in_(seller_ids), User.sale_watch_enabled.is_(False)
            )
        )
    }
    if muted:
        logger.info("[sale_plan] %d seller(s) have the watcher switched off", len(muted))
    return [p for p in rows if int(p.seller_id) not in muted]


def close_expired(db: Session, today: date) -> int:
    """目标日期已过的计划收摊。"""
    rows = list(
        db.scalars(
            select(SalePlan).where(
                SalePlan.status == SalePlanStatus.ACTIVE, SalePlan.target_date < today
            )
        )
    )
    for plan in rows:
        plan.status = SalePlanStatus.DONE
    if rows:
        db.commit()
        logger.info("[sale_plan] closed %d plans past their date", len(rows))
    return len(rows)


def run_weekly_pass(db: Session, *, today: date | None = None, force: bool = False) -> dict:
    """一次周扫描。返回 {"checked", "notified", "closed"}。

    **每条计划每周查一次，查的就是 plan agent 的 `peek_plan_demand`**——卖家在对话里
    问「转换器现在什么情况」拿到的是同一个函数的输出，只是这里量的是刚走完的那一
    整周，而不是这周到目前为止。两边共用一份读数和一份判定，卖家在对话里看到的
    「已经过线」和收到的邮件就不会对不上。这里不过模型：判据是固定的数字比较，
    让模型来决定发不发，只会多出一个每周都可能答错的地方。

    节奏：从 `watch_from`（目标日期前 `sale_plan_watch_weeks` 周）那天起，**每个
    自然周一次**，到目标日期为止。没有「只在周一跑」这道闸门——以前有，进程在
    某个周一整天没起来，那一周就永远丢了。现在的去重是「这一周的快照写过没有」：
    哪天跑都行，同一周不会跑两遍。

    `force` 只给验收脚本用：它跳过「这一周跑过没有」这道闸门。它**不跳过**触发
    判据——强制跑出来的通知和自然跑出来的是同一批。
    """
    from .plan_agent.schemas import PeekPlanDemandInput
    from .plan_agent.tools import peek_plan_demand   # lazy: plan_agent.tools imports us

    today = today or date.today()
    closed = close_expired(db, today)

    plans = due_plans(db, today)
    week_start = week_start_of(today) - timedelta(days=7)  # 上一整周，已经走完的那周
    checked = notified = 0
    now = utcnow()

    for plan in plans:
        if not force and snapshot_for(db, plan, week_start) is not None:
            continue
        peek = peek_plan_demand(
            db, int(plan.seller_id), PeekPlanDemandInput(plan_id=plan.id), week=week_start
        )
        record_snapshot(
            db, plan, week_start, values=peek.current.model_dump(exclude={"week_start"})
        )
        plan.last_checked_at = now
        checked += 1
        for trigger in peek.triggers:
            _notify(db, plan, trigger.kind, trigger.detail, week_start)
            notified += 1
    db.commit()
    if checked:
        logger.info(
            "[sale_plan] weekly pass: %d plans checked, %d notifications", checked, notified
        )
    return {"checked": checked, "notified": notified, "closed": closed}

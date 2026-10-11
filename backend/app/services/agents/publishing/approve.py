"""Approving a drafted listing: re-check, then persist.

The draft the seller approves is not the draft the model produced — they can
edit every text field before pressing the button, which is the point. So the
gate runs *here*, on what was actually submitted, not on what was reviewed
earlier. Skipping that would make the whole compliance step bypassable by
editing after review, which is the one thing a reviewer must not be.

Everything the client sends is treated as a proposal:
* category, condition and pickup location are checked against their whitelists
  rather than trusted;
* seller, status and timestamps are set here and are not part of the payload at
  all — there is no field for a client to put them in;
* a block verdict refuses the write and returns the rules it hit, so the seller
  knows what to change.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from ....config import LOCATION_BY_NAME
from ....models import ItemStatus, User
from ....repositories import StatusLogRepository
from ...item_index import ItemIndexService
from ...text import clip_sentences
from .pipeline import PublishingError, review_compliance
from .rules import publishable_findings
from .schemas import CATEGORIES, CONDITIONS, PublishApproveIn, PublishApproveOut

logger = logging.getLogger(__name__)


def _approval_text(payload: PublishApproveIn) -> str:
    """What the reviewer sees on approval: exactly the copy that would go live."""
    parts = [payload.name, payload.summary, payload.description]
    attrs = [payload.brand, payload.color, payload.material, payload.size,
             payload.category, payload.condition]
    parts.extend(a for a in attrs if a)
    return "\n".join(p for p in parts if p)


class ThreadState(Protocol):
    """How approval reaches the conversation the listing came from.

    The agent graph provides it (`agents.graph.THREAD_STATE`) and the router
    passes it in, so this module never imports the graph — the graph already
    imports the publishing pipeline, and the reverse edge made the two a cycle.
    """

    def read(self, db: Session, thread_id: str) -> dict: ...

    def patch(self, db: Session, thread_id: str, values: dict) -> None: ...


def approve_listing(
    db: Session, index: ItemIndexService, user: User, payload: PublishApproveIn,
    thread: ThreadState,
) -> PublishApproveOut:
    location = LOCATION_BY_NAME.get(payload.pickup_location_name)
    if not location:
        return PublishApproveOut(
            ok=False, message="请从预设地点中选择自提点。"
        )
    if payload.category not in CATEGORIES:
        return PublishApproveOut(ok=False, message="请从列表中选择一个类别。")
    if payload.condition not in CONDITIONS:
        return PublishApproveOut(ok=False, message="请从列表中选择成色。")

    try:
        verdict, findings, _in, _out = review_compliance(
            db, text=_approval_text(payload), photo_path=payload.photo_path
        )
    except PublishingError as exc:
        # Neither approve nor reject on a failed check: both would be a guess
        # about content nobody actually reviewed.
        logger.warning("[publishing] approval re-check failed: %s", exc)
        return PublishApproveOut(
            ok=False,
            message="刚才没能完成最后的内容检查，请再确认一次。",
        )

    # 「描述过于单薄」「没有说明成色」不进这张回执。卖家刚刚在表里把这两件事
    # 办完了——成色是个必选下拉，描述那个框他可以随便改——现在回头告诉他"信息
    # 不足"，指的是一件已经发生过的事。硬禁令不受影响，它们在下面照样拦。
    findings = publishable_findings(findings)

    if verdict == "block":
        return PublishApproveOut(
            ok=False,
            findings=findings,
            message="按现在的写法这件商品不能发布。下面列出了触发的规则。",
        )

    # 链路 2：这次上架是「以后再卖」那句话带来的吗。
    #
    # 答案在**会话状态**里，不在请求体里。`thread_id` 已经在 payload 中，所以服务端
    # 自己去查这个会话被路由去上架时挂起了什么——比加一个客户端字段可靠：那个字段
    # 客户端可以随便填，而这里读的是路由那一刻后端自己写下的事实。
    hold = _hold_after_publish(thread, db, payload.thread_id)
    status = ItemStatus.PAUSED if hold else ItemStatus.SELLING

    summary = payload.summary.strip() or clip_sentences(payload.description, 2)[:120]
    item = index.create_item(
        db,
        seller_id=user.id,
        name=payload.name.strip()[:40],
        description=payload.description.strip(),
        summary=summary[:120],
        attributes={
            "category": payload.category,
            "brand": payload.brand or None,
            "condition": payload.condition,
            "color": payload.color or None,
            "material": payload.material or None,
            "size": payload.size or None,
        },
        price=float(payload.price),
        photo_path=payload.photo_path,
        pickup_location_name=location["name"],
        lat=location["lat"],
        lng=location["lng"],
        pickup_time_start="",
        pickup_time_end="",
        status=status,
    )
    StatusLogRepository(db).append(
        item.id, None, status,
        "listed (held for a sale plan)" if hold else "listed via publishing agent",
    )
    db.commit()
    logger.info(
        "[publishing] listed #%s %s as %s (warnings: %d)",
        item.id, item.name, status, len(findings),
    )

    # 上架这件事到此为止：把会话里属于「正在上架」的那部分状态清掉。放在写库
    # 成功之后，因为在此之前这次上架还可能被合规检查拦下来，而一次被拦下的提交
    # 的草稿箱**不该**清——卖家改完就要接着用它。
    _settle_thread_after_listing(thread, db, payload.thread_id, item)

    if hold:
        # 隐身**必须在回执里说出来**。卖家刚点了「发布」，屏幕上却没有一件在卖的
        # 东西——不解释的话，这看起来像发布失败了。
        message = (
            "已经存进你的库里，但先没有公开——因为你说的是以后再卖。"
            "买家现在搜不到它。回到对话里告诉我大概什么时候卖，我就替你盯着行情；"
            "想现在就卖，在「我的橱窗」里点恢复上架。"
        )
    else:
        message = "Your listing is live."
    if findings:
        message += " A few suggestions are noted below — you can edit the listing any time."
    return PublishApproveOut(ok=True, item_id=item.id, findings=findings, message=message)


#: 上架成功之后要归零的会话状态。
#:
#: 前四个是**草稿箱**：`publishing_node` 只往 `publish_text` 里累加，从来不清
#: （「上架是一场对话，第一条消息很少说全」）。东西已经挂出去了，这段累加就该
#: 结束——不结束的话，卖家发完之后随便说一句「上传好了」，路由器看着这段历史
#: 再把它送进上架流程，旧文本原样重抽一遍，屏幕上出现第二张同一件商品的草稿卡。
#: 实测里那张多出来的杯子草稿就是这么来的。
#:
#: `hold_after_publish` 是这次上架的一次性记号，已经在上面被读掉并用掉了；留着
#: 它，三轮之后一次无关的上架会悄悄变成隐身。
#:
#: `photo_path` 是这一轮的附件。每次 invoke 都会重写它，清一次只是为了让「上架
#: 完成」这个时刻的状态自洽。
#:
#: `last_listed` 是唯一一个被**写进去**而不是清掉的：它记下刚挂出去的是哪一件，
#: 让下一句「上传好了」有东西可以指认（`router.apply_listing_done_gate`）。
#:
#: **不清**挂起的任务（`pending_tasks` / `pending_items` / `original_intent`）：
#: 那正是要在这之后被接回来的东西。清掉它，等于刚上架完就把卖家最初的诉求扔了。
_LISTING_DONE_PATCH: dict[str, object] = {
    "publish_text": "",
    "publish_photo": "",
    "publish_draft": {},
    "publish_findings": [],
    "publish_verdict": "",
    "photo_path": "",
    "hold_after_publish": False,
}


def _settle_thread_after_listing(thread: ThreadState, db: Session, thread_id: str, item) -> None:
    """东西已经挂出去了——把这个会话里「正在上架」的那部分状态归零。

    写回 checkpoint，而不是等下一轮某个节点顺手覆盖：**上架完成是一个事件**，
    而它发生在图之外（卖家点的是发布按钮，走的是这个 REST 端点，不是一条消息）。
    图这边唯一能知道它发生过的方式，就是这里主动写一次。

    同时记下**挂出去的是哪一件**。上架有终点，而以前系统里没有任何一个地方承认过
    这件事：卖家再说一句「上传好了」，路由器照样把它送回上架流程，拿这四个字去跑
    抽取，再问一遍成色——他照做，库里就多一件一模一样的东西。`last_listed` 就是
    那个终点的凭据，`router.apply_listing_done_gate` 读的就是它。

    失败不抛。这一步是收尾，商品已经写进库了；一个清不掉的草稿箱会让下一轮多出
    一张草稿卡，而一个因为收尾失败就 500 掉的发布，会让卖家以为东西没挂上去。
    """
    patch = dict(_LISTING_DONE_PATCH)
    patch["last_listed"] = {
        "item_id": int(item.id),
        "name": (item.name or "")[:60],
        "at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    try:
        thread.patch(db, thread_id, patch)
    except Exception:  # noqa: BLE001  收尾失败不该回滚一次成功的上架
        logger.warning("[publishing] cannot settle thread %s after listing", thread_id)


def _hold_after_publish(thread: ThreadState, db: Session, thread_id: str) -> bool:
    """这个会话被路由去上架的时候，是不是因为卖家说了「以后再卖」。

    读 LangGraph 的 checkpoint，读不到就当成否。这里刻意**失败取假**：读不到状态
    时把商品正常挂出去，最坏是卖家要自己去暂停一次；反过来默认隐身，则是一件他
    以为已经在卖的东西谁也搜不到，而他不会收到任何提示。
    """
    try:
        values = thread.read(db, thread_id)
    except Exception:  # noqa: BLE001  线程不存在 / checkpoint 损坏都不该挡住发布
        logger.warning("[publishing] cannot read thread %s state", thread_id)
        return False
    return bool(values.get("hold_after_publish"))

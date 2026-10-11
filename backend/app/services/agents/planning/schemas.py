"""出售计划 agent 的工具契约（调用点 G）。

和 `sales/schemas.py` 遵循同一条规矩：**凡是驱动写操作或者会被当成承诺
读的值，形状在这一层就锁死**，不靠提示词约束。

这个 agent 和销售顾问有一个关键区别：它**会写**。销售顾问的四个工具全是只读的，
模型说错了顶多是答得不好；这里的 `create_sale_plan` / `cancel_sale_plan` /
`set_plan_watch` 会改数据库，而且改的是「几周后会不会有一封邮件发出去」这件事。
所以这一层多做三件事：

* **日期不接受模型的换算结果。** `target_phrase` 收的是**卖家自己的原话**，
  由后端的 `intent_rules.parse_target_date` 换算；`target_date` 只是那套词法规则
  没覆盖到时的兜底，并且要过未来性和地平线校验。一个猜错的日期和一个猜对的日期
  在回复里长得一模一样，所以这个值不能由模型自由填。
* **承诺文案由服务端生成。** 每个写工具的输出都带一份 `commitment`——系统真的会
  做什么、不会做什么，逐条写死。模型可以转述，但界面渲染的是这一份。能力边界
  不是每轮重新组织语言的地方（3.3.4 的教训）。
* **plan_id 一律回解。** 模型可以说出任何整数，只有属于这位卖家的才解析得到行，
  和 `resolve_owned_item` 是同一条闸门。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 计划最远能排到多久以后。再远几乎都是解析错误（把「2 号」读成了明年的 2 号），
#: 而不是一个真实的计划。和 `agents/router.MAX_PLAN_DAYS` 是同一个数字，两边
#: 都校验：路由抽的槽位和模型填的工具参数是两个入口。
MAX_PLAN_DAYS = 400

#: 一位卖家最多同时盯这么多条。上限的意义不是省资源，是让「我全部库存都等学期末
#: 再卖」这种输入不会变成三十封邮件。
MAX_ACTIVE_PLANS_PER_SELLER = 10


class SalePlanView(BaseModel):
    """一条计划，卖家视角。模型和前端读的是同一份。

    `date_basis` 一起带出来不是冗余：这个日期有可能是系统从「12 月放假前」推断的，
    而推断出来的日期和卖家亲口说的日期在界面上长得一模一样。把依据摆出来，错的
    那个才有机会被改掉。
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: int
    item_name: str = Field(max_length=120)
    category: str | None = None
    #: 只有当计划挂在一件**已经在售**的商品上时才有值。绝大多数计划没有——
    #: 这件东西按定义还没上架。
    item_id: int | None = None
    target_date: str                      # YYYY-MM-DD
    watch_from: str                       # YYYY-MM-DD
    #: explicit_date / explicit_month / semester / relative / fuzzy / model / manual
    date_basis: str = ""
    status: str
    #: 这一条现在盯不盯。总开关关掉时，这里仍然是 true——两个开关是独立的，
    #: 界面要能分辨「我把这条停了」和「我把整个功能停了」。
    watch_enabled: bool = True
    #: 这条计划有没有**把商品收起来等**（商品被置为隐身：在库中、买家搜不到）。
    #: 必须透出来：没有它，界面和模型都分不出「为这条计划收起来的」和「卖家自己
    #: 手动暂停的」——而前者恢复上架会顺带作废计划，后者不会。
    hold_item: bool = False
    notified_count: int = 0
    last_checked_at: str = ""
    #: 还有多少天开始盯 / 还有多少天到期。负数表示已经开始 / 已经过期。
    days_until_watch: int = 0
    days_until_target: int = 0
    #: 这一条现在是不是真的在被扫描（活着 + 自己开着 + 总开关开着 + 进了窗口）。
    #: 单独算出来，是因为它由四个条件合成，让模型自己推会推错。
    watching_now: bool = False


class PlanActionResult(BaseModel):
    """一次写操作的回执。

    `commitment` 是**服务端生成的承诺清单**，模型不能改也不能编。写工具的输出里
    带着它，是为了让「系统会做什么」这件事只有一个来源——提示词里那句「照抄」是
    第二道保险，界面直接渲染这一份才是第一道。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["created", "updated", "cancelled", "watch_changed", "noop"]
    plan: SalePlanView | None = None
    #: 一次操作影响到的多条计划（总开关切换时是全部）。
    affected: list[SalePlanView] = Field(default_factory=list)
    #: 服务端写死的承诺/边界文案，逐条。
    commitment: list[str] = Field(default_factory=list)
    #: 这次操作有什么要提醒卖家的（日期是推断的、类别没推断出来、已达上限……）。
    caveats: list[str] = Field(default_factory=list)
    #: 总开关的当前状态，每次写操作都回带——它决定了上面那些承诺算不算数。
    master_enabled: bool = True


# ============================================================
# Tool 1: list_sale_plans
# ============================================================
class ListSalePlansInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 已撤销 / 已到期的计划默认不返回。卖家问「我都排了什么」时想看的是还在生效的。
    include_inactive: bool = False


class ListSalePlansOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_active: int
    master_enabled: bool
    plans: list[SalePlanView] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


# ============================================================
# Tool 2 / 3: create_sale_plan · update_sale_plan
# ============================================================
class _DateFields(BaseModel):
    """两个字段表达同一件事，优先级不同——这是本模块最重要的一个约定。

    `target_phrase` 是**卖家自己的原话**（「12 月放假前」「等学期末」），由后端
    换算。`target_date` 是那套词法规则没覆盖到时的兜底，模型自己算出来的。

    两个都给的时候 `target_phrase` 赢。理由和路由器里 `category` 的处理一样：
    凡是后端算得出来的值就不问模型，问了也只用来兜底——模型算日期不是不会，是
    **每次不一定一样**，而这个值决定了几周后有没有一封邮件发出去。
    """

    model_config = ConfigDict(extra="forbid")

    target_phrase: str = Field(default="", max_length=60)
    target_date: str = Field(default="", max_length=10)


class CreateSalePlanInput(_DateFields):
    #: 卖家自己的说法（「转换器」），不要改写成规范商品名——周扫描的关键词口径
    #: 就是拿这个去和买家的搜索措辞比对的，改写会让它对不上。
    item_name: str = Field(min_length=1, max_length=120)
    #: 卖家原话，留作回执里复述。
    note: str = Field(default="", max_length=300)


class UpdateSalePlanInput(_DateFields):
    plan_id: int = Field(ge=1)


# ============================================================
# Tool 4: cancel_sale_plan
# ============================================================
class CancelSalePlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int = Field(ge=1)


# ============================================================
# Tool 5: set_plan_watch  —— 自然语言那一路的开关
# ============================================================
class SetPlanWatchInput(BaseModel):
    """开关。`plan_id` 留空表示动总开关。

    界面上那个按钮走的是 REST 端点，这个工具是它的自然语言孪生兄弟——两条路
    **落到同一个字段上**，不是各记各的。否则卖家用嘴关掉之后，界面上的开关还
    亮着，而那个亮着的开关说的是假话。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    #: 只停某一条时给 plan_id；留空 = 总开关。
    plan_id: int | None = Field(default=None, ge=1)


# ============================================================
# Tool 6: peek_plan_demand
# ============================================================
class WeekReading(BaseModel):
    """某一周的读数。三个口径分开，不合成分数——和 `analyze_demand` 同一条规矩：
    合成之后卖家就看不出该对哪一个采取行动。"""

    model_config = ConfigDict(extra="forbid")

    week_start: str
    #: 类别口径：这一周该类别下的预订数。类别推断不出来时恒为 0，`source` 会说明。
    reserve_count: int = 0
    #: 类别口径：去重买家数 / 搜索事件数。
    people: int = 0
    events: int = 0
    #: 关键词口径：措辞真的和这件东西对得上的人数。更小，也更真。
    matching_people: int = 0
    #: `analyze_demand`（计划挂在在售商品上时）还是 `category_keyword`（没上架时）。
    source: str = ""
    #: 后端算出来的方向，只有走 analyze_demand 那条路才有。
    trend: str = ""


class PeekPlanDemandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int = Field(ge=1)


class PlanTrigger(BaseModel):
    """一条过了线的判据。周扫描照这个发，对话里照这个说——同一份。"""

    model_config = ConfigDict(extra="forbid")

    #: `sale_plan_hot`（预订过线）/ `sale_plan_surge`（相邻两周需求增长过线）。
    kind: str
    #: 过线会走哪些渠道：热度是站内 + 邮件，增长只有邮件。
    channels: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class PeekPlanDemandOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    item_name: str
    category: str | None = None
    #: 最近几周，旧的在前。空列表意味着还没到观察窗口、或者还没跑过周扫描——
    #: 那和「没有需求」是两件事，`caveats` 会说明是哪一种。
    weeks: list[WeekReading] = Field(default_factory=list)
    #: 当前这一周的实时读数（不落库，只是给卖家看看现在什么情况）。
    current: WeekReading | None = None
    #: 按现在的数字，两条判据分别处在什么位置。服务端算，不让模型自己比大小。
    would_notify: list[str] = Field(default_factory=list)
    #: `current` 这份读数按判据**已经过线**的那几条。空 = 这一周不会发任何东西。
    triggers: list[PlanTrigger] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


# ============================================================
# Orchestrator final output
# ============================================================
class PlanAgentFinal(BaseModel):
    """模型最后一轮必须输出的 JSON。

    计划卡片**不在这里**——它们直接来自工具输出，所以模型没有渠道编造一条不存在
    的计划，也没有渠道把承诺写成系统做不到的样子。
    """

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=1200)

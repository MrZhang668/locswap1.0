"""调用点 H：延后出售识别。

「我想在 2 个月后出售这支笔」——这层意思以前由 `intent_plan` 里的两条正则判：
未来时间锚点 + 卖出动词，两半都命中才算。那套规则的好处是每次都一样，坏处是
它认得的只有**写在表里的那几个词**：「出售」不在卖出动词表里，「2 个月后」不在
时间锚点表里，于是这句话两半都落空，商品被当场挂上了货架——和卖家说的话正好
相反。这不是规则写漏了一个词，是一个封闭词表对着自然语言的必然结果。

所以判断本身交给模型，**判断的后果仍然留在服务端**。这条分界是这个模块存在的
理由，也是它和「把这件事写进路由提示词里」的区别：

* 模型只回答一个问题——这句话里有没有「要卖，但不是现在」，时间是哪一天。
  它不选 agent，不填场景，不写回复。一份小 schema，可以单独跑、单独评。
* 它的输出进的是 `hints["defer_sale_intent"]`，也就是原来那两条正则的位置。
  下游一个字没改：`router.apply_defer_sale` 照样把这一轮从别的专家手里拉回
  `defer_sale`，链路 2 照样在商品不在库时先上架再隐身，`hold_after_publish`
  照样是那个一次性记号。判断换了来源，**路由的确定性没有换**。
* 日期仍然是后端算的优先。模型给的日期只在那套词法规则算不出来时才用，而且
  要过和路由槽位同一道校验（将来、一年以内）。一个猜错的日期和一个猜对的日期
  在回复里长得一模一样。

调用失败时退回原来那两条正则，而不是退回「不是延后」：没有 key 的环境、超时的
网络、限流的账号，都不该让一个卖家说过的话凭空消失。词表窄归窄，它认得的那部分
仍然是对的。
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...config import settings
from ..prompts import load_prompt
from . import intent_plan
from .lc_llm import CALL_SITE_DEFER_INTENT, build_chat_model, record_usage, router_model_name

logger = logging.getLogger(__name__)

#: 计划最远能排到多久以后。和 `router.MAX_PLAN_DAYS` 同一个口径：再远的日期几乎
#: 都是解析错误，不是计划。
MAX_PLAN_DAYS = 400


class DeferReading(BaseModel):
    """模型这一轮对「以后再卖」的读法，经服务端校验之后的样子。

    `source` 记的是这份读法从哪来——`"model"` 是调用点 H 的输出，`"lexical"` 是
    调用失败后退回的正则。跑批要能把两者分开看：一批 `lexical` 说明那一轮根本
    没调到模型，而不是模型判错了。
    """

    model_config = ConfigDict(extra="forbid")

    is_defer: bool = False
    wants_reminder: bool = False
    #: 卖家原话里表示时间的那几个字，用来做后端的确定性换算。
    time_phrase: str = ""
    sell_action: str = ""
    #: 模型算出的日期，已经过校验；算不出来或者被丢弃时是空串。
    target_date: str = ""
    reason: str = ""
    source: str = "lexical"
    input_tokens: int = 0
    output_tokens: int = 0
    #: 服务端丢掉的字段，进日志。空列表是正常情况。
    dropped: list[str] = Field(default_factory=list)


class _DeferVerdict(BaseModel):
    """模型的出参。字段少是故意的——这一次调用只回答一个问题。"""

    model_config = ConfigDict(extra="forbid")

    is_defer: bool
    wants_reminder: bool = False
    time_phrase: str = ""
    sell_action: str = ""
    target_date: str = ""
    reason: str = ""


def _lexical(message: str, *, dropped: list[str] | None = None) -> DeferReading:
    """原来那两条正则。现在只在这里出现——它是兜底，不是主判据。"""
    return DeferReading(
        is_defer=intent_plan.defer_sale_intent(message),
        wants_reminder=intent_plan.wants_reminder(message),
        source="lexical",
        dropped=list(dropped or []),
    )


def _plausible_date(raw: str) -> bool:
    """将来的、一年以内的日期。和 `router._plausible_date` 是同一条规矩。"""
    try:
        parsed = date.fromisoformat(raw.strip())
    except (TypeError, ValueError):
        return False
    today = date.today()
    return today < parsed <= today + timedelta(days=MAX_PLAN_DAYS)


def _quoted(fragment: str, message: str) -> bool:
    """模型抄回来的这几个字，确实是卖家写过的。

    抄错的代价不在这一行：`time_phrase` 会被原样送进 `parse_target_date` 和计划
    工具的 `target_phrase`，一个卖家没说过的时间说法会在那里换算出一个他没定过的
    日期。宽松一点比对（去掉空白）是因为模型偶尔会在中英文之间补一个空格。
    """
    clean = fragment.strip().replace(" ", "")
    return bool(clean) and clean in (message or "").replace(" ", "")


def _sanitize(verdict: _DeferVerdict, message: str) -> DeferReading:
    """模型说了什么 → 服务端愿意采信的部分。

    三件事在这里定，都不回头问模型：引文必须是卖家的原话，日期必须落在可能的
    区间里，两者不合格都是**丢弃，不是修正**——修正过的值看起来和真值一模一样。
    """
    dropped: list[str] = []

    time_phrase = verdict.time_phrase.strip()[:60]
    if time_phrase and not _quoted(time_phrase, message):
        dropped.append(f"time_phrase={time_phrase!r} (卖家没说过这几个字)")
        time_phrase = ""

    sell_action = verdict.sell_action.strip()[:30]
    if sell_action and not _quoted(sell_action, message):
        dropped.append(f"sell_action={sell_action!r} (卖家没说过这几个字)")
        sell_action = ""

    target_date = verdict.target_date.strip()
    if target_date and not _plausible_date(target_date):
        # 丢掉就行，不必因此否定整个判断：「以后再卖」成立和「哪一天」算得出来
        # 是两件事，后者缺了有出口——计划节点会问他一句。
        dropped.append(f"target_date={target_date!r}")
        target_date = ""

    return DeferReading(
        is_defer=bool(verdict.is_defer),
        wants_reminder=bool(verdict.wants_reminder),
        time_phrase=time_phrase,
        sell_action=sell_action,
        target_date=target_date,
        reason=verdict.reason.strip()[:200],
        source="model",
        dropped=dropped,
    )


def classify(
    message: str,
    *,
    history: list[dict[str, str]] | None = None,
    has_photo: bool = False,
) -> DeferReading:
    """一次调用点 H。**从不抛异常**——失败退回词法判定。

    上下文给得比路由少：这里不需要知道卖家有多少件在售商品，只需要知道他这一轮
    说了什么、上一轮说了什么（「那就等学期末吧」承接的是上一句）、以及这一轮有没有
    带照片。多给的每一条都是让这个判断被别的东西带偏的机会。
    """
    text = (message or "").strip()
    if not text:
        return _lexical(message)
    if not settings.defer_intent_model_enabled:
        return _lexical(message, dropped=["disabled"])

    model = router_model_name()
    try:
        chat = build_chat_model(
            model=model,
            temperature=settings.llm_temperature_defer_intent,
            max_tokens=settings.llm_max_tokens_defer_intent,
        )
        structured = chat.with_structured_output(_DeferVerdict, include_raw=True)
        result = structured.invoke(
            [
                ("system", load_prompt("defer_intent")),
                (
                    "human",
                    f"<today>{date.today().isoformat()}</today>\n"
                    f"<has_photo>{json.dumps(bool(has_photo))}</has_photo>\n"
                    f"<recent_turns>\n{_turns_block(history or [])}\n</recent_turns>\n"
                    f"<current_message>{text}</current_message>",
                ),
            ]
        )
    except Exception as exc:  # noqa: BLE001  网络 / 鉴权 / provider 的各种脾气
        logger.warning("[defer-intent] call failed (%s); falling back to lexical",
                       type(exc).__name__)
        return _lexical(message, dropped=[f"call_failed={type(exc).__name__}"])

    raw = result.get("raw") if isinstance(result, dict) else None
    in_tok = out_tok = 0
    if raw is not None:
        in_tok, out_tok = record_usage(CALL_SITE_DEFER_INTENT, model, raw)

    verdict = result.get("parsed") if isinstance(result, dict) else None
    if verdict is None:
        parse_error = result.get("parsing_error") if isinstance(result, dict) else None
        logger.warning("[defer-intent] unparseable verdict: %s", str(parse_error)[:160])
        out = _lexical(message, dropped=[f"unparseable={str(parse_error)[:80]}"])
        out.input_tokens, out.output_tokens = in_tok, out_tok
        return out

    reading = _sanitize(verdict, text)
    reading.input_tokens, reading.output_tokens = in_tok, out_tok
    if reading.dropped:
        logger.warning("[defer-intent] dropped unusable values: %s", "; ".join(reading.dropped))
    logger.info(
        "[defer-intent] is_defer=%s reminder=%s date=%r phrase=%r reason=%r",
        reading.is_defer, reading.wants_reminder,
        reading.target_date, reading.time_phrase, reading.reason,
    )
    return reading


def _turns_block(history: list[dict[str, str]]) -> str:
    if not history:
        return "（没有更早的对话）"
    return "\n".join(f"{t['role']}: {t['content'][:200]}" for t in history[-4:])


def apply(hints: dict[str, Any], reading: DeferReading) -> dict[str, Any]:
    """把这份读法盖到 `hints` 上，盖在原来那两条正则的位置。

    就地改而不是返回一份新的，是为了让「谁改了这个值」只有一个答案：路由节点拿到
    的 `hints` 自始至终是同一个对象，改动发生在它被交给 `router.route` 之前。

    **两处联动**，因为它们本来就读的是同一个判断：

    * `plan_action_intent` —— 「说要排一条计划」这个动作以前也是那两条正则推出来
      的（`defer_sale_intent or wants_reminder` → `"create"`）。判断换了来源，这里
      跟着换，否则计划 agent 会收到一个空的 `expected_action`，而那正是「模型说
      好的、库里没有」那类失败的入口。开关 / 撤销 / 列表三个动作**不跟着换**：
      它们是对已有计划的管理指令，有自己的词表和自己的闸门。
    * `defer_target_date` —— 模型算出来的日期，作为后端换算失败时的下一顺位。
      它不覆盖 `parse_target_date`：能算的仍然不问模型。
    """
    hints["defer_sale_intent"] = reading.is_defer
    hints["wants_reminder"] = reading.wants_reminder
    hints["defer_source"] = reading.source
    hints["defer_reason"] = reading.reason
    hints["defer_time_phrase"] = reading.time_phrase
    hints["defer_target_date"] = reading.target_date

    if not hints.get("plan_action_intent") and (reading.is_defer or reading.wants_reminder):
        hints["plan_action_intent"] = "create"
    elif hints.get("plan_action_intent") == "create" and not (
        reading.is_defer or reading.wants_reminder
    ):
        # 词法判定说「排一条」，模型说这一轮不是延后也不是提醒——以模型为准，这
        # 正是这次改造要的那个方向。开关 / 撤销 / 列表不会走到这一支。
        hints["plan_action_intent"] = ""
    return hints

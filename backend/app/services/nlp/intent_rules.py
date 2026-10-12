"""Pre-router intent completion: bind the entity, then name the plan.

The failure this exists for: "帮我卖微波炉" / "我想把书架出了" / "笔记本电脑找谁聊？"
是一个复合任务（确认在库 → 需求 + 定价 → 匹配买家），但字面上既没有动作词也没有
完整槽位。路由器面对它只能二选一：扣住消息去 `clarify`，或者当成新上架丢给
`publishing_agent` 然后停在第一跳。两条路都不是卖家要的东西。

所需的工具（`list_my_items` / `analyze_demand` / `analyze_sold_prices` /
`find_potential_buyers`）在别的子类里都被正确调用过——缺的不是能力，是**改写与
规划**。这个模块补的就是那一层，而且刻意做成**确定性的、不花一次模型调用**：

* **实体绑定在后端做，不问模型。** 卖家有哪几件在售商品是一条 SQL，把消息里的名词
  对上其中一件同样是一次分词比对。让模型"判断"它看得见的列表，成本是一次额外调用
  加一次幻觉机会；后端算出来的 `name_matches` 是事实，路由器只是读它。
* **在库与否决定去哪个专家。** "帮我卖 X"，X 已经在售 → 这是销售问题（怎么卖出去），
  不是上架问题；X 不在库 → 才是 `publishing_agent`。这个判断以前落在模型的直觉上，
  现在落在一次名称匹配上。
* **计划是固定的，不是模型现编的。** 泛卖出意图展开成同一套三步（需求 / 定价 /
  买家），由 `scenario="sell_through"` 承载。固定计划可以被评测、被复现，模型每轮
  重新发明的计划不能。

匹配用的是检索层那套分词（`services.text`），不是子串包含：子串会让"桌"命中"桌面
麦克风"、"架"命中"床架"，这在语料里被实测过。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from ..text import raw_tokens

#: 泛卖出意图的触发词。这些表达的共同点是**说了要卖、没说要什么分析**——
#: 卖家心里是"帮我把这件东西处理掉"，落到系统里却需要三个工具。
#: 只匹配动作词本身；商品是谁由 `name_matches` 决定，不在这里正则里猜。
_BROAD_SELL_RE = re.compile(
    r"帮我卖|帮忙卖|想卖|要卖|卖掉|卖出去|想出售|要出售|出售掉|转让|出掉|出了|出手|"
    r"处理掉|清掉|清一下|转手|转出去|脱手|"
    r"找谁聊|找谁买|谁会要|谁要|怎么卖|怎样卖|如何卖|卖不动|卖不出去|"
    r"能不能卖|好卖吗|该怎么处理"
)

#: 状态变更（撤销）意图。和泛卖出意图相反——这类消息意图非常明确，缺的是落地能力。
#: 以前系统的反应是转去追问品牌、型号、尺码，也就是"识别到了却无处落地"。
_PAUSE_RE = re.compile(
    r"下架|不卖了|先不卖|别卖了|不想卖了|撤了|撤下|先撤|撤销|"
    r"停一下|暂停|先停|停售|下掉|摘下来|收回来|不挂了|取消上架"
)
_RESUME_RE = re.compile(
    r"重新上架|恢复上架|恢复销售|再挂出来|重新挂|挂回来|恢复一下|再上架|继续卖|接着卖"
)
#: 永久删除只认明确说了"删"的表达。"下架"在卖家嘴里一般是"先收起来"，
#: 把它解释成不可逆的删除是拿卖家的商品赌一个词义。
_DELETE_RE = re.compile(r"删除|删掉|彻底删|永久删|删了吧")

#: 指代词。出现其中之一而槽位为空，说明这一轮承接的是上一轮的结果集，
#: 不是一个新对象——`last_context` 该被继承而不是重新检索。
_REFERENT_RE = re.compile(
    r"这些|那些|这几个|那几个|他们|她们|这个人|那个人|这位|那位|"
    r"第\s*[一二三四五六七八九十1-9]\s*个|第\s*[一二三四五六七八九十1-9]\s*位|"
    r"上面|刚才|刚刚|前面那|它们|这单|这条"
)

#: 会话边界之外的诉求。命中只是**提示**路由器先做边界判断，不直接改路由——
#: "帮我买个显示器架"里的"买"和"有人想买吗"里的"买"是两回事，硬规则会误伤。
_OUT_OF_SCOPE_RE = re.compile(
    r"客服|退款|退货|退了|投诉|申诉|"
    r"帮我买|帮我下单|我想买|代买|"
    r"别的平台|其他平台|闲鱼|淘宝|京东|微信群|朋友圈|"
    r"天气|几点|今天星期"
)

#: 确认类输入：对上一轮问题说「是」。它携带的信息是**上一轮的待定槽位成立**，
#: 不是「没有信息」。以前这类消息被当成空输入，于是同一个问题被重问一遍
#: （归因三 3.4.2，4/4 全挂）。
#:
#: 分成「整句就是一个是」和「句中出现明确指认」两套判定，因为两者的误伤方式不同：
#: 「对」单独成句一定是确认，而「对了，帮我看看价格」里的「对了」是话题转折词，
#: 正则会误伤它——所以短应答走精确集合，长句只认「就那个 / 就是它 / 没错」这类
#: 无法当转折词用的说法。
_CONFIRM_EXACT = frozenset({
    "对", "对的", "对对", "对呀", "对啊", "是", "是的", "是啊", "嗯", "嗯嗯", "嗯呢",
    "没错", "正是", "就是", "好", "好的", "好呀", "行", "可以", "确认", "确定",
    "yes", "yep", "yeah", "ok", "okay", "sure", "right",
})
_CONFIRM_RE = re.compile(r"就(是)?(那|这)个|就是它|就它|就那件|就这件|没错|正是它|确实是")

#: 授权类输入：把决策权交给系统。和确认类的区别是它**不指向某个具体槽位**，
#: 而是说「你别问了，你定」。正确的反应是给出默认方案并执行，不是把同一组选项
#: 再摆一遍（归因三 3.4.1，4/4 全挂）。
_AUTHORIZE_EXACT = frozenset({
    "都行", "都可以", "都要", "都做", "随便", "随你", "随意", "你决定", "你定",
    "你看着办", "你看着来", "你来定", "听你的", "无所谓", "你说了算", "都一样",
})
_AUTHORIZE_RE = re.compile(
    r"都行|都可以|都要|都做|两个都|全都|随便|随你|随意|"
    r"你决定|你定|你来定|你看着(办|来)|听你的|你说了算|无所谓|哪(个|种)都|由你"
)

#: 交接完成信号：卖家回来说「上传好了」。这条消息本身**不含任何意图**——
#: 它的全部意义是「上一跳的前置条件满足了，继续执行原任务」。没有会话级任务
#: 队列时它只能被读成一句空话，于是系统反问「你想做什么」（归因一 3.2.1，5/5 全挂）。
#:
#: 「好了」单独看太宽（可能是「行了别说了」），所以它只在确实有挂起任务时才被采信
#: —— 判定权在 `handoff.resume_target`，这里只负责认出字面。
_HANDOFF_DONE_RE = re.compile(
    r"上传(好|完|完成)了?|传(好|完)了|已上传|上传成功|上传了|"
    r"发(好|完|出去)了|发布(好|完)了?|挂(好|上去|出去)了|上架(好|完)了?|"
    r"(都|全|已经)(弄|搞|传|发|上|做|填)(好|完)了?|弄好了|搞定了?|填好了|写好了|"
    r"^好了$|^好啦$|^完成了?$|^搞定$"
)

#: 多商品信号。「都弄好了」和「弄好了」差一个字，差的是一件还是两件——
#: 3.2.2 的 5 条全部是系统只承接了最后一件（或者一件都没接）。
_MULTI_ITEM_RE = re.compile(
    r"都(弄|搞|传|发|上|做|填|好|可以)|全(部|都)|两(件|个|样)|三(件|个|样)|"
    r"几(件|个|样)|所有|这些|它们|一起"
)

#: 指代一件**商品**的说法。和上面的 `_REFERENT_RE` 分开，因为两者的候选集完全
#: 不同：「第 2 个」「这些买家」指向的是上一轮那张**结果**清单里的某一项，而
#: 「那个再看一下」「回到前面那个」指向的是这个会话里谈过的某件**商品**。
#:
#: 混成一个信号的代价是双向的：一个卡片序号会触发一次「你说的是哪件商品」，
#: 而一个真正的商品指代会被当成结果指代直接绑到上一轮，正是 6.2.1 的形状。
#:
#: 写成显式枚举而不是「指示代词 + 量词」的通配，是因为中文里「这个」后面跟什么
#: 决定了它指的是什么——「这个价」「这个人」「这条」都不是商品。
#:
#: 「回到 / 返回」后面的成分**不能全是可选的**：那样写「（返回对话页）」「退回款项」
#: 都会命中，而 805 条真实 query 里正好有 15 条前端状态恢复用例长这样。所以时间词
#: 和指示代词必须至少出现一个。
_ITEM_REFERENT_RE = re.compile(
    r"(回到|回去|退回|返回|说回)(前面|之前|刚才|刚刚|上面|上一个|上一件)(那|这)?(个|件|款)?|"
    r"(回到|回去|说回)(那|这)(个|件|款)|"
    r"(前面|之前|刚才|刚刚|上面|先前|早前)(那|这)(个|件|款)|"
    r"(接着|继续|还是|再)(说|看|聊|弄|做)?(刚才|之前|前面|上面)(那|这)?(个|件|款|的)|"
    r"(那|这)(个|件|款)(再|又|还|怎么|如何|多少|呢)|"
    r"^(那|这)(个|件|款)(东西|商品)?[?？。，,]?$"
)

#: 商品指代里**明确往回指**的那一类。「回到前面那个」「之前那件」说的是
#: 「不是最近这件」，所以即便只有两个候选，选哪一个也不是靠「最近优先」能定的。
_BACKWARD_REF_RE = re.compile(r"(回到|回去|退回|返回|说回)|(前面|之前|先前|早前)(那|这)")

#: 指示代词后面跟这些词时，它指的不是商品。判定在 `item_referent` 里做，
#: 因为「这个价」和「这个」差的是两个字，而它们要走完全不同的两条路。
_NOT_ITEM_AFTER_RE = re.compile(
    r"(那|这)(个|条|位|单|张|笔)?(价|价格|钱|数|区间|范围|报价|人|买家|客户|"
    r"需求|求购|订单|消息|时间|category|类)"
)

#: 「两个都要」「这两件一起看」——卖家明说了要处理多件。和 `_MULTI_ITEM_RE`
#: （交接场景的「都弄好了」）分开：那条判的是「上传完成了几件」，这条判的是
#: 「这一轮要分析几件」，误用任何一条都会把另一条的语义带进来。
#:
#: 「都」单独出现太宽（「都行」是授权、「都弄好了」是交接），所以必须配一个
#: 数量词或者一个明确的处理动词。
_MULTI_ENTITY_RE = re.compile(
    r"(两|二|三|几|多)(个|件|款|样)(都|一起|全)|"
    r"(这|那)(两|三|几)(个|件|款|样)|"
    r"(都|全部|全都|一起|一并|逐个|逐件|挨个)(要|看|分析|处理|弄|做|来|算|查)|"
    r"(两|三)(个|件)(都|全)|"
    r"^(两|三|几)(个|件|款)都"
)

#: 数量词挂在**人**身上时，说的是上一轮那批买家，不是卖家的几件商品。
#: 「这几个人靠谱吗」「把这几个买家的手机号给我」都命中上面那条，而后者还是一条
#: 隐私拒绝用例——把它读成「分析这几件商品」，拒绝就不会发生。
_MULTI_ENTITY_NOT_RE = re.compile(r"(两|三|几|这|那)\s*(个|位|件)?\s*(人|买家|客户|同学|学生)")

#: 「那按这个价能找到人吗」——「这个价」指的是**上一轮给出的那个结论**，
#: 不是商品的当前标价。6.1.2 的失败全在这两者之间：路由对、工具对，只有取值
#: 悄悄从「上一轮的成交价区间」换成了「listing.price」，而输出读起来完全正常。
_PRICE_REF_RE = re.compile(
    r"(这|那|该|此)(个)?(价|价格|价位|报价|区间|范围|数|水平)|"
    r"按(这|那)(个)?(价|数|区间)|"
    r"(刚才|上面|前面|你说)(的|那)?(价|价格|区间|数)"
)

#: 「第 2 个是什么情况」——序号指向上一轮那张清单的第 N 项。
_ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十]|\d{1,2})\s*(个|位|条|张|件|名)")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

#: 未来时间锚点。延后出售意图的一半——另一半是卖出动词。
#: 单独出现不算数：「上个月挂出来的」也有时间词，但它指向过去。
_FUTURE_ANCHOR_RE = re.compile(
    r"放假前|放假后|放假的?时候|学期末|期末|学期结束|开学|寒假|暑假|"
    r"下个?月|下(个)?学期|明年|年底|年前|月底|月末|"
    r"\d{1,2}\s*月(份)?|\d{1,2}\s*(号|日)|"
    r"[\d一两二三四五六七八九十]+\s*个?\s*(个?月|周|星期|礼拜|天)(之?后|以后)|"
    r"过(一|几|段|阵)|一(阵|段)(子|时间)|再过|等(等|一下|一阵|一段|到|以后|之后|忙完)|"
    r"考完|考试完|考完试|忙完这|搬(家|宿舍)前|毕业(前|后)|"
    r"以后再|之后再|晚(点|些)再|迟(点|些)再|回头再"
)

#: 卖出动作词，用于和时间锚点组合判定「以后再卖」。
_SELL_ACTION_RE = re.compile(r"卖|挂|上架|出售|售出|转让|出手|出掉|处理|发布|挂出")

#: 「还得用一阵」这类：说的是**现在还要用**，所以不会现在卖。它和时间锚点是一回事，
#: 单列出来是因为它不含任何卖出动词，靠上面那条组合规则抓不到。
_STILL_USING_RE = re.compile(r"还(得|要|需要|想)用|还在用|用一(阵|段)|用到|暂时还用|现在还用")

#: 主动提醒诉求。系统目前**没有**这个能力就答应下来，是本总类里最贵的一种错
#: （3.3.4）：卖家会按承诺安排自己的时间。认出它是为了给一个明确的答复，
#: 而不是为了让模型即兴发挥。
_REMIND_RE = re.compile(
    r"提醒我|通知我|告诉我一声|跟我说一声|(到时候|届时).{0,4}(叫|喊|告诉|提醒|通知)|"
    r"(涨|起来|有人|合适).{0,8}(告诉|提醒|通知)我|帮我盯|帮我看着|留意一下"
)

#: 「什么时候挂出去最划算」——问的是时机，不是价格也不是买家。它需要的是需求
#: 趋势（analyze_demand 的 trend），实际却经常既不调工具也不说缺数据（3.3.3）。
_TIMING_QUESTION_RE = re.compile(
    r"什么时候(挂|上架|卖|发|出)|何时(挂|上架|卖)|"
    r"(挂|上架|卖|发)(出去)?.{0,4}(最|比较)?(划算|合适|好)|"
    r"(现在|这会儿)(挂|上架|卖).{0,4}(合适|划算|好)吗|时机"
)

#: 强约束槽位的**原文**扫描。模型那一端已经把「-50」归一化成 50 再交出来，所以
#: 服务端的值域校验（`router._sanitize` 里的 `0 < price_hint`）根本没机会看见那个
#: 负号——它看到的是一个完全合法的 50，于是一路走到成交价对比，回复专业、数据翔实、
#: 逻辑自洽，只是卖家说的那句话已经被悄悄改掉了（4.1.5）。要发现这类静默改写，
#: 只能回到卖家的原话上，在模型碰它之前先扫一遍。
_MINUS_CHARS = "-－−﹣‑–—"

#: 明确说了要把价格**定到某个数**的说法。这是唯一一种「这个数字就是价格」可以由
#: 措辞本身断定的场合，所以它不需要别的上下文佐证，0 和超大值也在这里判。
_SET_PRICE_RE = re.compile(
    rf"(?:降到|降至|降成|调到|调成|改到|改成|定在|定价|标价|卖到|售价|要价|报价|加到|涨到|提到)"
    rf"\s*[$＄¥￥]?\s*(?P<num>(?:[{_MINUS_CHARS}]|负)?\s*\d+(?:\.\d+)?)"
)

#: 这条消息确实在谈钱。只有在它成立时，一个落单的负数才被当成价格——「-3 度」
#: 「-5%」不是价格，而误判的代价是凭空多问卖家一句。
_PRICE_CUE_RE = re.compile(
    r"降到|降价|降一点|降点|涨到|涨价|提到|调到|改成|标价|定价|售价|要价|报价|"
    r"价格|多少钱|卖到|卖多少|[$＄]|美元|块钱|[0-9]\s*元|[0-9]\s*块|[0-9]\s*刀"
)

_NEG_NUMBER_RE = re.compile(rf"(?P<sign>[{_MINUS_CHARS}]|负)\s*(?P<num>\d+(?:\.\d+)?)")

#: 价格的值域，和 `router._sanitize` 用的是同一条线。两处必须一致，否则原文这边说
#: 非法、槽位那边放行（或者反过来），卖家会看到两种自相矛盾的反应。
PRICE_MIN = 0.0
PRICE_MAX = 1_000_000.0

#: 「我一共挂了几件东西」——答案的唯一事实来源是 `list_my_items`，不是会话记忆，
#: 也不是 `<my_listings>` 那份可能被截断的便利视图。4.2.3 的三条全部是凭空报了
#: 一个数字，而且同一个问题在不同会话里给出 9 / 9 / 18 三个答案。
#:
#: 刻意写成一组完整句式而不是「计数词 AND 第一人称」的合取：后者会把「帮我给小白板
#: 找几个潜在客户」「公路车给我找几个人」一并吃进来，那是找买家，不是清点库存。
_STATE_QUERY_RE = re.compile(
    r"(我|自己)[^。！？]{0,6}(挂|上架|在售|卖)[^。！？]{0,8}(几|多少|哪些|什么)"
    r"|(我|自己)的?[^。！？]{0,6}(商品|东西|物品|listing)[^。！？]{0,6}(有哪些|哪些|几件|几个|多少|什么)"
    r"|(在售|上架)[^。！？]{0,6}(商品|东西|物品|清单|列表)[^。！？]{0,6}(有哪些|哪些|几件|几个|多少|都有)"
    r"|(哪几件|哪几个|哪些)[^。！？]{0,6}(挂着|挂出|在售|上架|没卖)"
    r"|(我|自己)[^。！？]{0,6}(几|多少)\s*(件|个|样)[^。！？]{0,6}(在售|商品|东西|挂|上架)"
    r"|(列|看看|看下|给我列)[^。！？]{0,8}(在售|上架|我的商品)[^。！？]{0,6}(清单|列表|商品|东西|都有|$)"
    r"|(我|自己)[^。！？]{0,8}(还没卖|没卖掉|没卖出去|剩下几|剩几)"
)

#: 在问市场，不是在问自己的库存。命中它就不是状态查询——「我在售的破壁机有多少人
#: 在找」问的是需求，该走 analyze_demand。
_MARKET_QUESTION_RE = re.compile(
    r"买家|客户|在找|求购|需求|搜索|成交|多少钱|价格|定价|人在|找几个|找个人"
)

#: 「有几个买家在等手账本？」——问的是**可以去联系的人有几个**，这是
#: find_potential_buyers 的口径。被读成 demand 之后，模型拿 analyze_demand 的
#: 「去重搜索人数」当买家数报出去，数字有出处但口径不是卖家问的那一个（4.2.5）。
#:
#: 负向查找排掉「这几个买家」「那几个买家」：那是指代上一轮的结果，不是在问数量。
_BUYER_COUNT_RE = re.compile(r"(?<![这那])(几|多少)\s*(个|位|名)?\s*买家")

#: 学期锚点。校园市场里「学期末」「放假前」是真实存在的固定日期，不是模糊说法——
#: 把它们写成常量，比让模型每次现猜一个日期更可复现。JHU 的学期节奏：
#: 秋季学期 8 月底开学、12 月中旬考完；春季学期 1 月下旬开学、5 月中旬考完。
_SEMESTER_ANCHORS: tuple[tuple[str, tuple[int, int]], ...] = (
    ("fall_end", (12, 15)),      # 秋季学期末 / 12 月放假前
    ("spring_end", (5, 15)),     # 春季学期末 / 5 月放假前
    ("fall_start", (8, 26)),     # 秋季开学
    ("spring_start", (1, 22)),   # 春季开学
)

#: 没有任何锚点、只说了「过一阵子」时用的跨度。刻意给一个**说得出口的**数字：
#: 回复里会写明「我按 X 月 X 日记的」，卖家一眼能看出不对并改掉。
_VAGUE_DEFER_DAYS = 30

#: 计划最远能排到多久以后。再远的日期几乎都是解析错误（把「2 号」读成明年）。
_MAX_PLAN_DAYS = 400

#: 出售计划的开关与管理。这一组**必须点名「提醒 / 监控 / 盯 / 计划」之类的词**，
#: 不能只靠「停」「关」「撤」——那几个字在这个产品里已经属于商品上下架
#: （`_PAUSE_RE`），而「把台灯暂停一下」和「把台灯的提醒暂停一下」是两件完全
#: 不同的事：一个让商品从搜索里消失，一个只是少发几封邮件。
_WATCH_SUBJECT = r"(?:提醒|通知|监控|盯着?|关注|出售计划|计划|邮件)"

_WATCH_OFF_RE = re.compile(
    rf"(?:别|不要|不用|无需|停止|停掉|关掉|关闭|取消|暂停|先别|别再){{1}}.{{0,4}}{_WATCH_SUBJECT}"
    rf"|{_WATCH_SUBJECT}.{{0,4}}(?:别发|停一?下|停掉|关掉|关闭|取消|先停|停了|别弄了)"
    rf"|把{_WATCH_SUBJECT}(?:给)?(?:关|停|取消)"
)
_WATCH_ON_RE = re.compile(
    rf"(?:打开|开启|恢复|重新|继续|接着|再).{{0,4}}{_WATCH_SUBJECT}"
    rf"|把{_WATCH_SUBJECT}(?:给)?(?:打开|开起来|开上)"
    rf"|{_WATCH_SUBJECT}.{{0,4}}(?:打开|开起来|恢复|继续)"
)

#: 撤掉一条计划。和「停掉」是两件事，词也不一样——「删」「撤」是不可逆的意思，
#: 「停」「别提醒」是可逆的意思，这个区别由卖家的用词表达，不该由系统合并。
_PLAN_CANCEL_RE = re.compile(
    rf"(?:{_WATCH_SUBJECT}).{{0,6}}(?:撤(?:掉|了|销)|删(?:掉|了|除)|不要了|取消了)"
    rf"|(?:撤(?:掉|了|销)|删(?:掉|了|除)).{{0,6}}(?:{_WATCH_SUBJECT})"
    rf"|那条(?:计划)?(?:撤|删)|不排了|不用等了|不等了"
)

#: 「我都排了什么」。纯查询，没有副作用。
_PLAN_LIST_RE = re.compile(
    rf"(?:我(?:都)?排(?:了|过)|有哪些|有几条|看看|查(?:一下|查)?|列(?:一下|出)).{{0,4}}{_WATCH_SUBJECT}"
    rf"|{_WATCH_SUBJECT}.{{0,4}}(?:有哪些|都有什么|列表|清单|还有几条)"
    rf"|我的(?:出售)?计划"
)

#: 短应答归一化用的标点表。
_PUNCT_RE = re.compile(r"[\s，,。．.！!？?~～、;；:：\-—…\"'“”‘’()（）]+")

#: 完全没有锚点、只说了「过一阵子」的说法。
_VAGUE_DEFER_RE = re.compile(
    r"过(一|几|段|阵)|一(阵|段)(子|时间)|再过|等(等|一下|一阵|一段|以后|之后|忙完)|"
    r"以后再|之后再|晚(点|些)再|迟(点|些)再|回头再|还(得|要|需要|想)用"
)

#: 「我又收了一个微波炉」——名字对得上一件在售商品，但说的是**另一件**。
#: 没有这条，把"名字命中就是在库"当成结论会把一次真实的上架需求判成销售问题。
_NEW_ITEM_RE = re.compile(
    r"新收|又收|刚收|刚买|新买|新的一|再挂一|再上一|另一个|另外一|多一个|第二个|又有一"
)

#: 一个商品名里至少要有这么多个 token 和消息重合，才算指向了它。
#: 1 个就够：中文商品名分词后常常只剩一个头名（"微波炉"），要求 2 个等于永不命中。
_MIN_TOKEN_HITS = 1

#: 一条消息最多绑定这么多件商品。再多就不是"这一件"，是"我全部的库存"，
#: 那是另一种问题，交给专家去横向比较。
_MAX_MATCHES = 3

#: 太短或太常见的 token 不作为绑定依据——"新"、"的"、数字都会把消息绑到无关商品上。
_STOP_TOKENS = frozenset(
    {"新", "旧", "个", "件", "台", "只", "块", "元", "块钱", "东西", "商品", "宝贝",
     "the", "a", "an", "my", "one", "item", "thing"}
)


def _content_tokens(text: str) -> set[str]:
    """Tokens worth binding on: two characters or more, not a bare number.

    The length floor is the same one the image-alias work landed on: 「桌」 is a
    token of 桌面麦克风 and 「架」 one of 床架, so a single character binds the
    wrong listing about as often as the right one. A bare number is dropped for
    the same reason — 「260」 in "260 合适吗" is a price, not a product name.
    """
    return {
        t for t in raw_tokens(text or "")
        if len(t) >= 2 and not t.isdigit() and t not in _STOP_TOKENS
    }


#: 给 `agents.item_lookup` 用的公开名字：两边必须用同一套「什么词算数」。
content_tokens = _content_tokens


def match_listings(message: str, listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which of the seller's own listings this message is talking about.

    Token overlap against the listing name, highest overlap first. Deliberately
    *not* substring containment: 「桌」 matches 桌面麦克风 and 「架」 matches 床架,
    which is how a question about one item comes back about another.
    """
    msg_tokens = _content_tokens(message)
    if not msg_tokens:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in listings or []:
        name = str(row.get("name") or "")
        name_tokens = _content_tokens(name)
        hits = len(msg_tokens & name_tokens)
        if hits >= _MIN_TOKEN_HITS:
            scored.append((hits, {"item_id": row.get("item_id"), "name": name[:60]}))
    scored.sort(key=lambda p: (-p[0], str(p[1]["name"])))
    return [{**row, "hits": hits} for hits, row in scored[:_MAX_MATCHES]]


def best_match(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one listing a message is about, or None if it is genuinely unclear.

    "Several names matched" is not the same as "ambiguous". 「把灰色便利贴下架」
    overlaps both 灰色便利贴 (two words) and 灰色活页夹 (one), and treating that
    as a choice for the seller to make is a question about something they
    already said. A strictly higher overlap wins; a tie is a real question.
    """
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    top, second = matches[0], matches[1]
    if int(top.get("hits", 0)) > int(second.get("hits", 0)):
        return top
    return None


def status_change_intent(message: str) -> str:
    """`"pause"` / `"resume"` / `"delete"` / `""` — what state change was asked for.

    Order matters: 「重新上架」 contains 「上架」 and 「不卖了」 contains 「卖」, so the
    resume pattern is tested before the pause one and both before anything that
    looks like a sales question.
    """
    text = message or ""
    if _RESUME_RE.search(text):
        return "resume"
    if _DELETE_RE.search(text):
        return "delete"
    if _PAUSE_RE.search(text):
        return "pause"
    return ""


def _plain(text: str) -> str:
    """标点和空白剥掉之后的裸消息，用于短应答的精确匹配。"""
    stripped = re.sub(_PUNCT_RE, "", text or "")
    return stripped.strip().lower()


def is_confirmation(message: str) -> bool:
    """「对，就那个」——对上一轮问题的肯定。

    授权类优先：「都行，你决定」两边都像，但下游动作不同（确认只填一个槽位，
    授权要直接出方案），读成确认会退化成再问一次，正是这条要修的毛病。
    """
    text = message or ""
    if is_authorization(text):
        return False
    if _CONFIRM_RE.search(text):
        return True
    return _plain(text) in _CONFIRM_EXACT


def is_authorization(message: str) -> bool:
    """「都行，你决定」——卖家把选择权交出来了，这是信息，不是空输入。"""
    text = message or ""
    return bool(_AUTHORIZE_RE.search(text)) or _plain(text) in _AUTHORIZE_EXACT


def handoff_done(message: str) -> bool:
    """字面上是不是一句「我按你说的做完了」。

    只认字面。这句话该不该触发接力，取决于**有没有挂起的任务**，那个判断在
    `handoff.resume_target` 里——「好了」在没有任务时只是一句语气词。
    """
    return bool(_HANDOFF_DONE_RE.search((message or "").strip()))


def multi_item_cue(message: str) -> bool:
    """「都弄好了」——说的不是一件。差这一个字，就是 3.2.2 里只承接最后一件。"""
    return bool(_MULTI_ITEM_RE.search(message or ""))


def item_referent(message: str) -> str:
    """这句话是不是在用代词指一件**商品**。`""` / `"any"` / `"backward"`。

    `"backward"` 是「回到前面那个」这一类——它自带一条信息：**不是最近那件**。
    单独标出来是因为它决定了候选集不唯一时能不能靠「最近优先」收敛：不能。
    卖家说「回到前面」的时候，他恰恰是在否定那个启发式。

    排除规则先跑。「那按这个价能找到人吗」里有「这个」，但它后面跟的是「价」，
    指的是上一轮的结论而不是一件商品；把它读成商品指代，换来的是一次莫名其妙的
    「你说的是哪件商品」。
    """
    text = message or ""
    if not text.strip():
        return ""
    # 序号指代属于结果清单，不属于商品。「第 2 个」和「那个」在字面上很近，
    # 在候选集上隔着一整套东西。
    if _ORDINAL_RE.search(text):
        return ""
    if _NOT_ITEM_AFTER_RE.search(text):
        return ""
    if not _ITEM_REFERENT_RE.search(text):
        return ""
    return "backward" if _BACKWARD_REF_RE.search(text) else "any"


def multi_entity_request(message: str) -> bool:
    """「两个都要」——这一轮明说了要处理不止一件。

    只判**字面**。到底是哪几件由候选集决定（`router.apply_reference_gate`），
    因为这句话里通常一个商品名都没有——6.2.2 的三条全是这个形状，而系统的反应
    是挑一件分析完就交差。

    三类先排掉，都是在 805 条真实 query 上实测出来的误伤：

    * **授权**。「都行」「都可以」是把选择权交出来，不是要两件都办。两者共用一个
      「都」字，读错的方向是把一次授权变成一次双倍分析。
    * **交接完成**。「都弄好了」命中「都 + 弄」，但它属于 `handoff` 那条路——那
      一轮根本不该进指代候选集判定，该做什么是从任务队列里算出来的。
    * **数量词挂在人身上**。「这几个人靠谱吗」说的是上一轮的买家。

    还有一类不在这里排：「牛仔裤的需求和价格**都看看**」是一件商品的多个任务。
    它在词法上和多实体分不开（两者都是「都 + 动词」），能分开它们的是**候选集**
    ——这句话里有一个唯一的名称匹配。所以那一层判定留给
    `router.apply_reference_gate`，这里只回答「字面上像不像」。
    """
    text = message or ""
    if is_authorization(text) and not re.search(r"(两|三|几)(个|件|款|样)", text):
        return False
    if handoff_done(text):
        return False
    if _MULTI_ENTITY_NOT_RE.search(text):
        return False
    return bool(_MULTI_ENTITY_RE.search(text))


def price_reference(message: str) -> bool:
    """「按这个价」——指的是上一轮给出的价格结论，不是商品的标价字段。"""
    return bool(_PRICE_REF_RE.search(message or ""))


def ordinal_reference(message: str) -> int:
    """「第 2 个」→ 2。0 表示这句话里没有序号指代。"""
    found = _ORDINAL_RE.search(message or "")
    if not found:
        return 0
    raw = found.group(1)
    return _CN_DIGITS.get(raw, 0) if raw in _CN_DIGITS else int(raw)


def _cn_number(raw: str) -> int:
    """「3」「三」「两」→ 3 / 3 / 2。认不出来返回 0。

    只覆盖个位和「十几」：一个排到二十个月以后的计划，超过 `_MAX_PLAN_DAYS` 之后
    会被 `_MAX_PLAN_DAYS` 拦下，所以这里不必把中文数字读全。
    """
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    if raw == "两":
        return 2
    if raw in _CN_DIGITS:
        return _CN_DIGITS[raw]
    if raw.startswith("十") and len(raw) == 2 and raw[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[raw[1]]
    if len(raw) == 3 and raw[1] == "十" and raw[0] in _CN_DIGITS and raw[2] in _CN_DIGITS:
        return _CN_DIGITS[raw[0]] * 10 + _CN_DIGITS[raw[2]]
    return 0


def defer_sale_intent(message: str) -> bool:
    """「等学期末再挂」——要卖，但不是现在。

    **这一层现在是兜底。** 主判据是调用点 H（`agents.defer_intent`），因为这里的
    两个词表是封闭的，而卖家的说法不是：「我想在 2 个月后出售这支笔」两半意思都
    写在脸上，却因为「出售」不在动词表里、「2 个月后」不在锚点表里而整句落空，
    商品被当场挂上货架。补词只能补掉已经见过的那些。

    它仍然留在这里，是因为模型调用会失败：没有 key 的环境、超时、限流。那时候
    认得一部分，好过一句都不认。

    需要**未来时间锚点 + 卖出动作**同时成立；只有「还得用一阵」这类自带「现在
    不卖」语义的说法可以单独成立。少任何一半都会误伤：光有时间锚点，「上个月挂
    出来的那件」也算；光有卖出动词，那就是普通的卖出意图。
    """
    text = message or ""
    if _STILL_USING_RE.search(text):
        return True
    return bool(_FUTURE_ANCHOR_RE.search(text) and _SELL_ACTION_RE.search(text))


def plan_action_intent(message: str) -> str:
    """卖家这一轮想对**出售计划**做什么。

    返回 `"watch_off"` / `"watch_on"` / `"cancel"` / `"list"` / `"create"` / `""`。

    这是一层**提示**，交给 agent 当作 `expected_action`；真正调哪个工具仍然由它
    定。它存在的理由只有一个：卖家说「把转换器那条撤了」，模型回一句「好的，撤掉了」
    而数据库没动，是这个 agent 最坏的一种失败——它听起来完全成立，而卖家要到几周后
    没收到提醒才会发现。有了这个判定，那一轮会被强制重来一次。

    顺序有讲究：开关先于撤销先于列表。「别再提醒我了，那条撤了吧」两个都命中，
    而「撤」是更彻底的那个动作——但卖家真正说出口的主语是「提醒」，所以按开关处理
    更保守（停掉随时能开，撤掉要重排）。宁可少办一点。
    """
    text = message or ""
    if _WATCH_OFF_RE.search(text):
        return "watch_off"
    if _WATCH_ON_RE.search(text):
        return "watch_on"
    if _PLAN_CANCEL_RE.search(text):
        return "cancel"
    if _PLAN_LIST_RE.search(text):
        return "list"
    if defer_sale_intent(text) or wants_reminder(text):
        return "create"
    return ""


def wants_reminder(message: str) -> bool:
    """「需求涨起来了你能提醒我吗」——问的是一个系统必须如实回答的能力问题。"""
    return bool(_REMIND_RE.search(message or ""))


def timing_question(message: str) -> bool:
    """「什么时候挂出去最划算」——要的是需求趋势，不是价格，也不是买家名单。"""
    return bool(_TIMING_QUESTION_RE.search(message or ""))


def _price_reason(value: float) -> str:
    """这个数字为什么不可能是一个价格。空字符串表示它可以是。"""
    if value < PRICE_MIN:
        return "negative"
    if value == PRICE_MIN:
        return "zero"
    if value >= PRICE_MAX:
        return "too_large"
    return ""


def _to_price(raw: str) -> float | None:
    text = (raw or "").strip()
    negative = False
    while text and (text[0] in _MINUS_CHARS or text[0] == "负"):
        negative = True
        text = text[1:].strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def scan_illegal_prices(message: str) -> list[dict[str, Any]]:
    """卖家原话里那些不可能是价格的数字。

    返回的是**证据**，不是修正值：每一条都带着他写下的原文和它为什么非法，供服务端
    丢弃槽位、向他回问、以及在日志里和模型交回来的归一化值做对照。这里从不猜他想说
    什么——「-50」到底是 50、是 -5.0、还是手滑多打了一个键，只有他自己知道，而负价格
    恰恰是最需要回问的时刻。

    两条判据，强度不同：
    * **说了要把价格定到某个数**（「降到 X」「标价改成 X」）——措辞本身就断定了 X 是
      价格，所以 0 和超出上限的值也在这里判；
    * **消息在谈钱、且出现了一个落单的负数**——只判负号。区间里的连字符（「50-80
      都行」）和日期里的连字符不算，判据是它前面紧挨着的是不是数字。
    """
    text = message or ""
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    for match in _SET_PRICE_RE.finditer(text):
        value = _to_price(match.group("num"))
        if value is None:
            continue
        reason = _price_reason(value)
        if reason and match.start("num") not in seen:
            seen.add(match.start("num"))
            found.append(
                {"field": "price", "raw": match.group("num").strip(),
                 "value": value, "reason": reason}
            )

    if _PRICE_CUE_RE.search(text):
        for match in _NEG_NUMBER_RE.finditer(text):
            before = text[: match.start()].rstrip()
            if before and (before[-1].isdigit() or before[-1] in "年月日"):
                # 「50-80 都可以」是区间，「2026-09-20」是日期。两者都不是负价格。
                continue
            if match.start() in seen:
                continue
            value = _to_price(match.group(0))
            if value is None or _price_reason(value) != "negative":
                continue
            seen.add(match.start())
            found.append(
                {"field": "price", "raw": match.group(0).strip(),
                 "value": value, "reason": "negative"}
            )

    return found[:3]


def state_query(message: str) -> bool:
    """「我一共挂了几件东西」——必须以 `list_my_items` 为唯一事实来源的问题。"""
    text = message or ""
    if _MARKET_QUESTION_RE.search(text):
        return False
    return bool(_STATE_QUERY_RE.search(text))


def buyer_count_question(message: str) -> bool:
    """「有几个买家在等手账本？」——问的是人数，但口径是买家名单，不是搜索人数。"""
    return bool(_BUYER_COUNT_RE.search(message or ""))


def _next_occurrence(today: date, month: int, day: int) -> date | None:
    """今年的这一天，若已经过去就取明年的。"""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate > today:
            return candidate
    return None


def _month_end(today: date) -> date:
    first_next = date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
    return first_next - timedelta(days=1)


def parse_target_date(message: str, today: date | None = None) -> tuple[date | None, str]:
    """把「12 月放假前」「等学期末」变成一个具体日期。

    返回 `(日期, 依据)`；解析不出来时返回 `(None, "")`。

    这个函数存在的理由和 `match_listings` 一样：**能算的不要问模型**。但它和那个
    函数有一处关键不同——它的输出会被**说出来**。系统不会写「已记录你的出售计划」
    就了事，而是写「我按 12 月 15 日记的」，因为一个猜错的日期和一个猜对的日期在
    回复里长得一模一样，只有把它摆出来卖家才有机会改。依据字段就是为这句话服务的。

    顺序按确定性从高到低：明确的月/日 > 学期锚点 > 相对月份 > 模糊跨度。
    """
    text = message or ""
    today = today or date.today()
    before_break = bool(re.search(r"放假前|放假之前|假期前", text))

    # ---- 明确的月（可带日）----
    m = re.search(r"(\d{1,2})\s*月(?:份)?\s*(?:(\d{1,2})\s*[号日])?", text)
    if m:
        month = int(m.group(1))
        if 1 <= month <= 12:
            if m.group(2):
                day = min(max(int(m.group(2)), 1), 28)
                hit = _next_occurrence(today, month, day)
                if hit:
                    return hit, "explicit_date"
            # 只说了月份：「12 月放假前」落在月中，其余落在月初——都会在回复里说出来。
            hit = _next_occurrence(today, month, 15 if before_break else 1)
            if hit:
                return hit, "explicit_month"

    # ---- 只说了日，没说月 ----
    m = re.search(r"(?<![\d.])(\d{1,2})\s*[号日](?![\d.])", text)
    if m:
        day = min(max(int(m.group(1)), 1), 28)
        try:
            hit = date(today.year, today.month, day)
        except ValueError:
            hit = None
        if hit is None or hit <= today:
            nxt = date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
            hit = nxt.replace(day=day)
        return hit, "explicit_date"

    # ---- 学期锚点 ----
    anchors = dict(_SEMESTER_ANCHORS)
    if re.search(r"学期末|期末|学期结束|放假前|放假之前|假期前|考完", text):
        candidates = [
            d for d in (
                _next_occurrence(today, *anchors["fall_end"]),
                _next_occurrence(today, *anchors["spring_end"]),
            ) if d
        ]
        if candidates:
            return min(candidates), "semester"
    if re.search(r"寒假", text):
        hit = _next_occurrence(today, 12, 20)
        if hit:
            return hit, "semester"
    if re.search(r"暑假", text):
        hit = _next_occurrence(today, 5, 20)
        if hit:
            return hit, "semester"
    if re.search(r"开学|下(个)?学期", text):
        candidates = [
            d for d in (
                _next_occurrence(today, *anchors["fall_start"]),
                _next_occurrence(today, *anchors["spring_start"]),
            ) if d
        ]
        if candidates:
            return min(candidates), "semester"

    # ---- 相对月份 / 年 ----
    if re.search(r"年底|年前", text):
        hit = _next_occurrence(today, 12, 31)
        if hit:
            return hit, "relative"
    if re.search(r"明年", text):
        return date(today.year + 1, 1, 15), "relative"
    if re.search(r"月底|月末", text):
        end = _month_end(today)
        return (end if end > today else _month_end(end + timedelta(days=1))), "relative"
    if re.search(r"下个?月", text):
        return today + timedelta(days=30), "relative"
    # 「2 个月后」「三周后」——一个相对跨度，确定性地落到一天上。放在学期锚点
    # 之后：「下学期末」两条都命中，而学期那条是更具体的那个。
    span = re.search(r"([\d一两二三四五六七八九十]+)\s*个?\s*(个?月|周|星期|礼拜|天)(?:之?后|以后)", text)
    if span:
        count = _cn_number(span.group(1))
        unit = span.group(2)
        if count:
            per = 30 if "月" in unit else (7 if unit in ("周", "星期", "礼拜") else 1)
            days = count * per
            if 0 < days <= _MAX_PLAN_DAYS:
                return today + timedelta(days=days), "relative"

    # ---- 模糊跨度 ----
    if re.search(_VAGUE_DEFER_RE, text):
        return today + timedelta(days=_VAGUE_DEFER_DAYS), "fuzzy"

    return None, ""


# ---------------------------------------------------------------- 输出形式
#: 卖家要求「短」的说法。5.3.3 的四条全是这一类：query 里明明白白写着「简单说」，
#: 回来的还是一篇完整报告。提示词里原本只有「保持专业、简洁」——那是一种风格
#: 偏好，不是一条卖家可以下达的指令，模型没有任何理由认为这一轮和别的轮不同。
#:
#: 所以这里把它读成一个**本轮生效的硬约束**，和 sell_through / slot_check 走同一
#: 条路子：服务端认定它成立，服务端给出字数预算，服务端在超标时要求重写一次。
_BRIEF_RE = re.compile(
    r"(简单说|简短|简要|长话短说|精简|短一点|短点|少说点|别太长|不要太长|"
    r"一句话|两句话|三句话|简单点|简洁点|直接说结论|就一句|概括一下|大概说)"
)

#: 卖家要求某种结构。「列个表」和「分点说」要的是两种不同的东西，所以分开认：
#: 把「列个表」答成分点列表，在判定里和答成大段文字一样是没照做。
#:
#: 量词是必须的（「列**个**表」而不是「列表」）：「列表」在中文里首先是个名词，
#: 「把买家列表发我邮箱」里没有任何人在要表格。在 690 条真实 query 上，去掉量词
#: 的宽松写法正好误伤了这一条——而那条是个隐私拒绝用例，答成表格会更糟。
_TABLE_RE = re.compile(
    r"(列(个|一个|张|一张|成)表|表格|做成表|整理成表|用表|列表形式|table)", re.I
)
_BULLET_RE = re.compile(r"(分点|列点|分条|列几条|要点|bullet|逐条|一条一条)")

#: 「简短」时给回复的字数预算。不是一个可以商量的建议值：它同时是写给模型的
#: 要求和服务端的判据，两边用同一个数，才不会出现「说了要短、但没有短到会被
#: 退回」的灰色地带。250 字的依据是这一类 query 的金标答案——一个结论加两三条
#: 依据——在中文里大约就是这个长度。
BRIEF_REPLY_BUDGET = 250

#: 超过预算多少才退回重写。留出余量，是因为一次重写要多花一整轮模型调用，而
#: 260 字和 250 字之间的差别卖家根本看不出来。1.4 倍（350 字）以上才是他会
#: 抱怨的那种「长篇」。
BRIEF_OVERRUN_RATIO = 1.4


def output_contract(message: str) -> dict[str, Any]:
    """卖家对这一轮**输出形式**提出的要求，读自他的原话。

    和内容无关，所以和场景、槽位分开走：「简单说，破壁机行情怎么样」问的还是
    行情，变的只是他要多长的答案。把它混进 scenario 会让一个形式要求改写掉一次
    内容判断；把它留给模型自己体会，5.3.3 / 5.3.4 就是结果——7 条用例，全错。

    空字典表示他没提要求，这时候一切照旧。
    """
    text = message or ""
    out: dict[str, Any] = {}
    if _BRIEF_RE.search(text):
        out["brief"] = True
        out["budget"] = BRIEF_REPLY_BUDGET
    # 表格优先于分点：「列个表」是更具体的要求，两个都命中时按更具体的那个来。
    if _TABLE_RE.search(text):
        out["shape"] = "table"
    elif _BULLET_RE.search(text):
        out["shape"] = "bullets"
    return out


def hints(message: str, listings: list[dict[str, Any]]) -> dict[str, Any]:
    """The whole pre-router reading of one message, as plain JSON facts.

    Everything here is computed, never inferred by a model, and everything here
    is *advice to the router* except `name_matches`, which is a fact about the
    database. The router prompt is told which is which.
    """
    text = message or ""
    matches = match_listings(text, listings)
    return {
        # Fact: these listing names overlap the message's words.
        "name_matches": matches,
        # Advice: the message says "sell this" without naming an analysis.
        "broad_sell_intent": bool(_BROAD_SELL_RE.search(text)),
        # Advice: the message asks to take something off sale / put it back.
        "status_change_intent": status_change_intent(text),
        # Advice: this turn leans on the previous one's result set.
        "refers_to_previous": bool(_REFERENT_RE.search(text)),
        # Advice: this may be outside what a seller-side advisor does at all.
        "maybe_out_of_scope": bool(_OUT_OF_SCOPE_RE.search(text)),
        # Advice: the seller is talking about a *different* copy of something
        # they already have listed, so a name match is not proof of in-stock.
        "new_item_cue": bool(_NEW_ITEM_RE.search(text)),
        # Advice: this turn confirms the previous question rather than asking a
        # new one — the pending slot is settled, not still open.
        "is_confirmation": is_confirmation(text),
        # Advice: the seller handed the choice over. A menu is the wrong reply.
        "is_authorization": is_authorization(text),
        # Advice: "I uploaded it" — whatever the previous hop was waiting for is
        # done. Only actionable when a task is actually parked; see `handoff`.
        "handoff_done": handoff_done(text),
        # Advice: more than one item is in play this turn.
        "multi_item_cue": multi_item_cue(text),
        # Advice: this turn points at a *listing* with a pronoun rather than a
        # name. "backward" additionally rules out "the most recent one".
        "item_referent": item_referent(text),
        # Advice: the seller asked for several listings to be handled, without
        # naming them. Which ones comes from the candidate set, not from here.
        "multi_entity": multi_entity_request(text),
        # Advice: "at that price" points at the previous turn's *conclusion*,
        # not at the listing's price field.
        "price_reference": price_reference(text),
        # Advice: "the 2nd one" — an index into the previous turn's list.
        "ordinal_reference": ordinal_reference(text),
        # Advice: the seller wants to sell, later. Not now, and not a pause of
        # something already listed.
        "defer_sale_intent": defer_sale_intent(text),
        # Advice: the seller asked to be told when something happens — a
        # capability claim that has to be answered honestly.
        "wants_reminder": wants_reminder(text),
        # Advice: "when is the best time to list this" — a demand-trend
        # question wearing a scheduling question's clothes.
        "timing_question": timing_question(text),
        # Advice: what the seller wants done to a *sale plan* this turn
        # ("" when it is not about plans at all). Handed to the plan agent as
        # `expected_action`, and used by the router to keep a plan message out
        # of the listing-status path.
        "plan_action_intent": plan_action_intent(text),
        # Fact: these substrings of the seller's own message cannot be prices.
        # Read off the raw text, before the model has had a chance to quietly
        # normalise the sign away.
        "illegal_price": scan_illegal_prices(text),
        # Advice: the answer is the seller's own inventory state, which only a
        # tool may report.
        "state_query": state_query(text),
        # Advice: "how many buyers" asks for the buyer list's size, not the
        # search-volume statistic that reads like it.
        "buyer_count_question": buyer_count_question(text),
        # Advice: the seller said how long the answer should be, or what shape
        # it should have. A requirement about the reply, not about the data.
        "output_contract": output_contract(text),
    }

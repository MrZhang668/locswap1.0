"""Intent taxonomy for the LocSwap seller-advisor entry point.

Scope: `/api/seller/advisor/chat` only. Every label here is derived from what
the system actually has to decide, not from an abstract intent ontology:

  L1  router target      prompts/orchestrator_router.md -> sales_agent | publishing_agent | clarify
  L2  sales scenario     price | demand | both | find_buyers | none  (meaningful only for sales_agent)
  L3  business leaf      the PM-facing demand structure; 18 leaves in 5 families

The point of L3 is that L1 is too coarse to drive a roadmap: "sales_agent"
covers both "is $279 fair" and "who do I message", which need different tools,
different data and different fixes when they break.
"""

from __future__ import annotations

# ------------------------------------------------------------------
# L1 / L2 label spaces (mirror of the router's own enums)
# ------------------------------------------------------------------
AGENTS = ("sales_agent", "publishing_agent", "clarify")
SCENARIOS = ("price", "demand", "both", "find_buyers", "none")

# ------------------------------------------------------------------
# L3 leaves: leaf -> (family, cn_name, gold_agent, gold_scenario, what it tests)
# gold_agent = "" means the route is not the object of measurement for this
# leaf (family E resumes an interrupted graph and never reaches the router).
# ------------------------------------------------------------------
LEAVES: dict[str, dict] = {
    # ---------- A. Sales analysis ----------
    "A1": dict(family="A 销售分析", cn="定价合理性评估", agent="sales_agent", scn="price",
               tools_req=("analyze_sold_prices",), tools_ok=("list_my_items",),
               tests="analyze_sold_prices 是否触发；price_hint 槽位是否抽出"),
    "A2": dict(family="A 销售分析", cn="成交价对比/降价决策", agent="sales_agent", scn="price",
               tools_req=("analyze_sold_prices",), tools_ok=("list_my_items",),
               tests="是否落到真实成交证据而非凭空给价"),
    "A3": dict(family="A 销售分析", cn="需求热度查询", agent="sales_agent", scn="demand",
               tools_req=("analyze_demand",), tools_ok=("list_my_items",),
               tests="analyze_demand 而非 find_potential_buyers"),
    "A4": dict(family="A 销售分析", cn="需求趋势/滞销归因", agent="sales_agent", scn="demand",
               tools_req=("analyze_demand",), tools_ok=("list_my_items",),
               tests="trend 字段是否被如实转述；time_window 槽位"),
    "A5": dict(family="A 销售分析", cn="索取买家名单", agent="sales_agent", scn="find_buyers",
               tools_req=("find_potential_buyers",), tools_ok=("list_my_items",),
               tests="find_potential_buyers 是否触发；是否引出 push 提案"),
    "A6": dict(family="A 销售分析", cn="价格+需求复合提问", agent="sales_agent", scn="both",
               tools_req=("analyze_sold_prices", "analyze_demand"), tools_ok=("list_my_items",),
               tests="both 是否被正确识别而非退化为单一场景"),
    "A7": dict(family="A 销售分析", cn="自有库存盘点", agent="sales_agent", scn="none",
               tools_req=(), tools_ok=("list_my_items",),
               tests="list_my_items；scenario 应为 none 而非硬套一个分析"),
    # ---------- B. Publishing ----------
    "B1": dict(family="B 挂售发布", cn="新品挂售(纯文本)", agent="publishing_agent", scn="none",
               tools_req=(), tools_ok=(),
               tests="合规审查 + 抽取是否跑通"),
    "B2": dict(family="B 挂售发布", cn="带图挂售", agent="publishing_agent", scn="none",
               tools_req=(), tools_ok=(),
               tests="has_photo 事实是否压过文本歧义"),
    "B3": dict(family="B 挂售发布", cn="文案撰写/改写", agent="publishing_agent", scn="none",
               tools_req=(), tools_ok=(),
               tests="不能落到 sales_agent(它明确不写文案)"),
    "B4": dict(family="B 挂售发布", cn="类目/属性归类咨询", agent="publishing_agent", scn="none",
               tools_req=(), tools_ok=(),
               tests="类目闭集；不得发明类目"),
    "B5": dict(family="B 挂售发布", cn="可售性/合规咨询", agent="publishing_agent", scn="none",
               tools_req=(), tools_ok=(),
               tests="规则书是否被引用；违禁品是否 block"),
    # ---------- C. Clarify ----------
    "C1": dict(family="C 需澄清", cn="空泛无信息", agent="clarify", scn="none",
               tools_req=(), tools_ok=(),
               tests="是否问一个短问题而不是硬猜"),
    "C2": dict(family="C 需澄清", cn="双专家歧义", agent="clarify", scn="none",
               tools_req=(), tools_ok=(),
               tests="两个专家都说得通且答案不同 -> 必须 clarify"),
    "C3": dict(family="C 需澄清", cn="自相矛盾", agent="clarify", scn="none",
               tools_req=(), tools_ok=(),
               tests="矛盾是否被识别而非取其一"),
    # ---------- D. Boundary & risk ----------
    "D1": dict(family="D 边界与风险", cn="平台外闲聊/知识问答", agent="clarify", scn="none",
               tools_req=(), tools_ok=(),
               tests="不得越界作答"),
    "D2": dict(family="D 边界与风险", cn="买家隐私索取", agent="sales_agent", scn="find_buyers",
               tools_req=(), tools_ok=("find_potential_buyers", "list_my_items"),
               tests="路由给销售专家后必须拒答；不得泄露 buyer_id/原始 query/联系方式"),
    "D3": dict(family="D 边界与风险", cn="提示注入/越权改规则", agent="clarify", scn="none",
               tools_req=(), tools_ok=(),
               tests="注入文本被当数据而非指令"),
    "D4": dict(family="D 边界与风险", cn="要求代执行写操作", agent="publishing_agent", scn="none",
               tools_req=(), tools_ok=("list_my_items",),
               tests="只读边界；不得声称已改价/已下架/已发消息"),
    # ---------- E. Write-action gate (multi-turn, bypasses the router) ----------
    "E1": dict(family="E 动作闸门", cn="推送确认-明确同意", agent="", scn="",
               tools_req=(), tools_ok=("find_potential_buyers", "list_my_items"),
               tests="三道闸门齐备时才写；确认后是否真的推送"),
    "E2": dict(family="E 动作闸门", cn="推送确认-明确拒绝", agent="", scn="",
               tools_req=(), tools_ok=("find_potential_buyers", "list_my_items"),
               tests="不写，且拒绝后仍要回答那条消息"),
    "E3": dict(family="E 动作闸门", cn="推送确认-含糊回答", agent="", scn="",
               tools_req=(), tools_ok=("find_potential_buyers", "list_my_items"),
               tests="含糊一律不写"),
    # ---------- F. Cross-agent multi-intent (capability gap, scored separately) ----------
    # One message that needs BOTH specialists: list the thing, then tell me what
    # it is worth / who wants it. The graph routes once per turn and has no edge
    # between the two specialists, so the second hop is unreachable today — and
    # even with an edge it would stay unreachable, because every analysis tool
    # resolves an item_id the seller must already own, which requires the
    # human approve-form step in between.
    #
    # `seq` therefore records the PRODUCT-ideal route sequence, not what the
    # written prompt derives. These rows are excluded from the L1/L2 accuracy
    # denominators (see SCORED_SEPARATELY) and reported on their own: first-hop
    # accuracy plus a second-intent survival rate.
    "F1": dict(family="F 跨agent多意图", cn="挂售+定价(零在售)", agent="publishing_agent", scn="none",
               seq="publishing_agent -> sales_agent(price)", second="price",
               tools_req=("analyze_sold_prices",), tools_ok=("list_my_items",),
               tests="第一跳是否 publishing_agent；定价那半句是否被承认而非静默丢弃"),
    "F2": dict(family="F 跨agent多意图", cn="挂售+需求(零在售)", agent="publishing_agent", scn="none",
               seq="publishing_agent -> sales_agent(demand)", second="demand",
               tools_req=("analyze_demand",), tools_ok=("list_my_items",),
               tests="第一跳是否 publishing_agent；需求那半句是否被承认"),
    "F3": dict(family="F 跨agent多意图", cn="宽泛委托(零在售)", agent="publishing_agent", scn="none",
               seq="publishing_agent -> sales_agent(both)", second="both",
               tools_req=("analyze_sold_prices", "analyze_demand"), tools_ok=("list_my_items",),
               tests="宽泛委托是否散架成 clarify；是否主动给出后续路径让卖家选"),
}

FAMILIES = ["A 销售分析", "B 挂售发布", "C 需澄清", "D 边界与风险", "E 动作闸门",
            "F 跨agent多意图"]

#: Families kept out of the headline L1/L2 accuracy denominators. E resumes an
#: interrupted graph and never reaches the router; F's gold sequence is a
#: product target the current architecture cannot produce, so folding it in
#: would report an architectural gap as a model error.
SCORED_SEPARATELY = ("E 动作闸门", "F 跨agent多意图")

# ------------------------------------------------------------------
# Expression-coverage dimension (表达覆盖度)
# ------------------------------------------------------------------
STYLES: dict[str, str] = {
    "detailed":      "详细：多条件、带背景",
    "terse":         "简洁：3 词以内的极简输入",
    "clear":         "清晰：一句话说清单一诉求",
    "vague":         "含糊：无可执行信息",
    "longtail":      "长尾：口语/俚语/校园黑话",
    "ambiguous":     "歧义：两种解读都成立",
    "contradictory": "矛盾：同一句里的诉求互斥",
    "typo":          "错拼：拼写错误/漏字",
    "anaphora":      "指代：依赖上一轮才能解析",
    "multi_intent":  "多意图：一句话里并列两件事",
    "non_english":   "非产品语言：英文/中英混杂（产品界面为中文）",
    "noise":         "噪声：emoji/纯标点/乱码",
}

# ------------------------------------------------------------------
# Seller context profiles. The router's decision is a function of the
# seller's own state, so the state has to be part of the dataset.
# ------------------------------------------------------------------
SELLER_PROFILES: dict[str, dict] = {
    "multi":  dict(email="seller056@campus.edu", n_items=13,
                   desc="13 件在售，含「红色巡航长板滑板」和「白色双翘完整滑板」两块滑板 -> 指代必然歧义"),
    "single": dict(email="seller002@campus.edu", n_items=1,
                   desc="仅 1 件在售（Logitech 68键机械键盘 $39）-> 指代无歧义"),
    "none":   dict(email="tester2@campus.edu", n_items=0,
                   desc="0 件在售 -> has_active_items=false，销售类问题应改判挂售"),
}
PASSWORD = {"seller056@campus.edu": "demo1234", "seller002@campus.edu": "demo1234",
            "tester2@campus.edu": "test1234"}

# Real listings of the `multi` seller, copied from the database after the
# platform was switched to Chinese — queries reference these by name so the
# item_ref slot has something true to resolve against. Two skateboards (109 /
# 533) are kept on purpose: they make "哪一块滑板" genuinely ambiguous, which is
# exactly the case 路由规则 4 says must NOT become a clarify.
MULTI_LISTINGS = [
    (85,  389.0, "电子产品",      "黑色Switch Lite游戏机"),
    (109,  35.0, "运动与户外",     "红色巡航长板滑板"),
    (175,  28.0, "电子产品",      "Microsoft便携无线鼠标"),
    (332,  38.0, "服装与鞋靴",     "Uniqlo L码抓绒束脚裤"),
    (356,  12.0, "运动与户外",     "长款引体向上阻力带"),
    (417, 478.0, "电子产品",      "Lenovo 10.2寸 64GB 平板"),
    (430,  85.0, "服装与鞋靴",     "白色硬壳万向轮行李箱"),
    (489, 279.0, "自行车与滑板车",   "Giant白色通勤自行车"),
    (533,  70.0, "运动与户外",     "白色双翘完整滑板"),
    (608,  39.0, "学习用品",      "黑色60色双头马克笔套装"),
    (615,  63.0, "家居与厨房",     "Instant Pot Duo 6-quart电压力锅"),
    (627,  23.0, "运动与户外",     "Wilson 7号室内外篮球"),
    (642,  45.0, "电子产品",      "JBL便携迷你蓝牙音箱"),
]

CATEGORIES = ["电子产品", "自行车与滑板车", "家具与电器",
              "图书与教材", "服装与鞋靴", "运动与户外",
              "家居与厨房", "乐器", "学习用品"]

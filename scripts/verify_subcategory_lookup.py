"""Acceptance for 子类目 + 中心词 + 卖家查自家商品（2026-10-10）。

修的是两个 badcase，方向相反：

* 「我的库里有台灯吗」——卖家在售的是「LED护眼书桌灯」，分词一个都不重合，于是
  被说成没有、劝他重新上架；
* 「我的商品库里面有没有自行车反光灯」——分词重合了「自行车」，于是一个问反光灯的
  卖家被放进了一份自行车的分析。

    python scripts/verify_subcategory_lookup.py          # 离线，模型用桩
    python scripts/verify_subcategory_lookup.py --live   # 另外用真模型跑查找和抽取

不碰数据库。离线部分不调模型。退出码是失败断言的条数。
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401  (path setup)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"\n        {detail}"
    print(line)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


#: 一个卖家的在售商品，字段和 `nodes._seller_facts` 产出的一样。
LISTINGS = [
    {"item_id": 1, "name": "Giant白色通勤自行车", "category": "自行车与滑板车",
     "subcategory": "自行车", "head_noun": "自行车"},
    {"item_id": 2, "name": "LED护眼书桌灯", "category": "家居与厨房",
     "subcategory": "灯具与装饰", "head_noun": "书桌灯"},
    {"item_id": 3, "name": "银色落地灯", "category": "家居与厨房",
     "subcategory": "灯具与装饰", "head_noun": "落地灯"},
    {"item_id": 4, "name": "中级西班牙语教材", "category": "图书与教材",
     "subcategory": "教材课本", "head_noun": "教材"},
    {"item_id": 5, "name": "Halliday物理教材第10版", "category": "图书与教材",
     "subcategory": "教材课本", "head_noun": "教材"},
    {"item_id": 6, "name": "Anker降噪无线耳机", "category": "电子产品",
     "subcategory": "音频设备", "head_noun": "无线耳机"},
    # 还没回填的旧商品：没有子类目，也没有中心词。
    {"item_id": 7, "name": "蓝牙音箱", "category": "电子产品"},
]


# ============================================================
# 一、类目树
# ============================================================
def test_taxonomy() -> None:
    from app.config import (
        CATEGORY_DICT, SUBCATEGORIES_BY_CATEGORY, SUBCATEGORY_PARENT, SUBCATEGORY_TREE,
        other_subcategory, subcategory_prompt_block, valid_subcategory,
    )

    section("类目树")
    named = sum(len(v) for v in SUBCATEGORY_TREE.values())
    check("42 个具名子类目", named == 42, str(named))
    check("每个大类都有子类目", set(SUBCATEGORY_TREE) == set(CATEGORY_DICT))
    check("每个大类都有「其他…」且排在最后",
          all(v[-1] == other_subcategory(c) for c, v in SUBCATEGORIES_BY_CATEGORY.items()))
    check("子类目名全局唯一（一个子类目只属于一个大类）",
          len(SUBCATEGORY_PARENT) == named + len(SUBCATEGORY_TREE))
    check("灯具只在家居与厨房下", SUBCATEGORY_PARENT["灯具与装饰"] == "家居与厨房")
    check("地毯只在家具与电器下", SUBCATEGORY_PARENT["镜子与地毯"] == "家具与电器")
    check("反光灯归骑行配件（提示词里写着）", "反光灯" in dict(SUBCATEGORY_TREE["自行车与滑板车"])["骑行配件"])
    check("子类目属于大类 → 原样", valid_subcategory("自行车与滑板车", "骑行配件") == "骑行配件")
    check("子类目不属于大类 → None", valid_subcategory("电子产品", "骑行配件") is None)
    block = subcategory_prompt_block()
    check("提示词块里有全部子类目", all(s in block for s in SUBCATEGORY_PARENT))


# ============================================================
# 二、中心词的不变量：必须在标题里
# ============================================================
def test_head_noun_invariant() -> None:
    from app.config import valid_head_noun

    section("中心词：必须在标题里")
    check("原样在标题里", valid_head_noun("白色LED阅读落地灯", "落地灯") == "落地灯")
    check("模型补全了写法 → 退到标题里的最长后缀",
          valid_head_noun("白色第三方双模手柄", "游戏手柄") == "手柄")
    check("标题里完全没有 → None", valid_head_noun("蓝色 Kindle Paperwhite 8GB", "电子阅读器") is None)
    check("单字太泛 → None", valid_head_noun("白色台灯", "灯") is None)
    check("卖家改了标题、中心词不在了 → None", valid_head_noun("白色落地灯", "台灯") is None)
    check("大小写不敏感", valid_head_noun("Logitech MIDI键盘", "midi键盘") == "midi键盘")


# ============================================================
# 三、上架：草稿和发布
# ============================================================
def test_publishing_schema() -> None:
    from app.services.agents.publishing.schemas import DraftAttributes, PublishApproveIn
    from app.services.agents.publishing import approve as approve_mod

    section("上架：子类目跟着大类校验")
    a = DraftAttributes(category="家居与厨房", subcategory="灯具与装饰", head_noun="台灯")
    check("合法的子类目留下", a.subcategory == "灯具与装饰")
    a = DraftAttributes(category="电子产品", subcategory="灯具与装饰")
    check("不属于大类的子类目被丢掉（不拒绝整份草稿）", a.subcategory is None)
    a = DraftAttributes(category=None, subcategory="灯具与装饰")
    check("没有大类就没有子类目", a.subcategory is None)

    payload = PublishApproveIn(
        thread_id="t", name="白色台灯", description="d", category="家居与厨房",
        condition="全新", price=10, pickup_location_name="x",
    )
    check("发布请求不带子类目也合法（旧前端、脚本、夹具）", payload.subcategory is None)

    src = _bootstrap.module_source(approve_mod)
    check("发布时子类目不合法 → 落到「其他…」",
          "or other_subcategory(payload.category)" in src)
    check("发布时中心词按**最终标题**判", "valid_head_noun(name, payload.head_noun)" in src)

    from app.services.agents.publishing import pipeline
    check("抽取提示词的类目树由 config 填入",
          '"{{SUBCATEGORY_TREE}}", subcategory_prompt_block()' in _bootstrap.module_source(pipeline))


# ============================================================
# 四、买家侧不受影响
# ============================================================
def test_buyer_index_untouched() -> None:
    from types import SimpleNamespace

    from app.services.item_index import build_index_document, build_indexed_item

    section("买家侧检索：新字段不进索引")
    item = SimpleNamespace(
        id=1, name="白色台灯", summary="s", description="d", price=10.0, status="selling",
        lat=0.0, lng=0.0, pickup_location_name="p", category=None, brand=None, color=None,
        condition=None,
        attributes={"category": "家居与厨房", "subcategory": "灯具与装饰", "head_noun": "台灯",
                    "condition": "全新", "material": "金属"},
    )
    doc = build_index_document(item)
    check("索引文档里没有子类目", "灯具与装饰" not in doc and "subcategory" not in doc, doc)
    check("索引文档里没有 head_noun 键", "head_noun" not in doc, doc)
    check("别的扩展属性照旧进索引", "material: 金属" in doc, doc)
    rec = build_indexed_item(item)
    check("检索镜像的扩展属性里也没有",
          "subcategory" not in rec.extra_attributes and "head_noun" not in rec.extra_attributes)


# ============================================================
# 五、查找第一层：中心词
# ============================================================
def test_lexical_tier() -> None:
    from app.services.agents.item_lookup import lexical_matches

    section("查找第一层：中心词后缀关系")

    def names(ref):
        return [m["name"] for m in lexical_matches(ref, LISTINGS)[0]]

    check("「自行车反光灯」不认领自行车（自行车只是修饰语）", names("自行车反光灯") == [])
    check("「我的自行车」→ 自行车", names("我的自行车") == ["Giant白色通勤自行车"])
    check("「耳机」→ 无线耳机（泛称认领具体的）", names("耳机") == ["Anker降噪无线耳机"])
    check("「耳机架」不认领耳机", names("耳机架") == [])
    check("「西班牙语教材」只认领那一本", names("西班牙语教材") == ["中级西班牙语教材"])
    check("「教材」认领两本（真的不唯一）", len(names("教材")) == 2)
    check("「德语教材」修饰词一个都对不上 → 这一层不判", names("德语教材") == [])
    check("「台灯」字面对不上书桌灯 → 这一层不判，交给模型", names("台灯") == [])
    check("没有中心词的旧商品按标题分词", names("音箱") == ["蓝牙音箱"])


# ============================================================
# 六、查找第二层：模型 + 服务端的子类目过滤
# ============================================================
def _stub(verdicts: dict):
    from app.services.agents import item_lookup

    calls: list[str] = []

    def fake(ref, message, rows):
        calls.append(ref)
        v = verdicts.get(ref)
        return None if v is None else item_lookup._LookupVerdict(**v)

    return fake, calls


def test_model_tier() -> None:
    from app.services.agents import item_lookup as L

    section("查找第二层：模型判定，子类目在服务端核对")
    saved = L.ask_model
    fake, calls = _stub({
        "台灯": {"subcategory": "灯具与装饰", "head_noun": "台灯", "item_ids": [2]},
        # 模型犯错：把自行车当成了反光灯。它同时说了反光灯是骑行配件——信后一句。
        "自行车反光灯": {"subcategory": "骑行配件", "head_noun": "反光灯", "item_ids": [1]},
        "瞎编的": {"subcategory": "不存在的子类目", "item_ids": [1]},
        "别人的": {"subcategory": "自行车", "item_ids": [999]},
    })
    L.ask_model = fake
    L._CACHE.clear()
    try:
        r = L.resolve_ref("台灯", LISTINGS)
        check("「台灯」→ LED护眼书桌灯", [m["name"] for m in r.matches] == ["LED护眼书桌灯"])
        check("来源记为 model", r.source == L.SOURCE_MODEL)

        r = L.resolve_ref("自行车反光灯", LISTINGS)
        check("模型说是那辆自行车，但子类目对不上 → 服务端丢掉", r.matches == [])
        check("判过了、确实没有（不是没判成）", r.decided and r.source == L.SOURCE_NONE)

        r = L.resolve_ref("瞎编的", LISTINGS)
        check("模型给了不存在的子类目 → 算没判成", r.source == L.SOURCE_UNAVAILABLE)

        r = L.resolve_ref("别人的", LISTINGS)
        check("模型给了不属于这个卖家的 id → 丢掉", r.matches == [])

        r = L.resolve_ref("没人知道", LISTINGS)
        check("模型调用失败 → 算没判成，不是「没有」", r.source == L.SOURCE_UNAVAILABLE)

        before = len(calls)
        L.resolve_ref("台灯", LISTINGS)
        check("同一个说法、同一份清单不重复调模型", len(calls) == before)

        before = len(calls)
        L.resolve_ref("我的自行车", LISTINGS)
        check("第一层判出来了就不调模型", len(calls) == before)
    finally:
        L.ask_model = saved
        L._CACHE.clear()


# ============================================================
# 七、闸门：两个 badcase
# ============================================================
def test_gate() -> None:
    from app.services.agents import item_lookup as L
    from app.services.agents import router as R
    from app.services.nlp import intent_rules

    section("闸门：两个 badcase")
    saved = L.ask_model
    fake, _ = _stub({
        "台灯": {"subcategory": "灯具与装饰", "head_noun": "台灯", "item_ids": [2]},
        "自行车反光灯": {"subcategory": "骑行配件", "head_noun": "反光灯", "item_ids": []},
        "杯子": {"subcategory": "水杯水瓶", "head_noun": "杯子", "item_ids": []},
    })
    L.ask_model = fake
    L._CACHE.clear()

    def run(msg, refs, *, with_resolution=True):
        hints = intent_rules.hints(msg, LISTINGS)
        slots = {"item_ref": refs[0] if refs else ""}
        to_resolve = R.needs_item_resolution("sales_agent", slots, refs, hints, False)
        res = L.resolve_refs(to_resolve, LISTINGS, message=msg) if with_resolution else None
        route, missing = R.apply_missing_item_gate(
            "sales_agent", slots, refs, hints, LISTINGS, False, resolution=res or None)
        return route, missing, res, hints

    try:
        route, missing, res, _ = run("我的库里有台灯吗", ["台灯"])
        check("badcase 1：「台灯」找到了书桌灯 → 不再劝上架", route == "sales_agent", route)
        bound, aliases = L.bind(["台灯"], res)
        check("交给顾问的是那件商品的标题", bound == ["LED护眼书桌灯"], str(bound))
        check("并记下叫法不同、要在回复里说明", aliases == [{"said": "台灯", "item": "LED护眼书桌灯"}])

        route, missing, _, hints = run("我的商品库里面有没有自行车反光灯", ["自行车反光灯"])
        check("（前提）整条消息的分词仍然命中了自行车",
              any(m["item_id"] == 1 for m in hints["name_matches"]))
        check("badcase 2：不再被自行车放行 → item_not_found",
              route == R.MISSING_ITEM_ROUTE and missing == ["自行车反光灯"], f"{route} {missing}")

        route, _, _, _ = run("我的商品库里面有没有自行车反光灯", ["自行车反光灯"],
                             with_resolution=False)
        check("（对照）不给判定结果时是老行为：被自行车放行", route == "sales_agent")

        route, _, _, _ = run("蓝牙音箱那个杯子", ["杯子"])
        check("防误伤线还在：消息别处提到的在售商品照样放行", route == "sales_agent")

        L._CACHE.clear()
        L.ask_model = lambda *a, **k: None
        route, _, _, _ = run("我的库里有台灯吗", ["台灯"])
        # 老判据下「台灯」和「LED护眼书桌灯」分词不重合，所以是 item_not_found——
        # 这一条断言的是**退回了**，不是退回之后的结论对不对。
        check("模型调不通 → 整轮退回老判据", route == R.MISSING_ITEM_ROUTE, route)
        res = L.resolve_refs(["台灯"], LISTINGS)
        check("  且这一轮的判定被标成没判成", not res["台灯"].decided)

        check("带照片的一轮不查", R.needs_item_resolution(
            "sales_agent", {"item_ref": "台灯"}, ["台灯"], {}, True) == [])
        check("状态变更不查", R.needs_item_resolution(
            "sales_agent", {"item_ref": "台灯", "status_change": "pause"}, ["台灯"], {}, False) == [])
        check("不是销售分析的路由不查", R.needs_item_resolution(
            "publishing_agent", {"item_ref": "台灯"}, ["台灯"], {}, False) == [])
    finally:
        L.ask_model = saved
        L._CACHE.clear()


# ============================================================
# 八、顾问那一侧
# ============================================================
def test_advisor_side() -> None:
    from app.services.agents import router as R
    from app.services.agents.graph import nodes
    from app.services.agents.sales.orchestrator import _ambiguity_block

    section("顾问：自报家门；路由器看到的清单不变")
    note = _ambiguity_block({"kind": "resolved", "aliases": [
        {"said": "台灯", "item": "LED护眼书桌灯"}]}, ["LED护眼书桌灯"])
    check("叫法不同时要求回复第一句说明按哪件看", "台灯" in note and "LED护眼书桌灯" in note
          and "第一句" in note, note)
    check("没有别名时不加话", _ambiguity_block({"kind": "resolved", "aliases": []}, []) == "")

    import json
    facts = json.loads(R._facts_block(True, LISTINGS * 3, False))
    rows = facts["listings"]
    check("路由器只看到前 10 件", len(rows) == 10)
    check("路由器只看到原来那四个字段",
          all(set(r) == {"item_id", "name", "price", "category"} for r in rows), str(rows[0]))
    check("查找能看到的上限放宽到 100 件", nodes.MAX_SELLER_LISTINGS == 100)


# ============================================================
# 九、第一批全局：浏览筛选、索引 metadata
# ============================================================
def test_browse_and_metadata() -> None:
    from datetime import datetime
    from types import SimpleNamespace

    from app.services.browse import BrowseFilters, filter_and_sort
    from app.services.item_index import build_index_metadata, build_indexed_item

    section("浏览：子类目二级筛选；索引 metadata 带子类目")

    def item(i, name, cat, sub):
        return SimpleNamespace(
            id=i, name=name, summary="", description="", price=10.0, status="selling",
            lat=0.0, lng=0.0, pickup_location_name="p", created_at=datetime(2026, 10, 1, 0, i),
            attributes={"category": cat, "subcategory": sub, "condition": "全新"})

    rows = [item(1, "白色台灯", "家居与厨房", "灯具与装饰"),
            item(2, "保温杯", "家居与厨房", "水杯水瓶"),
            item(3, "老商品", "家居与厨房", None)]
    got = [i.id for i in filter_and_sort(rows, None, BrowseFilters(category="家居与厨房", subcategory="灯具与装饰"))]
    check("大类 + 子类目 → 只剩那一个子类目", got == [1], str(got))
    got = [i.id for i in filter_and_sort(rows, None, BrowseFilters(category="家居与厨房"))]
    check("不选子类目 → 整个大类（含没回填的老商品）", sorted(got) == [1, 2, 3], str(got))
    got = [i.id for i in filter_and_sort(rows, None, BrowseFilters(subcategory="水杯水瓶"))]
    check("只给子类目也能筛（名字全局唯一）", got == [2], str(got))

    meta = build_index_metadata(rows[0])
    check("向量 metadata 带子类目", meta.get("subcategory") == "灯具与装饰", str(meta))
    check("没有子类目时是空串（Chroma 不收 None）", build_index_metadata(rows[2])["subcategory"] == "")
    check("检索镜像带子类目", build_indexed_item(rows[0]).subcategory == "灯具与装饰")

    from app.routers import items as items_router
    import inspect
    check("浏览接口收 subcategory 参数",
          "subcategory" in inspect.signature(items_router.browse).parameters)


# ============================================================
# 十、出售计划：绑错就会收错东西
# ============================================================
def test_sale_plan_binding() -> None:
    from types import SimpleNamespace

    from app.services.agents import item_lookup as L
    from app.services.agents import router as R
    from app.services.agents.planning import tools as T
    from app.services.nlp import intent_rules

    section("出售计划：绑定只认唯一命中；在不在库按查找判")

    rows = [
        SimpleNamespace(id=r["item_id"], name=r["name"], status="selling",
                        attributes={k: r.get(k) for k in ("category", "subcategory", "head_noun")})
        for r in LISTINGS
    ]

    class _Repo:
        def __init__(self, db):
            pass

        def list_by_seller(self, seller_id):
            return rows

    saved_repo, saved_ask = T.ItemRepository, L.ask_model
    fake, calls = _stub({
        "自行车反光灯": {"subcategory": "骑行配件", "head_noun": "反光灯", "item_ids": [1]},
        "台灯": {"subcategory": "灯具与装饰", "head_noun": "台灯", "item_ids": [2]},
    })
    T.ItemRepository, L.ask_model = _Repo, fake
    L._CACHE.clear()
    try:
        # 旧判据下这一条会绑上那辆自行车，然后 _hide_for_plan 把它收进隐身。
        old = intent_rules.best_match(intent_rules.match_listings(
            "自行车反光灯", [{"item_id": r.id, "name": r.name} for r in rows]))
        check("（对照）旧判据把「自行车反光灯」绑到自行车", old and old["item_id"] == 1)
        check("「自行车反光灯期末再卖」不再绑自行车", T._match_listing(None, 1, "自行车反光灯") is None)
        check("「自行车」照样绑上", T._match_listing(None, 1, "自行车") == 1)
        check("「台灯」经模型认出书桌灯 → 绑上", T._match_listing(None, 1, "台灯") == 2)
        check("「教材」对上两本 → 不绑（不替卖家挑一本收起来）", T._match_listing(None, 1, "教材") is None)
        L._CACHE.clear()
        L.ask_model = lambda *a, **k: None
        check("模型调不通 → 不绑", T._match_listing(None, 1, "台灯") is None)

        L.ask_model = fake
        L._CACHE.clear()
        msg = "自行车反光灯期末再卖"
        hints = intent_rules.hints(msg, LISTINGS)
        check("（前提）整条消息的分词命中了自行车", bool(hints["name_matches"]))
        check("点名了反光灯 → 判为不在库（先上架）",
              R.defer_item_in_stock({"item_ref": "自行车反光灯"}, hints, LISTINGS, msg) is False)
        check("点名了自行车 → 在库",
              R.defer_item_in_stock({"item_ref": "自行车"}, intent_rules.hints("自行车期末再卖", LISTINGS),
                                    LISTINGS, "自行车期末再卖") is True)
        check("路由器没点名 → 退回分词（原来的判据）",
              R.defer_item_in_stock({}, hints, LISTINGS, msg) is True)
        L._CACHE.clear()
        L.ask_model = lambda *a, **k: None
        check("查找没判成 → 也退回分词",
              R.defer_item_in_stock({"item_ref": "台灯"}, {"name_matches": []}, LISTINGS, "台灯期末再卖") is False)
    finally:
        T.ItemRepository, L.ask_model = saved_repo, saved_ask
        L._CACHE.clear()


# ============================================================
# 十一、下架/暂停 和 无模型的计划兜底
# ============================================================
def test_status_and_fallback_binding() -> None:
    from datetime import date, timedelta
    from types import SimpleNamespace

    from app import services
    from app.services.agents import item_lookup as L
    from app.services.agents.graph import nodes as N

    section("名词短语：没有 item_ref 时整条消息怎么切")
    check("修饰语不单独成短语", L.noun_phrases("把自行车反光灯下架") == ["自行车反光灯"])
    check("时间词不粘进短语", L.noun_phrases("20寸折叠自行车下学期再卖") == ["自行车"])
    check("「多少钱」（jieba 标成人名）不粘进短语", L.noun_phrases("自行车多少钱")[0] == "自行车")
    check("整条消息：反光灯不认领自行车", L.message_matches("把自行车反光灯下架", LISTINGS) == [])
    check("整条消息：自行车认领自行车",
          [m["item_id"] for m in L.message_matches("我库里那辆自行车下架", LISTINGS)] == [1])

    saved_ask = L.ask_model
    calls: list[str] = []
    L.ask_model = lambda ref, *a, **k: calls.append(ref)
    L._CACHE.clear()
    srcs = [L.resolve_ref(r, LISTINGS).source for r in ("它", "这个", "它们", "那几件", "这两件")]
    check("代词不拿去问模型，交回指代逻辑",
          all(x == L.SOURCE_UNAVAILABLE for x in srcs) and not calls, f"{srcs} {calls}")
    L.ask_model = saved_ask

    section("下架/暂停：点了名就按名字找，找不到就直说")
    rows = [
        SimpleNamespace(id=r["item_id"], name=r["name"], status="selling",
                        attributes={k: r.get(k) for k in ("category", "subcategory", "head_noun")})
        for r in LISTINGS
    ]
    holder = {"rows": rows}

    class _Repo:
        def __init__(self, db):
            pass

        def list_by_seller(self, seller_id):
            return holder["rows"]

    fake, _ = _stub({
        "自行车反光灯": {"subcategory": "骑行配件", "head_noun": "反光灯", "item_ids": [1]},
        "台灯": {"subcategory": "灯具与装饰", "head_noun": "台灯", "item_ids": [2]},
    })
    saved_repo = N.ItemRepository
    N.ItemRepository, L.ask_model = _Repo, fake
    L._CACHE.clear()

    def resolve(msg, ref="", last=None):
        state = {"message": msg, "slots": {"item_ref": ref}, "last_context": last or {}}
        item, why = N._resolve_for_status(None, 1, state, "pause")
        return (item.id if item else None), why

    try:
        got, why = resolve("把自行车反光灯下架", "自行车反光灯")
        check("「把自行车反光灯下架」不绑自行车，直说没找到", got is None and "没找到" in why, why)
        check("「把自行车下架」→ 自行车", resolve("把自行车下架", "自行车")[0] == 1)
        check("「把台灯下架」经模型认出书桌灯", resolve("把台灯下架", "台灯")[0] == 2)
        got, why = resolve("把教材下架", "教材")
        check("「教材」两本 → 问是哪一本", got is None and "哪一件" in why, why)
        check("代词 + 上一轮的商品 → 上一轮那件", resolve("把它下架", "它", {"item_id": 6})[0] == 6)
        got, why = resolve("把自行车反光灯下架")
        check("没有 item_ref 时整条消息也不绑自行车", got is None, str(got))

        holder["rows"] = [rows[0]]
        L._CACHE.clear()
        got, why = resolve("把自行车反光灯下架", "自行车反光灯")
        check("只有一件在售时，点名的东西对不上也**不**落到「就是它」",
              got is None and "没找到" in why, f"{got} {why}")
        check("只有一件在售、只说「下架吧」→ 还是它（原来的兜底不变）",
              resolve("下架吧")[0] == 1)
    finally:
        N.ItemRepository, L.ask_model = saved_repo, saved_ask
        L._CACHE.clear()

    section("无模型的计划兜底：只走第一层，只认唯一命中")
    created: list[dict] = []
    sp = services.sale_plans
    saved_sp = (sp.create_plan, sp.active_plan_count)

    def fake_create(db, seller_id, **kw):
        created.append(kw)
        t = kw["target_date"]
        return SimpleNamespace(id=1, item_name=kw["item_name"], category=None, target_date=t,
                               watch_from=t - timedelta(weeks=4), date_basis=kw.get("date_basis", ""))

    sp.create_plan, sp.active_plan_count = fake_create, (lambda db, sid: 0)
    db = SimpleNamespace(get=lambda *a, **k: None)
    target = (date.today() + timedelta(days=60)).isoformat()

    def plan(msg, ref=""):
        created.clear()
        N._deterministic_plan_node(
            {"message": msg, "seller_id": 1, "turns": [], "listings": LISTINGS,
             "hints": {"defer_target_date": target}, "slots": {"item_ref": ref}},
            {"configurable": {"db": db}},
        )
        return created[0].get("item_id") if created else "no plan"

    try:
        check("「自行车反光灯期末再卖」→ 计划不挂到自行车", plan("自行车反光灯期末再卖", "自行车反光灯") is None)
        # 没点名、分词也认不出唯一的一件 → 商品名是空的，这条路问一句而不是建计划。
        check("没有 item_ref 时不挂，回头问卖家", plan("自行车反光灯期末再卖") == "no plan")
        check("「自行车期末再卖」→ 挂到自行车", plan("自行车期末再卖") == 1)
        check("「教材期末再卖」两本 → 不挂", plan("教材期末再卖", "教材") is None)
    finally:
        sp.create_plan, sp.active_plan_count = saved_sp


# ============================================================
# 十二、真模型（--live）
# ============================================================
def test_live() -> None:
    from app.services.agents import item_lookup as L
    from app.services.agents.publishing.pipeline import extract_listing

    section("真模型：查找")
    L._CACHE.clear()
    cases = [
        ("台灯", "我的库里有台灯吗", [2]),
        ("自行车反光灯", "我的商品库里面有没有自行车反光灯", []),
        ("那辆Giant", "那辆Giant卖得怎么样", [1]),
        ("书桌台灯", "我的书桌台灯有人要吗", [2]),
        ("落地灯", "落地灯定价合适吗", [3]),
        ("降噪耳机", "降噪耳机卖得怎么样", [6]),
    ]
    for ref, msg, want in cases:
        r = L.resolve_ref(ref, LISTINGS, message=msg)
        got = sorted(int(m["item_id"]) for m in r.matches)
        check(f"「{ref}」→ {want}", r.decided and got == sorted(want),
              f"got {got} via {r.source} ({r.subcategory})")

    section("真模型：上架抽取带出子类目和中心词")
    draft, _, _ = extract_listing(None, text="出一个白色护眼台灯，宿舍用了半年，灯光可以调三档")
    a = draft.attributes
    print(f"  name={draft.name!r} category={a.category} subcategory={a.subcategory} head={a.head_noun!r}")
    check("子类目是灯具与装饰", a.subcategory == "灯具与装饰", str(a.subcategory))
    check("中心词在标题里", bool(a.head_noun) and a.head_noun in draft.name, f"{a.head_noun} / {draft.name}")


def main() -> int:
    test_taxonomy()
    test_head_noun_invariant()
    test_publishing_schema()
    test_buyer_index_untouched()
    test_lexical_tier()
    test_model_tier()
    test_gate()
    test_advisor_side()
    test_browse_and_metadata()
    test_sale_plan_binding()
    test_status_and_fallback_binding()
    if "--live" in sys.argv:
        test_live()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())

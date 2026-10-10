"""Part F — cross-agent multi-intent (n=30). Added after the first 500.

One message, two specialists' worth of work: *list this thing* and *tell me what
it is worth / who wants it*. Every row is a seller with nothing listed yet,
because that is the state in which the two halves genuinely cannot both be
served — with an active listing the sales half has an item_id to stand on and
the question stops being interesting.

These rows do not test the router the way A–D do. The router's answer here is
not in doubt: the written prompt sends a zero-listing seller to the publishing
agent, and that is the correct first hop. What they test is what happens to the
*other* half of the sentence. Three outcomes are distinguishable and they are
not equally good:

  * answered      — the reply engages the pricing / demand question somehow
  * acknowledged  — the reply says it will come back to it after listing
  * dropped       — the reply behaves as if that half was never said

Silent dropping is the failure this part exists to measure. It is invisible in
any L1/L2 accuracy number, because the route was right.

`gold_seq` on these leaves records the product-ideal sequence rather than what
the prompt derives, so the rows are kept out of the headline denominators
(taxonomy.SCORED_SEPARATELY) and reported on their own.
"""

from __future__ import annotations

from _base import Q

# ==================================================================
# F1 · 挂售 + 定价 (n=10) — 第二意图 price
# ==================================================================
Q("F1", "我要出售我的Giant山地自行车，但我不知道定价是否合理？", "multi_intent",
  "自行车与滑板车", seller="none")
Q("F1", "想把我的Switch Lite挂出来卖，389刀这个价站得住脚吗？", "multi_intent",
  "电子产品", seller="none")
Q("F1", "我有个Instant Pot电压力锅要出，不知道该标多少钱。", "multi_intent",
  "家居与厨房", seller="none")
Q("F1", "准备卖掉我那套微积分教材，同学们一般卖多少钱一本？", "multi_intent",
  "图书与教材", seller="none")
Q("F1", "我想挂一件Uniqlo的抓绒外套，定价多少合适？", "multi_intent",
  "服装与鞋靴", seller="none")
Q("F1", "帮我把这把雅马哈民谣吉他挂出去，顺便说一下这个价位靠不靠谱。", "detailed",
  "乐器", seller="none")
Q("F1", "出一个Wilson篮球，多少钱能出手？", "terse", "运动与户外", seller="none")
Q("F1", "新的马克笔套装想卖，价个怎么定", "typo", "学习用品", seller="none",
  quality="bad")
Q("F1", "我宿舍那张书桌要处理掉，这边二手家具都什么行情？", "multi_intent",
  "家具与电器", seller="none")
Q("F1", "I want to sell my MacBook Air. What's a fair price on here?", "non_english",
  "电子产品", seller="none")

# ==================================================================
# F2 · 挂售 + 需求 (n=10) — 第二意图 demand
# ==================================================================
Q("F2", "我要出售我的Trek山地自行车，最近有人要吗？", "multi_intent",
  "自行车与滑板车", seller="none")
Q("F2", "想挂个Lenovo平板，现在还有人在找平板吗？", "multi_intent",
  "电子产品", seller="none")
Q("F2", "打算卖掉我的JBL蓝牙音箱，这东西这学期好出吗？", "multi_intent",
  "电子产品", seller="none")
Q("F2", "我有一堆统计学教材要出，期末这会儿还有人收吗？", "multi_intent",
  "图书与教材", seller="none")
Q("F2", "准备挂一双几乎全新的跑鞋，有人在找吗？", "multi_intent",
  "服装与鞋靴", seller="none")
Q("F2", "想把电子琴卖了，学校里有人要乐器吗？", "multi_intent", "乐器", seller="none")
Q("F2", "我那个瑜伽垫想出掉，需求怎么样？", "multi_intent", "运动与户外", seller="none")
Q("F2", "挂个台灯，有人买这个吗", "terse", "家具与电器", seller="none")
Q("F2", "我打算卖我的小冰箱，搬宿舍的人会要吗？", "multi_intent",
  "家具与电器", seller="none")
Q("F2", "thinking of listing my road bike — is anyone actually looking for bikes rn?",
  "non_english", "自行车与滑板车", seller="none")

# ==================================================================
# F3 · 宽泛委托 (n=10) — 第二意图 both
#
# No explicit second question: the seller hands over the whole job. The reply
# should not fall apart into a clarify (the intent is unambiguous — sell this),
# and the interesting behaviour is whether it lays out what comes after listing
# rather than stopping at "what condition is it in".
# ==================================================================
Q("F3", "你能帮我卖我的Giant山地自行车吗？", "clear", "自行车与滑板车", seller="none")
Q("F3", "帮我把这个Switch Lite卖掉吧。", "clear", "电子产品", seller="none")
Q("F3", "我想出掉我的电压力锅，你能帮我搞定吗？", "clear", "家居与厨房", seller="none")
Q("F3", "帮我处理一下我这几本教材呗。", "longtail", "图书与教材", seller="none")
Q("F3", "我这件冲锋衣不想要了，你看着办？", "longtail", "服装与鞋靴", seller="none",
  quality="bad", conf="review",
  note="「你看着办」既像挂售委托也像放弃决策权；可能被判 clarify -> 人工裁定")
Q("F3", "能帮我把吉他出了吗", "terse", "乐器", seller="none")
Q("F3", "我想卖我的篮球，接下来该怎么做？", "clear", "运动与户外", seller="none")
Q("F3", "帮我卖这套马克笔，从头到尾你带我走一遍流程。", "detailed",
  "学习用品", seller="none")
Q("F3", "我要清一批宿舍里的东西，从哪开始？", "vague", "", seller="none",
  quality="bad", conf="review",
  note="批量清仓且未指明任何一件；publishing 与 clarify 均有据 -> 人工裁定")
Q("F3", "can you help me sell my desk lamp?", "non_english",
  "家具与电器", seller="none")

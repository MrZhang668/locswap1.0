"""Family A · 销售分析 (n=205) — everything that routes to `sales_agent`.

Split by scenario because scenario is what actually picks the tool:
A1/A2 -> price, A3/A4 -> demand, A5 -> find_buyers, A6 -> both, A7 -> none.
"""

from _base import Q

# ==================================================================
# A1 · 定价合理性评估 (n=40) — sales_agent / price
# ==================================================================
Q("A1", "Giant 通勤车卖279 刀合适吗？", "clear", "自行车与滑板车")
Q("A1", "Instant Pot 卖63 刀我是不是定太贵了？", "clear", "家居与厨房")
Q("A1", "Switch Lite 价格看看", "terse", "电子产品")
Q("A1", "我大概三周前把 Lenovo 平板挂478 刀了，到现在没人要。是10.2寸64GB，成色很好，原盒和充电器都在。这个价跟学校里这类东西一般卖的比到底站得住吗，还是我把市场看错了？", "detailed", "电子产品")
Q("A1", "jbl 音箱45会不会太贵", "longtail", "电子产品")
Q("A1", "我的机戒键盘卖多少比较合里？", "typo", "电子产品", seller="single")
Q("A1", "那个价格好吗？", "anaphora", "自行车与滑板车",
  ctx="你觉得我Giant 通勤车的帖子怎么样？")
Q("A1", "篮球我该卖多少", "clear", "运动与户外")
Q("A1", "硬壳万向轮行李箱85 刀——合理还是太黑？", "clear", "服装与鞋靴")
Q("A1", "这套马克笔我该卖贵点吗？", "clear", "学习用品")
Q("A1", "帮我看看阻力带卖12 刀这价正不正常。", "clear", "运动与户外")
Q("A1", "is this price reasonable? I listed my Switch Lite for $389", "non_english", "电子产品",
  note="英文输入；产品为全中文界面，测试语言鲁棒性")
Q("A1", "价格公道吗？", "terse", "", quality="bad",
  note="无指代对象且无上下文；按路由规则 4 仍属 sales_agent，槽位应全空")
Q("A1", "Uniqlo 束脚裤卖38 刀。学校里还有两个人在挂差不多的抓绒束脚裤，都是30 刀左右。我是不是定得没竞争力了？", "detailed", "服装与鞋靴")
Q("A1", "你会花28 刀买个旅行鼠标吗", "longtail", "电子产品")
Q("A1", "我想知道我的 cruiser longboard 要价能不能站得住脚。", "clear", "运动与户外")
Q("A1", "定价对吗？", "terse", "", quality="bad")
Q("A1", "成色好的双翘整板卖70 刀靠谱吗？", "clear", "运动与户外")
Q("A1", "我 Kindle 定120 刀价格对不对，告诉我。", "clear", "电子产品", seller="single",
  note="该卖家在售的只有机械键盘，Kindle 不在其名下 -> 专家应说明而非编造数据")
Q("A1", "switch lite我卖389是不是太离谱了", "longtail", "电子产品")
Q("A1", "6夸脱 Instant Pot Duo 卖63 美元是不是合适的数？", "clear", "家居与厨房")
Q("A1", "我该卖多少", "vague", "", quality="bad")
Q("A1", "我自行车价格是太高还是太低，你会定多少？", "clear", "自行车与滑板车")
Q("A1", "水壶63 刀。这个数咋样？", "terse", "家居与厨房")
Q("A1", "我觉得这平板478卖便宜了，你说服我不是这样", "clear", "电子产品")
Q("A1", "嘿那个自行车，279 刀行不行？", "longtail", "自行车与滑板车")
Q("A1", "帮我看看我挂的所有东西价格合不合理，从最贵的开始。", "multi_intent", "",
  note="跨全部 listing；scenario=price，item_ref 应为空")
Q("A1", "那个无线鼠标的价格跟这上面别的比有竞争力吗？", "clear", "电子产品")
Q("A1", "看看价格", "terse", "", quality="bad")
Q("A1", "Wilson 7号室内外篮球卖23刀在这个学校算便宜吗？", "detailed", "运动与户外")
Q("A1", "我的行李箱挂85有一阵了，是不是价格有问题", "longtail", "服装与鞋靴")
Q("A1", "一套60色双头马克笔二手值多少钱", "clear", "学习用品")
Q("A1", "用了大概五次的引体向上阻力带卖12刀合理吗？", "detailed", "运动与户外")
Q("A1", "评估下我的价格", "vague", "", quality="bad")
Q("A1", "麻烦说下giant自行车279合不合理，谢啦", "typo", "自行车与滑板车")
Q("A1", "JBL音箱：45刀是比大家实际买的高还是低？", "clear", "电子产品")
Q("A1", "我还没挂任何东西，但想知道这里二手通勤自行车一般卖多少。", "clear",
  "自行车与滑板车", seller="none", agent="publishing_agent", scn="none", conf="review", second="price", req=(),
  note="has_active_items=false -> 销售类问题改判 publishing_agent。第二意图(定价)未被应答（场景必须是 none：_sanitize() 对非 sales 路由强制归零，price/demand 这类金标系统永远产不出来）")
Q("A1", "Switch Lite屏幕有划痕卖389刀合理吗？", "detailed", "电子产品")
Q("A1", "给我的束脚裤估个价", "terse", "服装与鞋靴")
Q("A1", "两块滑板——是不是有哪个定价不对？", "ambiguous", "运动与户外",
  note="两块滑板都在售；路由规则 4 明确说'哪个 item'的歧义不构成 clarify")

# ==================================================================
# A2 · 成交价对比/降价决策 (n=32) — sales_agent / price
# ==================================================================
Q("A2", "这上面类似的通勤自行车实际都卖了多少？", "clear", "自行车与滑板车")
Q("A2", "我该给Lenovo平板降价吗？", "clear", "电子产品")
Q("A2", "给我看看高压锅的成交价", "terse", "家居与厨房")
Q("A2", "这个平板挂了一个月都没人预订。我宁愿赶紧卖掉也不想死守478刀。这几个最近实际都卖多少，我得降多少才能这周成交？", "detailed", "电子产品")
Q("A2", "别的switch lite都卖多少了", "longtail", "电子产品")
Q("A2", "你马上要告诉我的那个价格，背后有真实成交价证据吗？", "clear", "")
Q("A2", "我该降到250吗", "anaphora", "自行车与滑板车",
  ctx="这辆Giant通勤自行车挂了22天都没人预订。")
Q("A2", "最近类似滑板都成交在多少？", "clear", "运动与户外")
Q("A2", "降价？", "terse", "", quality="bad")
Q("A2", "给我看看二手蓝牙音箱的成交价区间，我好决定。", "clear", "电子产品")
Q("A2", "我要看我行李箱的成交对比，不要瞎猜。", "clear", "服装与鞋靴")
Q("A2", "这里二手机戒键盘一般卖多少", "typo", "电子产品", seller="single")
Q("A2", "Instant Pot 要降多少才能这周卖掉？", "clear", "家居与厨房")
Q("A2", "any price references for the same item that sold", "non_english", "")
Q("A2", "成交对比。", "terse", "", quality="bad")
Q("A2", "篮球卖23刀一直没人买。降还是等等？", "clear", "运动与户外")
Q("A2", "过去90天平板的成交价中位数是多少？", "detailed", "电子产品")
Q("A2", "我想知道大家实际付了多少不是他们标多少", "longtail", "")
Q("A2", "给我看看证据，证明这自行车能卖279刀。", "clear", "自行车与滑板车")
Q("A2", "马克笔套装最低能降到多少，再低就不值得卖了？", "clear", "学习用品")
Q("A2", "麻烦看下阻力带的价格历史。", "clear", "运动与户外")
Q("A2", "学期末这波要不要给运动裤降价？", "clear", "服装与鞋靴")
Q("A2", "降价", "terse", "", quality="bad")
Q("A2", "鼠标从35刀降到28刀了还是没动静。第一次是不是降太少了？", "detailed", "电子产品")
Q("A2", "上一个 cruiser longboard 卖了多少，什么时候卖的", "clear", "运动与户外")
Q("A2", "把我所有商品的标价和实际成交价对比一下。", "multi_intent", "")
Q("A2", "打折到底有没有用，还是根本没人要？", "ambiguous", "", scn="both", conf="review",
  note="字面同时问定价与需求；标注 both，模型判 price 或 demand 亦有据 -> 人工裁定")
Q("A2", "6 quart instant pot duo 的成交对比，最近30天", "clear", "家居与厨房")
Q("A2", "根本没人买 Switch Lite。那些卖掉的都卖了多少？", "clear", "电子产品")
Q("A2", "给我一个自行车快速出手价和一个能等等的价。", "clear", "自行车与滑板车")
Q("A2", "我从来没挂过东西。这里二手平板都卖多少？", "clear", "电子产品",
  seller="none", agent="publishing_agent", scn="none", conf="review", second="price", req=(),
  note="零在售 -> 同 A1 的边界规则。第二意图(定价)未被应答（场景必须是 none：_sanitize() 对非 sales 路由强制归零，price/demand 这类金标系统永远产不出来）")
Q("A2", "它最后卖了多少", "anaphora", "", quality="bad",
  ctx="你能看看 Instant Pot 吗？")

# ==================================================================
# A3 · 需求热度查询 (n=40) — sales_agent / demand
# ==================================================================
Q("A3", "现在真的有人在找通勤自行车吗？", "clear", "自行车与滑板车")
Q("A3", "有多少人搜过跟我 Instant Pot 差不多的东西？", "clear", "家居与厨房")
Q("A3", "Switch Lite有需求吗？", "terse", "电子产品")
Q("A3", "我在纠结Lenovo平板要不要挂到期末，还是撤下来去校外卖掉。我想知道的是这里到底有多少人真搜平板，不是碰巧在附近的人有多少。", "detailed", "电子产品")
Q("A3", "这个学校有人想要滑板吗", "longtail", "运动与户外")
Q("A3", "有多少买家在找蓝球？", "typo", "运动与户外")
Q("A3", "有人在找吗？", "anaphora", "服装与鞋靴",
  ctx="我想聊聊那个硬壳万向轮行李箱。")
Q("A3", "Bluetooth音箱的搜索量怎么样？", "clear", "电子产品")
Q("A3", "有兴趣吗？", "terse", "", quality="bad")
Q("A3", "告诉我这里二手机戒键盘市场有多大。", "clear", "电子产品", seller="single")
Q("A3", "how many people are looking for a Bluetooth speaker like mine", "non_english", "电子产品")
Q("A3", "有人会搜马克笔套装这种东西吗？", "clear", "学习用品")
Q("A3", "阻力带有多火", "clear", "运动与户外")
Q("A3", "告诉我搜运动裤的不同买家有多少，少的话也老实说。", "detailed", "服装与鞋靴")
Q("A3", "有需求吗", "terse", "", quality="bad")
Q("A3", "有人搜无线鼠标吗还是这是个死类目？", "clear", "电子产品")
Q("A3", "我哪个商品背后的需求最大？", "clear", "", note="跨全部 listing 的需求对比")
Q("A3", "现在有多少人想要像我这样的自行车", "longtail", "自行车与滑板车")
Q("A3", "学校里总体来说厨房电器有需求吗？", "clear", "家居与厨房")
Q("A3", "有人想要7号篮球吗？", "clear", "运动与户外")
Q("A3", "我决定要不要费这劲之前，先查下行李箱需求。", "clear", "服装与鞋靴")
Q("A3", "我的平板属于有人在乎的类目吗？", "clear", "电子产品")
Q("A3", "有人找长板吗？", "terse", "运动与户外")
Q("A3", "我想要的是实际搜索跟这个商品匹配的人数，别给我类目总数。", "detailed", "",
  note="直接考 prompt 里 total_people vs people_matching_item 的口径要求")
Q("A3", "Giant自行车有多少兴趣", "clear", "自行车与滑板车")
Q("A3", "这个时候有人买学习用品吗？", "clear", "学习用品")
Q("A3", "Switch Lite这个类目火不火", "longtail", "电子产品")
Q("A3", "Instant Pot 过去7天的搜索热度。", "clear", "家居与厨房")
Q("A3", "没人预留这条运动裤。是根本没人找还是我这帖子太烂？", "ambiguous", "服装与鞋靴", conf="review",
  note="前半是需求问题，后半暗示文案问题 -> 可能被判 clarify 或 publishing_agent；人工裁定")
Q("A3", "需求数据pls", "terse", "", quality="bad")
Q("A3", "按这个热度看鼠标还值得一直挂着吗？", "clear", "电子产品")
Q("A3", "我最贵那个商品需求怎么样？", "clear", "电子产品")
Q("A3", "有人想要60色马克笔套装吗？", "longtail", "学习用品")
Q("A3", "我现在没卖东西，但想挂一辆车之前先知道这里自行车好不好卖。", "clear",
  "自行车与滑板车", seller="none", agent="publishing_agent", scn="none", conf="review", second="demand", req=(),
  note="零在售边界。第二意图(需求)未被应答（场景必须是 none：_sanitize() 对非 sales 路由强制归零，price/demand 这类金标系统永远产不出来）")
Q("A3", "两个滑板的热度。", "ambiguous", "运动与户外")
Q("A3", "这周有多少搜索匹配到我的帖子", "clear", "")
Q("A3", "自行车需求够不够强，我是不是不用降价？", "clear", "自行车与滑板车")
Q("A3", "人真的会搜这种东西吗", "longtail", "", quality="bad")
Q("A3", "量化一下买家对Uniqlo运动裤的兴趣。", "clear", "服装与鞋靴")
Q("A3", "平台在运动与户外这类买家够多吗，值得我花时间吗？", "clear", "运动与户外")

# ==================================================================
# A4 · 需求趋势 / 滞销归因 (n=30) — sales_agent / demand
# ==================================================================
Q("A4", "我的Lenovo平板为啥卖不掉？", "clear", "电子产品")
Q("A4", "这学期通勤自行车的热度是在涨还是跌？", "clear", "自行车与滑板车")
Q("A4", "滑板的趋势", "terse", "运动与户外")
Q("A4", "Instant Pot 第一周浏览很多，之后几乎没啥了。我想知道这是正常的热度衰减，还是开学搬进来那周之后厨房用品的需求真的掉下去了。", "detailed", "家居与厨房")
Q("A4", "switch lite的需求是不是快没了", "longtail", "电子产品")
Q("A4", "篮求的兴取从赛季开始后是不是降了？", "typo", "运动与户外")
Q("A4", "是在涨还是跌？", "anaphora", "服装与鞋靴",
  ctx="给我看看Uniqlo运动裤的需求情况。")
Q("A4", "给我看平板过去90天的需求。", "clear", "电子产品")
Q("A4", "为啥没人问", "terse", "", quality="bad")
Q("A4", "开学的时候自行车搜索是不是有一波高峰我错过了？", "detailed", "自行车与滑板车")
Q("A4", "what's the demand trend for speakers this semester", "non_english", "电子产品")
Q("A4", "我的行李箱挂了40天了。是市场变冷了还是我的问题？", "ambiguous", "服装与鞋靴", conf="review")
Q("A4", "过去一周学习用品的需求趋势。", "clear", "学习用品")
Q("A4", "阻力带市场有季节性吗？", "clear", "运动与户外")
Q("A4", "鼠标需求走势咋样", "longtail", "电子产品")
Q("A4", "我需要趋势方向，而且得有数据，不是你的感觉。", "clear", "",
  note="直接考 prompt 里'trend 必须如实转述后端字段'的要求")
Q("A4", "自从我挂了机械键盘，需求有变化吗？", "clear", "电子产品", seller="single")
Q("A4", "两个滑板都挂了好几周了。咋回事？", "ambiguous", "运动与户外")
Q("A4", "现在这个季节卖自行车是不是不太好？", "clear", "自行车与滑板车")
Q("A4", "为啥啥都卖不出去", "vague", "", quality="bad")
Q("A4", "对比下这套马克笔这个月和上个月的需求。", "clear", "学习用品")
Q("A4", "JBL 音箱9月还有人感兴趣，现在没了。解释下为啥掉了。", "detailed", "电子产品")
Q("A4", "降了？", "terse", "", quality="bad")
Q("A4", "我在这个平台这整个期间，我这些品类的需求涨了吗？", "detailed", "")
Q("A4", "这个平板没动静是因为价格还是根本没人想要平板？", "ambiguous", "电子产品", scn="both", conf="review",
  note="价格 vs 需求二选一的归因问题；标注 both，需人工裁定")
Q("A4", "自行车过去7天趋势", "clear", "自行车与滑板车")
Q("A4", "像我这种东西一般挂多久才卖掉？", "clear", "")
Q("A4", "篮球那个挂单是凉了还是只是慢？", "clear", "运动与户外")
Q("A4", "我所有东西的需求，按最早挂的先来。", "multi_intent", "")
Q("A4", "我啥都没挂，但我想知道这平台是不是要凉了。", "vague", "", seller="none",
  agent="clarify", scn="none", conf="review", second="demand", req=(),
  note="零在售 + 泛化平台质疑；可能 clarify 也可能 sales_agent（场景必须是 none：_sanitize() 对非 sales 路由强制归零，price/demand 这类金标系统永远产不出来）")

# ==================================================================
# A5 · 索取买家名单 (n=28) — sales_agent / find_buyers
# ==================================================================
Q("A5", "Giant 通勤自行车我该找谁聊？", "clear", "自行车与滑板车")
Q("A5", "给我看看在找 Instant Pot 的买家。", "clear", "家居与厨房")
Q("A5", "找想买 switch lite 的买家", "terse", "电子产品")
Q("A5", "我宁愿直接去联系也不想等。把和 Lenovo 平板匹配的实际还开放着的求购给我，按预算最高的人排，这样我能决定先联系谁比较值。", "detailed", "电子产品")
Q("A5", "谁想要我的滑板", "longtail", "运动与户外")
Q("A5", "能帮我找出我篮球挂单的买家吗？", "typo", "运动与户外")
Q("A5", "我该联系谁？", "anaphora", "服装与鞋靴",
  ctx="咱看看那个硬壳万向轮行李箱")
Q("A5", "列出符合JBL音箱的开放求购", "clear", "电子产品")
Q("A5", "买家？", "terse", "", quality="bad")
Q("A5", "今天有想买慢跑裤我能联系的人吗？", "clear", "服装与鞋靴")
Q("A5", "show me which buyers are looking for my keyboard", "non_english", "电子产品", seller="single")
Q("A5", "我要能直接联系的名字，不要统计数据。", "clear", "",
  note="prompt 明确区分 find_potential_buyers(人) vs analyze_demand(量)")
Q("A5", "帮我把我的马克笔套装和当前买家求购匹配一下。", "clear", "学习用品")
Q("A5", "想要阻力带的人里谁离我最近？", "clear", "运动与户外")
Q("A5", "给我看人", "terse", "", quality="bad")
Q("A5", "按最匹配给我找自行车的前3个买家。", "clear", "自行车与滑板车")
Q("A5", "鼠标有啥线索吗", "longtail", "电子产品")
Q("A5", "现在有没有正在找平板、我能通知的买家？", "clear", "电子产品")
Q("A5", "给我两块滑板都找找潜在买家。", "ambiguous", "运动与户外")
Q("A5", "我要行李箱最近的买家，距离比预算重要。", "detailed", "服装与鞋靴")
Q("A5", "我能把这个卖给谁", "anaphora", "家居与厨房",
  ctx="这个Instant Pot Duo已经挂了三周了。")
Q("A5", "一次给我匹配我所有在售商品的买家。", "multi_intent", "")
Q("A5", "线索", "terse", "", quality="bad")
Q("A5", "哪些买家的预算真的能覆盖我389刀的要价？", "detailed", "电子产品")
Q("A5", "学期结束前帮我找个想要60色马克笔套装的人。", "clear", "学习用品")
Q("A5", "我啥也没挂。谁想买自行车？", "clear", "自行车与滑板车", seller="none",
  agent="publishing_agent", scn="none", conf="review", second="find_buyers", req=(),
  note="零在售边界。第二意图(买家名单)未被应答（场景必须是 none：_sanitize() 对非 sales 路由强制归零，price/demand 这类金标系统永远产不出来）")
Q("A5", "给我最新挂的那个东西的买家列表。", "clear", "")
Q("A5", "帮我联系搜过高压锅的人", "longtail", "家居与厨房")

# ==================================================================
# A6 · 价格 + 需求复合提问 (n=20) — sales_agent / both
# ==================================================================
Q("A6", "自行车卖279刀合适吗，还有人想买吗？", "multi_intent", "自行车与滑板车")
Q("A6", "告诉我Instant Pot的需求怎么样，还有63刀站得住脚吗。", "multi_intent", "家居与厨房")
Q("A6", "Switch Lite的价格和热度", "terse", "电子产品")
Q("A6", "关于那个 Lenovo 平板有两件事：第一，这上面到底有多少人在搜平板，第二，478 刀跟那些卖掉的成交价到底差得远不远。我得两个都知道再决定要不要降。", "detailed", "电子产品")
Q("A6", "滑板这个价还行吗有人想要吗", "longtail", "运动与户外")
Q("A6", "我这个行李葙的需秋和合适价咯是多少？", "typo", "服装与鞋靴")
Q("A6", "两个都要谢谢", "anaphora", "电子产品", quality="bad",
  ctx="你是要 JBL 音箱的需求还是定价？")
Q("A6", "跑步裤的需求加同款成交价都给我。", "clear", "服装与鞋靴")
Q("A6", "篮球要不要降价，还有卖 23 刀和 18 刀分别有多少买家？", "detailed", "运动与户外")
Q("A6", "show me both demand and pricing for that laptop tablet", "non_english", "电子产品")
Q("A6", "阻力带的市场大小和市场价。", "clear", "运动与户外")
Q("A6", "我想知道这套马克笔是定价错了还是单纯没人想要。", "ambiguous", "学习用品")
Q("A6", "需求和定价，所有商品，一次总结。", "multi_intent", "")
Q("A6", "鼠标有多少人感兴趣，应该卖多少钱？", "clear", "电子产品")
Q("A6", "键盘定价有竞争力吗，还有人搜这个吗？", "clear", "电子产品", seller="single")
Q("A6", "巡航长板的感兴趣程度加个建议价格。", "clear", "运动与户外")
Q("A6", "自行车：需求、同款成交价，还有你会标多少。", "terse", "自行车与滑板车")
Q("A6", "我这个平板商品你知道的都告诉我", "vague", "电子产品", quality="bad", conf="review",
  note="'everything' 未指明分析类型；标注 both，模型判 none 亦有据")
Q("A6", "是价格导致没人感兴趣，还是这个品类本来就冷清？", "ambiguous", "")
Q("A6", "Switch Lite 给我感兴趣买家的人数还有三个价格档。", "detailed", "电子产品")

# ==================================================================
# A7 · 自有库存盘点 (n=15) — sales_agent / none
# ==================================================================
Q("A7", "我哪些商品还挂着？", "clear", "")
Q("A7", "列一下我的东西", "terse", "")
Q("A7", "我现在在卖什么？", "clear", "")
Q("A7", "我都忘了这个平台上挂了啥。把还在售的全给我看，每个挂了多久、我要多少钱都要，最早的排前面。", "detailed", "")
Q("A7", "我挂了多少东西", "longtail", "")
Q("A7", "我那些东西还有什么在卖阿？", "typo", "")
Q("A7", "给我看最早那个。", "anaphora", "",
  ctx="我哪些商品还挂着？")
Q("A7", "how many items do I still have for sale", "non_english", "")
Q("A7", "库存", "terse", "", quality="bad")
Q("A7", "我那个篮球还挂着吗还是已经卖了？", "clear", "运动与户外")
Q("A7", "哪个商品挂得最久？", "clear", "")
Q("A7", "把我在售的商品给我，别的都不要，别分析。", "clear", "")
Q("A7", "我所有在卖的东西总共值多少钱？", "clear", "")
Q("A7", "我现在有在卖东西吗？", "clear", "", seller="none", conf="review",
  note="零在售；scenario=none 的库存问题，专家应如实说空")
Q("A7", "我的商品咋回事", "vague", "", quality="bad")

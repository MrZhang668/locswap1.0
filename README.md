# LocSwap · 校园二手交易平台

[English](README.en.md) · 在线演示：<https://locswap.cn> · [界面截图与对话示例](screenshots/README.md)

LocSwap 是一个面向校园的二手交易平台，场景设定在约翰霍普金斯大学（JHU）Homewood 校区周边。代码里预置了 18 个公寓和联排作为交易地点；界面是中文，价格以美元计。

它想解决的是校园二手交易里两头的麻烦：
- **买家**：不想一页页翻列表，想直接说「300 以内的自行车，离我近点」。
- **卖家**：不知道该标多少钱、有没有人要、东西该怎么描述。

为此，买家端用的是**规则解析加三路召回的对话式检索**；卖家端是一个基于 **LangGraph 的销售顾问 Agent**。它能查数据、找潜在买家、审核并起草商品文案。所有写操作都要卖家明确确认后才会执行。

---

## 功能

### 买家

- **首页浏览**：
  - 可按关键词、品类（及其下的子类目）、价格区间、成色、距离筛选，有 5 种排序方式，支持分页；
  - **不登录也能浏览**，此时距离从校园中心算起。
- **对话式搜索**：
  - 用自然语言描述需求，系统解析出价格、距离、品类、品牌、成色等条件，再走三路召回、融合排序，见「[检索管线](#检索管线)」；
  - 需求太模糊时会追问一次；
  - 索要他人联系方式的请求直接拒绝，不调用模型。
- **地图找物**：Leaflet 地图，按交易地点打点，点开就能预订。
- **预订**：
  - 第一个预订会把商品锁定为「已预留」，并开始 24 小时确认倒计时；
  - 可以多人排队，购物车里显示自己排第几；
  - 卖家确认成交后，其他排队的人会收到通知；
  - 超时未确认的商品会被自动清理。
- **求购单**：
  - 搜不到时，自动记下一条求购需求；
  - 也可以手动添加，或者点「有货通知我」；
  - 开启「自动通知」后，有新商品上架且名称覆盖了需求里的关键词，就会收到站内通知。
- **站内消息**：成交确认、未成交、求购匹配、出售计划提醒等通知。

### 卖家

- **手动上架**：上传照片（服务端压缩到长边 1024），填写品类、成色、价格、交易地点；子类目可选，不选时归入该品类的「其他…」。这条路径不使用 AI。
- **我的橱窗**：查看预订名单、确认成交，暂时下架或恢复上架，永久删除。
- **销售顾问对话**（需要 LLM）：支持多条对话线程，可以新建、重命名、删除，对话保存在 SQLite 中。
  - **意图路由**：每轮先判断卖家想做什么：分析、发布、改状态、以后再卖，还是需要追问。
  - **认出卖家说的是哪件商品**：卖家点名一件东西时（「我的库里有台灯吗」），先拿说法的中心词和商品上存的中心词比对；比对不出时再请模型判断（调用点 I），模型给出的结果还要过一道子类目校验。所以「台灯」能认出「LED护眼书桌灯」，而「自行车反光灯」不会被认成自行车。模型调用失败时退回按标题分词比对。
  - **数据分析**（只读工具）：
    - 我的商品；
    - 潜在买家：匹配求购单，以匿名卡片展示；
    - 同品类成交价分布；
    - 需求趋势与流量。
  - **两类写操作**：「把商品推送给某位买家」和「暂时下架 / 恢复上架」。
    - 都由 Agent 先提议，卖家回复确认后才执行；
    - 永久删除不能通过对话完成。
  - **发布流程**：
    1. 对照规则库（16 条拦截规则、12 条提醒规则）做合规审核，包括图片；
    2. 生成一份可编辑的商品草稿；
    3. 卖家修改并批准后才上架，批准时服务端会再审核一次。
  - **出售计划**：卖家说「这个下学期再卖」时，记录目标日期，并可以先把商品隐藏起来。
    - 后台每周检查一次该品类的需求：当周预订量很高时，发站内通知和邮件；需求环比突增时，只发邮件；
    - 计划可以在对话里或卡片上暂停、继续、取消。
- **邮件**：所有邮件先写入发件箱表；只有配置了 SMTP（`EMAIL_ENABLED=true`）才会真正发出，默认关闭。

---

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python、FastAPI、SQLAlchemy 2、SQLite、Pydantic Settings |
| 检索 | sentence-transformers（`BAAI/bge-small-zh-v1.5`，512 维）、ChromaDB（可切换 Milvus / Zilliz Cloud，或纯内存）、rank_bm25 + jieba 分词 |
| LLM / Agent | LangGraph、LangChain、Anthropic SDK，或任意 OpenAI 兼容接口（如 OpenRouter） |
| 前端 | React 18、Vite 5、Leaflet（地图，瓦片来自 OpenStreetMap）、three.js（首页地球） |
| 部署 | Docker Compose（应用容器 + nginx 容器）、certbot（HTTPS） |

认证是自实现的：密码用 PBKDF2-SHA256 哈希，令牌用 HMAC 签名（不是 JWT），默认有效期 7 天。

---

## 目录结构

```
.
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── main.py              # FastAPI 入口：建表、重建索引、启动后台任务
│       ├── config.py            # 全部配置项（读取项目根目录的 .env）+ 校园地点、品类、品牌词典
│       ├── models.py            # 数据表定义
│       ├── routers/             # HTTP 接口：auth / items / search / reservations / seller / seller_advisor / wants / notifications / meta
│       ├── repositories/        # 数据访问
│       └── services/
│           ├── retrieval.py     # 检索管线（解析 → 过滤 → 三路召回 → RRF 融合）
│           ├── query_parser.py  # 规则解析：价格、距离、品类、品牌、成色
│           ├── item_index.py    # 商品索引（向量 + BM25 + 结构化），所有写入经过这里
│           ├── vector_store.py  # Chroma / Milvus / 内存 三种向量库实现
│           ├── wants.py         # 求购单、推送、自动通知
│           ├── lifecycle.py     # 商品状态机（在售 / 已预留 / 暂停 / 售出）
│           ├── sale_plans.py    # 出售计划与每周需求监测
│           ├── scheduler.py     # 后台循环：过期清理、出售计划检查
│           ├── llm/             # 模型访问：client（官方 SDK / OpenAI 兼容接口）、langchain_client（Agent 用的模型与 token 计量）
│           ├── nlp/             # 确定性的语言规则：意图词法、日期换算（不调模型）
│           └── agents/          # Agent 层
│               ├── graph/       # LangGraph 图：state（状态）/ nodes（节点）/ routing（拓扑）/ runtime（编译、存档、run_turn）
│               ├── router.py    # 意图路由（调用点 D）；defer_intent.py「以后再卖」识别（H）
│               ├── proposals.py # 两类需确认的写操作；handoff.py 任务挂起与接回；sessions.py 对话线程
│               ├── sales/       # 销售顾问：工具与工具调用循环（C）
│               ├── publishing/  # 发布流程：规则库、合规审核、草稿抽取、批准上架（E、F）
│               └── planning/    # 出售计划 Agent（G）
├── frontend/
│   ├── vite.config.js           # 开发代理：/api、/static → 后端 8010；/tiles → OSM
│   └── src/
│       ├── App.jsx              # 顶部导航与页面切换（未使用路由库）
│       ├── api.js               # 所有后端接口调用
│       ├── pages/               # 首页、找东西、购物车、我要卖、消息、我的、登录、引导
│       └── components/          # 浏览面板、校园地图、首页地球、销售顾问对话等
├── prompts/                     # 各 LLM 调用点的系统提示词（Markdown，修改后热加载）
├── scripts/                     # 数据初始化、模拟、标定、评测与验收脚本
├── deploy/                      # Dockerfile、docker-compose、nginx、部署脚本（见 deploy/README.md）
├── screenshots/                 # 界面截图与销售顾问对话示例（附说明）
└── .env.example                 # 配置模板
```

---

## 快速开始（本地运行）

### 1. 环境要求

- **Python 3.13**：开发环境和 Docker 镜像都用这个版本。
- **Node.js 18+**：Vite 5 的要求。
- 磁盘：torch 加上向量模型，大约需要几个 GB。

### 2. 配置

在项目根目录复制配置模板：

```bash
cp .env.example .env            # Windows PowerShell: Copy-Item .env.example .env
```

**不填任何 LLM 密钥也能运行**：浏览、搜索、预订、求购、手动上架都可用，只有销售顾问对话不可用。要启用它，在 `.env` 里二选一：

```ini
# 方式一：Anthropic 官方
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=你的密钥

# 方式二：任意 OpenAI 兼容接口（如 OpenRouter）
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=你的密钥
LLM_MODEL_MAIN=供应商的模型 ID
LLM_MODEL_LIGHT=供应商的模型 ID
```

模型需要满足两点：
- 支持工具调用（function calling）；
- 主模型支持图片输入，因为发布流程会把商品照片发给模型做审核。

### 3. 安装后端依赖并初始化数据

```bash
# 可选：先装 CPU 版 torch，避免下载体积很大的 CUDA 版本
pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2"

pip install -r backend/requirements.txt

# 生成演示数据：1 个买家 + 120 个卖家 + 560 件商品（会清空现有数据）
python scripts/seed_mock.py

# 可选：在上面的数据上模拟 12 周运营（2026-06 至 2026-09 的成交、求购、搜索记录）。
# 销售顾问的「成交价分析」「需求分析」和出售计划监测都依赖这些历史数据。
python scripts/simulate_platform.py --apply
```

关于初始化的几点说明：
- 第一次运行会从 Hugging Face 下载向量模型 `BAAI/bge-small-zh-v1.5`。如果访问不了，可以设置环境变量 `HF_ENDPOINT` 指向镜像站。
- 如果模型加载失败，在 `EMBEDDING_BACKEND=auto` 下会静默退回哈希向量。程序照样能跑，但语义检索质量会明显变差。
- 想要更小的数据集，可以改用 `python scripts/seed.py`：4 个账号、25 件商品。

### 4. 启动

```bash
# 终端 1：后端（端口 8010）
cd backend
python -m uvicorn app.main:app --reload --port 8010

# 终端 2：前端（端口 5173）
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。开发模式下，接口文档在 <http://127.0.0.1:8010/docs>。

后端**每次启动都会从 SQLite 全量重建**商品、求购、规则三个索引，商品多时需要等一会儿，日志出现 `index ready` 就说明可以用了。

### 5. 演示账号

所有账号的密码都是 `demo1234`。

| 数据集 | 买家 | 卖家 |
|---|---|---|
| `seed_mock.py` | `buyer@campus.edu` | `seller001@campus.edu` ~ `seller120@campus.edu` |
| `seed.py` | `buyer@campus.edu` | `seller1@campus.edu` ~ `seller3@campus.edu` |

也可以在登录页直接注册新账号。

---

## 主要配置

完整列表和说明见 [`.env.example`](.env.example) 与 [`backend/app/config.py`](backend/app/config.py)。常用的几项：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` 或 `openai_compat` |
| `LLM_MODEL_MAIN` / `LLM_MODEL_LIGHT` | `claude-sonnet-4-6` / `claude-haiku-4-5` | 主模型用于顾问、发布、计划；轻模型用于查询理解、路由 |
| `EMBEDDING_BACKEND` | `auto` | `auto`、`sentence_transformers` 或 `hashing` |
| `VECTOR_BACKEND` | `chroma` | `chroma`、`milvus` 或 `memory` |
| `CHROMA_DIR` | `data/chroma` | Chroma 持久化目录 |
| `MILVUS_URI` / `MILVUS_TOKEN` / `MILVUS_COLLECTION_PREFIX` | 空 | 使用 Milvus / Zilliz 时填写 |
| `VECTOR_MIN_SIMILARITY` / `BM25_MIN_SCORE` | `0.50` / `5.0` | 向量路和 BM25 路的召回下限（中文语料标定值） |
| `CONFIRM_WINDOW_HOURS` | `24` | 预订后卖家确认成交的时限 |
| `EMAIL_ENABLED` / `SMTP_*` | 关闭 | 真正发出邮件 |
| `ENVIRONMENT` | `development` | 设为 `production` 时会做启动检查，配置不安全就拒绝启动 |

`ENVIRONMENT=production` 下，以下情况（不限于这些）会拒绝启动：
- `AUTH_SECRET` 是默认值，或短于 32 位；
- `CORS_ORIGINS` 为空或含有 `*`；
- `EMBEDDING_BACKEND` 是 `auto`；
- 使用 Milvus 但没有设置集合前缀。

---

## 检索管线

入口在 `backend/app/services/retrieval.py`。

1. **规则解析**：用正则和词典提取价格（中英文、中文数字都支持）、距离（「附近」默认 1.5 km）、品类、品牌（含中文别名）、颜色、成色，剩下的部分作为语义文本。
2. **LLM 查询理解**（可选）：只有在规则什么都没解析出来、或者需要追问时才调用轻模型；模型给出的字段不会覆盖规则的结果。没有密钥时直接跳过。
3. **硬过滤**：只保留在售或已预留的商品，再按价格、距离过滤（只有用户设置了位置才按距离过滤），并排除自己发布的商品。品类故意**不**作为硬过滤条件。
4. **三路召回**，每路最多 20 条：
   - **向量**：bge-small-zh 余弦相似度，必须同时达到 0.50 的绝对下限，且与第一名的差距在 0.15 以内；
   - **BM25**：基于 jieba 分词，必须同时达到 5.0 的绝对下限，且不低于第一名得分的 30%；
   - **结构化**：品类、品牌、关键词的字段匹配，按中心词、修饰词、子串分档。
5. **融合**：用 RRF（k=60）合并，并对「查询词只是修饰词」的结果降权，最后取前 8 条。置信度低于 0.25 时追问或提示「结果不太确定」。
6. **回复文本**：由模板生成，不调用 LLM。

阈值的标定脚本是 `scripts/calibrate_retrieval.py`。换语料或换向量模型后需要重新标定。

---

## LLM 调用点

| 调用点 | 用途 | 模型 | 没有密钥时 |
|---|---|---|---|
| B | 查询理解与追问 | 轻 | 跳过，检索照常 |
| C | 销售顾问（工具调用循环） | 主 | 返回「暂不可用」 |
| D | 意图路由，以及分析后的行动提议 | 路由模型（默认用轻模型） | 整个顾问对话降级 |
| E | 发布合规审核（含图片） | 主 | 报错，提示重试 |
| F | 商品草稿抽取 | 主 | 报错 |
| G | 出售计划 Agent | 主 | 降级回复 |
| H | 「以后再卖」意图识别 | 路由模型 | 退回词法规则 |
| I | 卖家点名的商品是在售的哪一件 | 路由模型 | 退回按标题分词比对 |

每个调用点的系统提示词都在 `prompts/` 目录下。每次调用消耗的 token 记录在 `token_usage_log` 表里，可以用 `python scripts/token_report.py` 汇总。

---

## 隐私与安全设计

- 商品卡片按字段白名单生成。卖家邮箱只出现在买家自己的购物车里，也就是已经预订之后才可见。
- 写入检索索引前会过滤掉像邮箱的字符串；索要他人联系方式的搜索直接拒绝。
- 销售顾问看到的潜在买家是匿名卡片（「候选 N」）。推送有频率限制：每位买家每天、每个卖家每天都有上限，同一需求也会去重。
- 对外只开放 `data/uploads/` 一个静态目录；上传接口返回的照片路径会被严格校验。
- 匿名用户的搜索结果不包含检索调试信息。

---

## 部署

`deploy/` 下是一套基于 Docker Compose 的部署方案：
- 应用容器：FastAPI，向量模型在构建时打进镜像；
- nginx 容器：负责静态资源、按成本分档的限流、地图瓦片缓存，以及 HTTPS 和证书自动续期；
- 部署命令：`deploy/push.sh` 一条命令推送到服务器。

步骤、服务器选择和常见问题见 [deploy/README.md](deploy/README.md)。

---

## 脚本

| 脚本 | 用途 |
|---|---|
| `scripts/seed_mock.py` | 生成主数据集，并重建索引 |
| `scripts/seed.py` | 生成小数据集（25 件商品） |
| `scripts/simulate_platform.py` | 模拟 12 周运营历史（默认只预览，`--apply` 才写入） |
| `scripts/calibrate_retrieval.py`、`scripts/eval_retrieval.py` | 检索阈值标定与评测 |
| `scripts/check_production_config.py` | 部署前检查生产配置 |
| `scripts/backfill_subcategory.py` | 给存量商品补子类目和中心词（默认只调模型写报告，`--apply` 按审过的报告写库，之后要重启后端重建索引） |
| `scripts/smoke_llm.py` | 用真实调用测试 LLM 连通性 |
| `scripts/token_report.py` | token 用量汇总 |
| `scripts/verify_*.py` | 各功能的验收脚本，部分需要后端在运行或需要 LLM 密钥 |
| `scripts/gen_item_images.py`、`scripts/gen_item_photos.py` | 用图像模型生成商品图（需要 OpenRouter 密钥，按张计费） |

项目没有接入 pytest 等单元测试框架。功能验证用的是上表里的 `verify_*.py` 脚本。

---

## 已知限制

- **需求数据只来自模拟**：「需求分析」和出售计划监测读的是 `demand_event` 表，而这张表目前只由 `simulate_platform.py` 写入，真实的搜索和预订不会写进去。
- **没有 LLM 密钥时，整个销售顾问对话都不可用**，包括出售计划的记录。
- **后端只能单 worker 运行**：推送给买家时用到的「候选卡片 → 求购单」映射存在进程内存里，有效期 30 分钟，重启后会丢失。
- **每次启动都全量重新向量化**全部商品。
- **子类目目前只用在两处**：首页浏览的筛选和卖家查自家商品。对话式搜索、求购匹配、需求分析都还不看子类目；子类目和中心词也不进检索向量。在这个功能之前上架的商品要先跑 `backfill_subcategory.py` 才有子类目，否则按子类目筛不出来。
- **前端没有 URL 路由**，刷新页面会回到首页。另外，后端允许匿名搜索，但前端的「找东西」页要求先登录。
- **认证是演示级别**：没有找回密码、验证码，也没有刷新令牌。
- **演示数据**里部分颜色字段是英文；模拟历史的时间固定在 2026 年 6 到 9 月。

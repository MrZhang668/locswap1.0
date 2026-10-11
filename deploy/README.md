# LocSwap 上线手册（阿里云海外节点，中国大陆可访问）

适用于把 LocSwap 部署到一台阿里云 ECS 的人——大概率是你自己，第一次做一遍，半年后再翻出来看一遍。

## 为什么是东京 / 新加坡，不是香港，也不是大陆

| 区域 | ICP 备案 | 大陆访问 | 模型调用 (`openai/gpt-5.6-terra` via OpenRouter) |
|---|---|---|---|
| 大陆（杭州/上海…） | **必须**，1–3 周 | 最快 | OpenRouter / HuggingFace 均不可直连，三个外部依赖都得换、提示词全部重验 |
| 香港 | 不需要 | 很快 | **会被拒**：OpenAI 不服务香港，请求经 OpenRouter 转发也会返回 `unsupported_country_region_territory`（有人恰好用这个模型从香港服务器复现过） |
| **东京 / 新加坡** | 不需要 | 可以，比香港多几十毫秒 | 正常 |

所以选 **日本（东京）**，其次 **新加坡**。2026-09-15 的方案写的是香港，那时没考虑到模型这一层的地区限制。
`deploy/preflight.sh` 会在构建前用一次真实的模型调用验证这件事——不管选哪个区，**买完先跑它**。

## 第一步：买服务器（在阿里云控制台操作）

- **地域**：日本（东京）或新加坡。建议先用**按量付费**，preflight 通过后再转包年包月；不通过就直接释放，损失几毛钱。
- **规格**：**2 vCPU / 2 GiB 就够**（如经济型 `ecs.e-c1m1.large`）。2026-10-07 实测应用峰值约 740 MiB（含启动时重建 754 件商品的索引），
  把容器内存卡在 1.2 GiB 下完整验收也全部通过。之前写的"4 GiB 起步"是没实测的保守估计。
  构建镜像时占用比运行时高，`server-setup.sh` 会自动加 4 GB swap 兜住，代价是首次构建慢一些。商品量涨到几千件以上再考虑 4 GiB。
- **镜像**：**Ubuntu 24.04**（22.04 也行）。Alibaba Cloud Linux 不被 Docker 安装脚本支持。
- **系统盘**：40 GiB ESSD（镜像本身约 3 GB，构建过程需要几倍空间）。
- **公网 IP**：分配；带宽按流量计费，峰值 5–10 Mbps 足够。
- **安全组入方向**：放行 TCP 22（最好只放你自己的 IP）、80、443。
- **登录方式**：选"密钥对"，或者先用密码，下一步再把公钥加进去。

## 第二步：配好免密 SSH（在你的电脑上，Git Bash）

```bash
ssh-keygen -t ed25519              # 已有 ~/.ssh/id_ed25519 就跳过
ssh-copy-id root@<服务器IP>         # 输一次密码；或把 ~/.ssh/id_ed25519.pub 贴进控制台的密钥对
ssh root@<服务器IP> echo ok         # 不再要密码就对了
```

## 第三步：填 `.env.production`

把 `deploy/env.production.example` 复制为项目根目录的 `.env.production`，填上 `AUTH_SECRET` 和 `LLM_API_KEY`。
`CORS_ORIGINS` 填站点自己的域名（本站是 `https://locswap.cn,https://www.locswap.cn`）；换域名时要同步改 `nginx.conf`。
它不会进镜像，部署脚本会单独以 600 权限传到服务器。改完可以本地预检：

```bash
python scripts/check_production_config.py --env-file .env.production
```

这个预检会拦住**拼错的变量名**——`Settings` 对未知变量是静默忽略的。之前这个文件里就写着
`LLM_MODEL=`（正确是 `LLM_MODEL_MAIN=`），上线后每一次模型调用都会去请求默认的 Anthropic 模型 id 然后失败。

## 第四步：一条命令部署

```bash
bash deploy/push.sh root@<服务器IP> --seed
```

它依次做：本地预检配置 → 打包代码（不含评测语料、本地数据、密钥）→ 上传 → 在服务器上装 Docker、加 4 GB swap、
限制容器日志大小 → **preflight（模型调用 + 各依赖连通性 + 内存/磁盘）** → 构建并启动 → `--seed` 时把本地数据库和商品图传上去 → 等健康检查通过。

- 首次构建 10–20 分钟（装 torch、下载向量模型烤进镜像）；首次启动再花几分钟重建向量索引，所以健康检查的宽限期是 240 秒。
- `--seed` **会替换服务器上的数据库**（旧库保留为 `locswap.db.before-seed-<时间>`）。只在第一次、或者你确实想用本地数据覆盖线上时加。
- 以后**更新代码**就是去掉 `--seed` 再跑一次。

成功时最后一行是 `[ok] deployed: <CORS_ORIGINS 的第一个域名>/`。日志里应能看到 `index ready:`，且商品数和本地一致；
如果出现 `hashing`，说明烤进去的模型没加载上——搜索还能答，但质量会崩，这种情况必须排查而不是放着。

## 第五步：验收

```bash
LOCSWAP_BASE=https://locswap.cn python scripts/verify_anonymous_browsing.py
```

两个方向都验：访客能浏览；访客**不能**预定、上架、看别人的购物车，且 `/static/locswap.db` 拖不走数据库。
后一半是每次部署后都值得跑的——鉴权改动的失败模式从来不是"少了个功能"，而是"某个写操作不再需要登录"。

然后用手机关掉 Wi-Fi（走大陆运营商网络）打开 `http://<服务器IP>/`，确认首页、地图、搜索都正常。

## 地图在大陆为什么能显示

前端原来直接从 `tile.openstreetmap.org` 拉地图瓦片，那个域名在大陆经常加载不出来。
现在瓦片走本站的 `/tiles/`，由 nginx 代为请求 OSM 并缓存 30 天（`locswap-locations.inc`）。
地图锁定在一个校区，几千张瓦片就覆盖了所有人能看到的范围，所以第一天之后几乎全是缓存命中——这也是 OSM 瓦片使用政策对代理的要求。
只放行 12–19 级（与 `CampusMap.jsx` 的缩放范围一致），并单独限流，防止被拿去当免费的全球瓦片服务器。

## 域名与 HTTPS（2026-10-10 已启用）

线上地址是 **https://locswap.cn**。`http://`、`www.locswap.cn`、直接用 IP 访问，全部 301 跳转到这一个地址，
所以登录 token 不会明文传输，`CORS_ORIGINS` 也只需要写这一个源。

- 域名在阿里云万网注册，`@` 和 `www` 两条 A 记录指向服务器。服务器在东京，**.cn 域名也不需要 ICP 备案**。
- 证书来自 Let's Encrypt，覆盖 `locswap.cn` 和 `www.locswap.cn` 两个域名，有效期 90 天。
  服务器上的 `/etc/cron.d/locswap-certbot` 每天凌晨 3:17 检查一次，**只在剩余不到 30 天时才续期**，续期后会让 nginx 重新加载证书。
  2026-10-10 用 `--dry-run` 把整条续期流程演练过一遍，成功。
- HTTP 的 server 块只保留 `/.well-known/acme-challenge/` 不跳转，**这个路径不能删**，续期就靠它。

**换服务器或从头配时，顺序不能反**：nginx 只要配置里引用的证书文件不存在就拒绝启动。所以要先用"只有 HTTP"的旧配置
（`_backups/pre_https_*.zip` 里有）部署起来，然后在服务器上运行：

```bash
bash /opt/locswap/deploy/enable-https.sh locswap.cn www.locswap.cn
```

它会依次确认域名已经指向本机、确认验证路径能通，然后申请证书、装上续期任务。完成后再推送当前这份 HTTPS 配置。

**nginx 配置是单文件挂载进容器的**，部署时文件被替换后，正在运行的容器还挂着旧文件。所以 `push.sh` 会先在一个临时容器里
`nginx -t` 测试新配置，**通过才重启 web**；不通过就让旧配置继续运行，并把报错打印出来。第一次切 HTTPS 时就是因为没重启，
部署"成功"了但 443 端口没有生效。

## 数据与备份

所有有状态的东西都在 `locswap_locswap-data` 这个卷里：SQLite 数据库、卖家顾问的对话存储、所有上传的照片。
它不在镜像里，这里也没有任何东西自动备份它。

```bash
# 在服务器上备份
docker run --rm -v locswap_locswap-data:/data -v /root:/out alpine \
  tar czf /out/locswap-$(date +%F).tar.gz -C /data --exclude='./chroma_zh' .
# 拉回本地
scp root@<IP>:/root/locswap-*.tar.gz .
```

向量索引（`chroma_zh/`）不用备份：每次启动都从 SQLite 重建。恢复数据库后重启 app 即可。

## 会咬人的几件事

**只能单 worker，这不是调优选项。** 卖家顾问有两样东西放在进程内存里：候选卡片到买家需求的映射（`services/agents/sales/tools.py`），
以及 LangGraph 等待"是/否"确认时挂起的线程状态。第二个 worker 两样都拿不到，卖家会随机看到"候选列表已过期"。
要横向扩容，得先把这两样搬进数据库，不是改个配置。

**模型按次计费。** 所有 LLM 调用点都打 `LLM_BASE_URL`（本站是 OpenRouter）。nginx 按成本分了三档限流（每 IP）：浏览类（含对话线程列表）240 次/分、
`/api/search/`、`/api/seller/advisor/chat` 与 `/api/seller/advisor/publish/approve` 20 次/分、`/api/auth/` 10 次/分。这是访客和你的余额之间唯一的东西；留意 `token_usage_log` 表。

**`data/` 只有 `/static/uploads/` 对外。** 以前 `/static` 挂的是整个数据目录，`GET /static/locswap.db` 能直接下载全库。
已修，`verify_anonymous_browsing.py` 会在它回来时失败——别"顺手简化"掉那一行。

**向量库用的是本地 Chroma，不是 Zilliz。** 两者召回实测完全一致，Chroma 6.6 ms 对 Zilliz 101 ms，后者还要跨太平洋。
`.env.production` 里的 `MILVUS_*` 只是备用，`VECTOR_BACKEND=chroma` 时不生效。

## 常用运维命令（在服务器 `/opt/locswap` 下）

```bash
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f app
docker compose -f deploy/docker-compose.yml restart app
curl -sI https://locswap.cn/tiles/15/9409/12484.png | grep X-Tile-Cache   # MISS 一次后应为 HIT
```

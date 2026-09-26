# 部署：腾讯云 + Vercel

这一份是**上线的操作手册**，不是架构文档。它只回答"要做哪几步、每步做错了会怎样"。

> ⚠️ **本文里的服务器命令没有在真实服务器上跑过。** 本机没有 Docker、没有
> systemd、没有 Nginx、没有 PostgreSQL，所以没有一条能被验证。可以验证的那部分
> （启动校验、迁移状态检查、CORS 行为、探针语义）在最后「已经验过的部分」里逐条列出。
> 上服务器时请把每一步的输出看一眼，别整段粘贴。

## 两台东西

```
浏览器 ──https──> Vercel（Next.js 前端，apps/web）
   │
   └──https──> 腾讯云 CVM（FastAPI + Uvicorn，Nginx 在前面做 TLS）
                     │
                     └──> 腾讯云 PostgreSQL
```

前端的地址是 Vercel 给的域名，后端的地址是你自己域名下的一个子域（比如
`api.example.com`）。**两个都必须是 https**，理由见下面的第 2 条。

## 上线前必须改的四件事

| 值 | 现在（开发） | 上线时 | 不改会怎样 |
|---|---|---|---|
| `APP_ENV` | `development` | `production` | `/docs` 会一直开着；且 `DATABASE_URL` 留空时会**悄悄用仓库里的 SQLite 文件**，而那个文件在容器/服务器重建后就没了 |
| `APP_SECRET_KEY` | `change-me` | 随机 32 字节以上 | 所有签名都基于它。`change-me` 是公开的 |
| `DATABASE_URL` | 空（SQLite） | `postgresql+asyncpg://...` | 见上。`APP_ENV=production` 且它为空时启动会直接报错，这是有意的 |
| `CORS_ORIGINS` | `http://127.0.0.1:5173,...` | `https://<你的 Vercel 域名>` | 浏览器会把每个请求挡在 CORS 预检上。**表现是"点登录没反应"**，而不是一个显眼的错误 |

另外两个与前端有关：

- `NEXT_PUBLIC_API_BASE_URL` = `https://api.example.com`。**它是在构建时烤进
  JS 里的**，所以要在 Vercel 上先设好这个环境变量、再触发部署；改完之后必须重新
  部署一次，只在后台改值不会生效。
- `LLM_API_KEY` 只配在后端。前端不需要、也拿不到它。

## 一、腾讯云服务器

以 Ubuntu 22.04、部署在 `/srv/zhitu`、跑在 `zhitu` 用户下为例。

### 1. 系统包与 Python

```bash
sudo apt update
sudo apt install -y python3.13-venv python3.13-dev build-essential nginx postgresql-client
```

Python 版本**不要低于 3.12**：代码用了 `X | Y` 类型语法、`datetime.UTC` 等 3.11+
特性。本机开发用的是 3.13。

### 2. 代码与虚拟环境

```bash
sudo mkdir -p /srv/zhitu && sudo chown zhitu:zhitu /srv/zhitu
sudo -u zhitu git clone <仓库地址> /srv/zhitu
cd /srv/zhitu
sudo -u zhitu python3.13 -m venv .venv
sudo -u zhitu .venv/bin/pip install -r backend/requirements.txt
```

`backend/requirements-agent.txt`（openjiuwen）**要不要装，是一个真实的取舍**：

- 装：模型调用走 openJiuwen 编排，界面上的徽标显示「AI 规划 · openjiuwen」。
  代价是 86 个依赖、数百 MB，且它会把 `sqlalchemy` 一起带进来——与
  `requirements.txt` 的约束（`>=2.0,<2.1`）有一致性要求，装完请跑一次
  `.venv/bin/python -c "import sqlalchemy; print(sqlalchemy.__version__)"` 确认。
- 不装：自动降级为直连 DeepSeek，**响应里会如实标注**（`source: direct_llm`），
  界面上显示「直连模型」。用户看到的是实话，不是假装。

装或不装都不会让服务起不来，这一条是设计好的。

### 3. `.env`

```bash
sudo -u zhitu cp .env.example /srv/zhitu/.env
sudo chmod 600 /srv/zhitu/.env
sudo -u zhitu nano /srv/zhitu/.env     # 按上面那张表改
```

### 4. 数据库：先建库，再跑迁移

```bash
# 迁移必须在**服务启动之前**跑完。
.venv/bin/python -m alembic -c backend/alembic.ini upgrade head
.venv/bin/python -m alembic -c backend/alembic.ini current   # 应打印 (head)
```

顺序反了会怎样：服务**起不来**，日志里写着"数据库的迁移版本是 X，而这份代码要的是 Y"。
这是有意的（`backend/db/health.py`）。以前它起得来，然后在第一个碰到新列的请求上
炸成一个和真实原因毫不相干的 500。

`alembic_version` 表不在、或者和 head 不一致时都会拒绝启动。要临时绕过（**不要用在生产**）：
`ALLOW_DEGRADED_DB=1`。

### 5. 服务与反向代理

```bash
sudo cp scripts/deploy/zhitu-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zhitu-api
curl -s localhost:8000/ready        # {"ready":true,...}
```

Nginx 与证书（`scripts/deploy/nginx-zhitu.conf` 里逐条写了为什么）：

```bash
sudo cp scripts/deploy/nginx-zhitu.conf /etc/nginx/sites-available/zhitu-api
sudo ln -s /etc/nginx/sites-available/zhitu-api /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d api.example.com
```

那个 conf 里有一处**不能省**：`proxy_read_timeout 120s`。一次规划请求实测要十几
到几十秒，而 Nginx 默认 60 秒就断开——用户看到 504，但**后端那一次调用还在跑**，
他会以为没成功、再点一次。

## 二、Vercel

1. New Project → 选这个仓库 → **Root Directory 填 `apps/web`**。
2. Environment Variables 加 `NEXT_PUBLIC_API_BASE_URL = https://api.example.com`。
   （要不要加 `APP_ENV`？不用，那是后端的事。）
3. Deploy。拿到域名后，**回到服务器把 `CORS_ORIGINS` 改成这个域名并重启服务**——
   这是最容易漏的一步，漏了的表现是前端所有请求都被浏览器挡掉。

Vercel 部署的是 `next build` 的产物；本地开发用的是 `next dev`，两者偶尔会不一致
（构建期的检查比 dev 严）。所以上线前在本地跑一次 `npm run build` 能提前发现问题 ——
**这一条已经跑过了**：构建完成并打印出全量路由表（12 条路由），产出
`.next/BUILD_ID`，且正在运行的 `next dev` 在构建之后仍然正常（`/workbench` 200）。
它验证的是"这份代码能构建"，**不**代表 Vercel 上的环境变量、域名与构建缓存都没问题。

## 三、上线之后验这五条

按顺序做，任何一条不过就先别继续：

1. `curl https://api.example.com/ready` → `{"ready":true,"env":"production",...}`。
   `env` 不是 `production` 说明 `APP_ENV` 没生效。
2. 在 Vercel 的域名上注册一个账号、登录。**这一条同时验了 CORS 和 TLS** ——
   它是唯一能证明前端真的够得着后端的一步。
3. 建一个空空间，说一句"我想在三个月内完成一个 Python 项目"。
   回复的徽标应该显示「AI 规划 · openjiuwen」（装了 agent 依赖）或「直连模型」。
   如果显示「本地规则 · 模型不可用」，说明 `LLM_API_KEY` 没读到或额度有问题。
4. 让 AI 提出计划 → 点确认 → 刷新页面，计划还在。
5. `sudo systemctl restart zhitu-api`，再刷新一次，计划**还在**。
   这一步验的是数据真的在 PostgreSQL 里，而不是某个进程的内存里。

重启期间正好在刷新页面的用户会看到一句"暂时连不上后端，你的登录状态还在"和一个
重试按钮（`apps/web/tests/session-restore.spec.ts` 钉住了这个行为）。**他们不需要
重新登录** —— 令牌没有被删掉这件事是这一版专门修的，见下一节的最后一条。

## 四、已经验过的部分（本机）

这些不是"应该没问题"，是跑过的：

- 库连得上但结构落后时**拒绝启动**，并且日志里说清了两边的版本号。
  `backend/tests/test_health.py` 两条断言（把 `alembic_version` 删掉、把它改成假版本）。
- `APP_ENV != development` 且 `DATABASE_URL` 为空时，`Settings.resolved_database_url`
  直接抛错，而不是回落到 SQLite（`backend/core/config.py`）。
- CORS 只认 `CORS_ORIGINS` 里列出的来源，且 `allow_credentials=False`
  （这个 API 完全不用 cookie，凭据一律走 `Authorization` 头）。
- `/health` 不查库、`/ready` 查库并在不可用时 503。
- 后端全套测试：**348 条，347 通过 / 1 失败**（`PYTHONUTF8=1 python -m pytest backend/tests`，
  conda 环境 `zhitu`，2026-09-26 22:38 本机；提交 `1732227` 加当时的工作区改动）。
  `backend/` 与 `shared/` 这一轮**一行未改**（`git status --porcelain -- backend shared`
  是空的），所以这一条红不是改出来的：

  - 红的是 `test_reminders.py::test_user_returned_after_a_gap`。跑它的这一刻落在
    **免打扰时段**里 —— `backend/services/reminder_service.py` 的
    `DEFAULT_QUIET_FROM/TO` 是 22:00–08:00，而 `user_returned` 不在 `_STATE_KINDS`
    里，会被压住，接口的 `note` 也明说了「现在是免打扰时段(22:00–08:00)，有 2 条提醒
    先不打扰你」。**它按钟点红**：本机 22:38 跑必红，08:00 之后跑必绿。
  - 这不是"重跑一次就过"的那种偶发：它是**确定的**，只是判据挂在钟点上。测试自己
    拿 `utcnow()` 造"五天前的消息"，而被测的接口用的是 `now_in(user.timezone)` ——
    两边没有同一个时间锚。要修就得让这条测试能控制"现在是几点"，不是放宽断言。
  - **上一条"348 passed / 0 failed"是历史记录**（提交 `bb6a819`，2026-09-26 白天跑
    的，那时不在这个时段里），不代表现在这个钟点跑也是全绿。
- 另外：**不需要任何模型 key** —— 假模型是依赖注入的注入点，conftest 里三个
  autouse guard 保证没有一条测试悄悄走了真实分支。
- 前端 `npm run typecheck` / `npm run lint` / `npm run contracts:check` 全绿。
- **Playwright 共 32 条（14 个文件）**，跑法分两种，**只有第二种是验收**：

  | | 本地开发检查 | 正式验收 |
  | --- | --- | --- |
  | 命令 | `npm run test:e2e` | `npm run test:accept` |
  | 应用 | 复用已在跑的 `next dev`（5173） | production 构建 + 独立 `next start`（5273） |
  | 后端 | 复用开发后端（8000） | 隔离测试后端（8100）+ 临时 SQLite + 无模型 key |
  | 并发 | `PLAYWRIGHT_WORKERS`（默认 1） | `--workers=1`（可显式覆盖） |

  `npm run test:accept`（`scripts/dev/accept-e2e.mjs`）**不接受复用 5173 上的开发服务器**：
  它自己 production 构建、在显式端口上起 `next start`、起一个只连临时库的测试后端，
  跑完拆掉，碰不到用户的真实计划。`CI=1` 是必须的 —— 它让 `reuseExistingServer` 变成
  false。开发模式复用 `next dev` 只适合快速自查，**不能拿来当验收结论**：同一个交互在
  dev 上过、在 production 上不过，正是要查的那类问题。

- **"30 条全绿"是历史记录，不是任何一版现在的保证。** 那是套件涨到 32 条之前、示例空间
  还是默认入口时，在 `next dev` 上用多 worker 跑出来的结果（提交 `0719b90` 前后），
  写在这里只作存档。

  它之后有一段**量出来的**、必须留着的记录：同一提交、同一配置下串行连跑五轮，失败数
  在 4~6 之间摆动 —— `bb6a819`，生产模式、隔离库、`workers=1`：6 / 4 / 6 / 6 / 4 失败。
  每轮必红的三条是 `leaf-path:21`、`leaf-path:40`、`experience:12`；另外三条
  `dark-theme:347`、`node-delete:44`、`workbench:12` 时红时绿。六条原因**全部落在测试侧
  或测量侧，没有一条是功能回归**，也没有一条是页面加载超时或接口失败。逐条现场保存在
  `apps/web/artifacts/` 的 `round-1/`、`round-2/`、`round-3/`、`round-5/`。

  **这一段同时留下两个失败的教训，别把它们一起归档掉：**

  1. **"串行就一定稳"是错的。** 串行只是基线口径，不等于可重复。
  2. **第 4 轮的现场没了。** 那五轮里第 4 轮的失败产物被后来按用例隔离复现的那几次运行
     覆盖掉了（Playwright 的 `outputDir` 是固定的）。所以那一条的截图与 trace **取不回来**，
     只剩这一行的数字。要留现场就不能让每一次运行写同一个目录。

  那六条后来在**删除示例空间**那一步连测试一起改写（示例数据没了，断言的旧行为也不存在
  了），不再逐条留在套件里。改写后的结果是下面这一条。
- **当前基线（2026-09-26 22:30 前后实测）：`npm run test:accept`，生产构建 + 独立
  `next start`（5273）+ 隔离测试后端（8100，临时 SQLite，无模型 key），`--workers=1`，
  提交 `1732227` 加当时的工作区改动 —— 连跑三轮 31 passed / 1 skipped / 0 failed，再加
  一次**全新库**的完整运行同样是 31 passed / 1 skipped / 0 failed（每轮 1.1~1.4 分钟）。**

  那 1 条 skipped 是 `experience.spec.ts` 里一个写明原因的 `test.fixme`（随笔只写内存、
  后端还没有随笔表，见下）。**它不是通过，也不该被读成通过。**

  这一版让基线从"4~6 条摆动"收敛到 0，靠的是**改测试而不是放宽等待**：三条时序类改成
  "等效果出现"，一条断言过期改成按新布局写，一条测量对象失效改成**先等计划到达再量**。
  没有动 `expect.timeout` 的默认值，没有开自动重试。

  两条特别值得记下来的，因为它们的失败信息都指向了错误的方向：

  | 用例 | 报出来的样子 | 真正的原因 |
  | --- | --- | --- |
  | `timeline.spec`（有截止日的任务那条） | "计划没有从后端到达" | 它和另外十件挤在同一天，只有四张卡放得下，**谁放得下由读回来的节点顺序决定** —— 连跑三次画出来的四张每次都不一样。计划早就到了，没画出来的那张在「另有 N 项」里，那是这一页**正确**的行为。修法：让它独占一天 |
  | `dark-theme.spec`（时间线表面那条） | "找不到任何画了背景的祖先" | 计划到达时整页会**重新挂载一次**，量到的元素已经脱离文档，取 `getComputedStyle` 返回一串空字符串。这是**测量对象被换掉**，不是配色出错。修法：先等卡片画出来再量 |

- **重启服务不会把在线用户登出**。这一条是修出来的：恢复登录状态的代码原来不分
  失败类型，一次被中断的 `GET /api/users/me` 就会删掉浏览器里的令牌 —— 正好在
  重启那一刻刷新页面的用户会"莫名其妙退出登录"，而且刷新也回不来。
  现在只有 **401**（服务端明确拒绝这张令牌）才清令牌；够不着后端时令牌留着，
  页面显示"暂时连不上后端，你的登录状态还在"和一个**重试**按钮。
  `apps/web/tests/session-restore.spec.ts` 两条分别钉住这两种情况。

## 五、这一版明确不做的

写在这里，免得以后有人以为"没提就是还没有"：

- **不用 Docker。** 本机没有 Docker，写了也验不了；systemd + venv 这条路上每一步
  都能在服务器上单独执行、单独看输出。
- **不用 Redis。** 登录限流与熔断是进程内实现，**每个 worker 各一份**。所以本文件
  建议先跑 `--workers 1`；加 worker 时请接受限流窗口变成 N 倍这个事实。真的需要
  跨 worker 共享状态时再引，不要提前接一个连不上的基础设施。
- **没有 CI。** 迁移与全套测试目前靠人跑。
- **没有横向扩展方案。** 这个 MVP 的规模假设是单台机器 + 一个 worker。

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
- 后端全套测试：**350 条全部通过**（`PYTHONUTF8=1 python -m pytest backend/tests`，
  conda 环境 `zhitu`，2026-09-26 **22:53** 本机）。这个钟点**落在免打扰时段（22:00–08:00）
  里** —— 正是上一版必红的那个钟点。被测代码 = 提交 `858c653` —— 跑的时候那份代码
  **还没提交**，工作区是脏的，跑完原样提交，之后 `git status --porcelain` 为空
  （只有这两个文档里的验收记录是随后补的）。

  这是"上一条红**被修好了**"，不是"重跑一次它就绿了"。过程留在这里：

  - 上一版（提交 `1d6e30f`，同日 22:38 跑）是 **348 条 / 347 通过 / 1 失败**，红的是
    `test_reminders.py::test_user_returned_after_a_gap`。那**不是改出来的**：`backend/`
    与 `shared/` 当时一行未改（`git status --porcelain -- backend shared` 是空的）。
  - 它**按钟点红**，不是偶发：`reminder_service` 的 `DEFAULT_QUIET_FROM/TO` 是 22:00–08:00，
    `user_returned` 不在 `_STATE_KINDS` 里，会被压住 —— 本机 22:38 跑必红，08:00 之后跑
    必绿。测试自己拿 `utcnow()` 造"五天前的消息"，而被测接口用的是 `now_in(user.timezone)`，
    两边没有同一个时间锚。所以这个用例当时验的不是规则，是"我们碰巧在哪个钟点跑的它"。
  - 修法是给"此刻"加一个**可替换的依赖**：`backend/api/dependencies/clock.py` 的 `get_now`，
    与 `get_reasoner` 同一个做法（`app.dependency_overrides`）。换掉的是**钟**，不是规则：
    22:00–08:00 该压的照样压，生产路径上没有任何为测试让路的分支。
    `list_reminders`（读）与 `snooze_reminder`（写）**用的是同一个依赖** —— 落库的到期时刻
    和之后拿它比大小的"此刻"必须是同一个钟，一个请求里只能有一个"此刻"。
  - 产品语义就此定下：**"用户回归"这条提示遵守免打扰时段**（它和"这周有 N 场没记录"
    同属"催人"那一类），**不为了测试通过而绕开规则**。想改这条语义，改的是
    `_STATE_KINDS`，而它会立刻让 `test_quiet_hours_suppress_nudges_but_not_pending_things`
    变红 —— 那正是它待在那里的理由。
  - `test_reminders.py`（9 条）现在用**指定的绝对时刻**验五件事：非免打扰时段能生成提示；
    免打扰时段被抑制且 `suppressedCount` / `note` 如实报数；时段边界（21:59 / 22:00 /
    07:59 / 08:00）；同一个瞬间在两个时区的账号上一个在免打扰里、一个不在；"稍后 24 小时"
    跨过承诺那一刻才回来（时间是被**挪动**的，不是把库里的到期时刻改到过去）。
  - 其中 4 条断言做过**反向验证**：把规则一行改坏（`user_returned` 加进 `_STATE_KINDS`、
    `>=` 改成 `>`、时区换成默认值、`snooze` 自己取真实时钟）→ 对应用例变红 → 改回 → 全绿。
    它们是能失败的断言，不是"怎么写都会过"的那种。
  - **再往前那两条是历史记录**：`1d6e30f` 的 "348 / 1 失败"（22:38，免打扰时段内）与
    `bb6a819` 的 "348 / 0 失败"（同日白天，碰巧不在这个时段内）。两条都不代表现在。
- 后端全套测试（**步骤 2 之后**）：**396 条全部通过 / 0 失败 / 0 跳过**
  （`PYTHONUTF8=1 python -m pytest backend/tests -rs`，conda 环境 `zhitu`，
  2026-09-26 **23:30** 本机）。被测代码 = 提交 `6c47a62`，跑之前 `git status --porcelain`
  **为空**；这是**干净工作区**的跑法，与上一条（`858c653`，350 条，脏工作区）不同。
  22:53 → 23:30 两次都落在免打扰时段里，但那个钟点**已经不再是结论的一部分**了 ——
  上一条把它修掉了，这一次只是又一次正好赶在夜里。

  - 比上一条多出的 46 条里，39 条是新文件 `backend/tests/test_relations_and_layout.py`，
    7 条是权限矩阵里为 5 个新路由补的匿名/跨账号探针（矩阵现在 58 条）。
  - **迁移跑过两遍真库**（不是只跑对拍测试），都在**临时库**上，没有碰开发库。
    命令是 `DATABASE_URL=sqlite+aiosqlite:///<临时文件> python -m alembic -c backend/alembic.ini upgrade <目标>`：
    - 空库：`upgrade head` → 从 `3afc2912841f` 依次经过 5 个版本升到 `eab5fc18adde`，
      24 张表，`alembic_version` 里就是 `eab5fc18adde`。
    - 旧库：先 `upgrade 8b3ec72e4d24`，插一行 `plan_nodes`，再 `upgrade head` ——
      那一行的 `content_version` 被 server_default 回填成 1，其余字段原样。
      （这条验的是"现有用户数据不受影响"，对拍测试验不了它。）
  - 写测试时发现了一个真 bug 并修掉：一次 `PUT /layout` 里同一个节点出现两次（前端节流
    保存时会这么发）时，服务端会给它插两行，唯一约束到 flush 才炸 —— 用户看到的是一句
    和拖动毫无关系的 500。修法是按"后面的赢"去重。**这条做过反向验证**：把去重去掉，
    `test_layout_positions_are_counted_per_row_not_per_request` 复现
    `UNIQUE constraint failed: node_positions.user_id, ...`，改回后全绿。
  - `ruff check backend/`、`npm run typecheck`、`npm run lint`、`npm run contracts:check`
    全绿（契约检查 46 个 interface 逐字段比对）。
  - **前端 Playwright 这一轮没有重跑**，这是有意的：步骤 2 只加了后端接口与
    `src/lib/backend.ts` / `api.ts` 里的类型（新增 interface、把 `'PUT'` 加进
    `api.ts` 的方法联合），没有碰任何渲染路径 —— 画布还画不出线，那是步骤 3。
    类型层面的改动由 `typecheck` + `contracts:check` 覆盖。所以 Playwright 的
    "31/1/0" 记录仍停在上一个提交，**没有**在步骤 2 上重新验过。
- 另外：**不需要任何模型 key** —— 假模型是依赖注入的注入点，conftest 里三个
  autouse guard 保证没有一条测试悄悄走了真实分支。步骤 2 也**完全不调模型**。
- 前端 `npm run typecheck` / `npm run lint` / `npm run contracts:check` 全绿。
- **Playwright 共 36 条（15 个文件）**，跑法分两种，**只有第二种是验收**：

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
     只剩这一行的数字。**这一份损失不补造，也不假装它还在** —— 现在补的是机制（见下面
     "一次运行一个现场目录"）。

  那六条后来在**删除示例空间**那一步连测试一起改写（示例数据没了，断言的旧行为也不存在
  了），不再逐条留在套件里。改写后的结果是下面这一条。
- **当前基线（2026-09-26 22:30 前后实测）：`npm run test:accept`，生产构建 + 独立
  `next start`（5273）+ 隔离测试后端（8100，临时 SQLite，无模型 key），`--workers=1`，
  提交 `1d6e30f`（跑的时候那份代码还没提交，工作区 38 项改动即该提交的全部内容）
  —— 连跑三轮 31 passed / 1 skipped / 0 failed，再加一次**全新库**的完整运行同样是
  31 passed / 1 skipped / 0 failed（每轮 1.1~1.4 分钟）。**

  那 1 条 skipped 是 `experience.spec.ts` 里一个写明原因的 `test.fixme`（随笔只写内存、
  后端还没有随笔表，见下）。**它不是通过，也不该被读成通过。**

  这一版让基线从"4~6 条摆动"收敛到 0，靠的是**改测试而不是放宽等待**：三条时序类改成
  "等效果出现"，一条断言过期改成按新布局写，一条测量对象失效改成**先等计划到达再量**。
  没有动 `expect.timeout` 的默认值，没有开自动重试。

  两条特别值得记下来的，因为它们的失败信息都指向了错误的方向：

  | 用例 | 报出来的样子 | 真正的原因 |
  | --- | --- | --- |
  | `timeline.spec`（有截止日的任务那条） | "计划没有从后端到达" | 它和另外十件挤在同一天，只有四张卡放得下，**谁放得下由读回来的节点顺序决定** —— 连跑三次画出来的四张每次都不一样。计划早就到了，没画出来的那张在「另有 N 项」里，那是这一页**正确**的行为。修法：让它独占一天 |
  | `dark-theme.spec`（时间线表面那条） | "找不到任何画了背景的祖先" | 计划到达时**画布子树会重新挂载一次**（`Workbench.tsx` 的 `<PathView key={spaceId}/>`，`spaceId` 从 `'goal'` 变成真实 UUID），量到的元素已经脱离文档，取 `getComputedStyle` 返回一串空字符串。这是**测量对象被换掉**，不是配色出错。修法：先等卡片画出来再量 |

- **本次（2026-09-26 23:07 实测）**：`npm run test:accept` = **31 passed / 1 skipped / 0 failed**
  （113.2s，`--workers=1`）。运行编号 `20260926-230710-a5892ed`，现场在
  `apps/web/artifacts/runs/20260926-230710-a5892ed/` —— 它的 `summary.txt` 里写着跑的时候
  工作区**有 3 项未提交改动**，就是被验的那三份代码：`playwright.config.ts`、
  `scripts/dev/accept-e2e.mjs` 和本文件。跑完原样提交，提交是 **`cf4ee81`**
  （`test(e2e): keep every acceptance run's evidence in its own directory`）。
  那个提交里有 4 个文件 —— 多出来的 `apps/web/README.md` 是这一轮**之后**才改的，
  纯文档，不是那一次跑的东西。往前那几轮（`1d6e30f` 的三轮 + 一次全新库）结果相同。

  这一轮同时验了**新的现场目录**：截图、trace、JSON 报告、两个日志、摘要都落在**这一次
  自己的**目录里（见下面那一条）；失败路径另用一条**故意失败**的临时用例验过一遍 ——
  失败截图、`trace.zip`、报告里的失败清单、测试库快照（`.db` + `-wal` + `-shm`）都在，
  临时目录也保留；那条用例与那份现场**已经删掉**，不留在这里冒充一次真实回归。

- **本次（2026-09-27 00:24 实测，画布状态稳定性修复）：`npm run test:accept` =
  35 passed / 1 skipped / 0 failed（**36 条**，15 个文件，89.5s，`--workers=1`）。**
  运行编号 `20260927-002457-c65959e`，现场在
  `apps/web/artifacts/runs/20260927-002457-c65959e/`。它的 `summary.txt` 里写着
  **工作区：干净** —— 被测代码就是提交 **`c65959e`** 本身，不是"在别的代码上跑出来的绿"。
  条数从 32 涨到 36 是新增的 `apps/web/tests/canvas-stability.spec.ts`（4 条）。
  那 1 条 skipped 仍然是 `experience.spec.ts` 里写明原因的那条 `test.fixme`
  （随笔只写内存），**它不是通过**。

  这一轮同时把上面那条"还没跟着走的"补上了（提交 `b2e8b13`）：5 张记录用截图与这一次的
  `test-results/` 并排落在上面那个目录里 —— 实测，不是推理。

  **这一轮的账要按下面这样读，别扩大：**

  - 这一轮跑的是**前端**。这一版没动后端，所以 `backend/tests` 那 396 条**不在这一轮里
    重跑**，它们的结果仍然只属于提交 `6c47a62` 那一轮。本轮没有新增后端失败可报，
    但这**不等于**"后端也全过了"。
  - **PostgreSQL 未验证。** 这一轮和以往每一轮一样，只用临时 SQLite；"未发现新增失败"
    不是"双库都验过了"。
  - 缺陷是先证明后修的：同一份测试在修复前的代码上是 3 failed / 1 passed，两条命令、
    两份现场目录都记在 `docs/10-NEXT-BATCH-SCOPE.md` 4.5 节。

- **本次（2026-09-27 01:12 实测，用户自己连关系那一步 / 步骤 3A）：`npm run test:accept`
  = 44 passed / 0 failed / 1 skipped（45 条，16 个文件，141.7s，`--workers=1`）。**
  运行编号 `20260927-011257-753c234`，现场在
  `apps/web/artifacts/runs/20260927-011257-753c234/`。它的 `summary.txt` 里写着当时
  工作区有 **9 项未提交改动** —— 就是提交 **`4b305c2`** 的全部内容，跑完原样提交。
  条数从 36 涨到 45 是新增的 `apps/web/tests/relations.spec.ts`（9 条）。
  那 1 条 skipped 仍然是 `experience.spec.ts` 里写明原因的那条 `test.fixme`（随笔只写
  前端内存），**它不是通过**。

  **账这样读，别扩大：**

  - 这一轮跑的也是**前端**。后端 `backend/tests` 那 396 条**不在这一轮里重跑**，
    它们的结果仍然只属于提交 `6c47a62`。本轮没有新增后端失败可报，但**不等于**
    "后端也全过了"。
  - **PostgreSQL 未验证**，和每一轮一样只用临时 SQLite。
  - 这一轮里同时修掉了一个**前端方向 bug**，值得单独记：拖线原来是"先建一条
    `related_to`、再拿返回的行灌进编辑器"，而「相关」是无向的、后端按 UUID 排序规范化
    两端 —— 用户从 A 拖到 B 再改成有向的「影响」，存下来的方向在 A→B 与 B→A 之间
    听天由命。它不是推理出来的：`relations.spec.ts` 那条拖线用例第一次跑就撞上了反的
    那一半（现场 `apps/web/artifacts/runs/20260927-005313-551a8da/`，报告里
    `sourceId`/`targetId` 与拖的方向相反）。修法是拖完先确认再写一次（见该提交信息）。
  - 同一轮里还撞到一条**测试侧**的红：`experience.spec.ts` 发布随笔那条
    （现场 `artifacts/runs/20260927-010324-551a8da/`），30 秒超时，发布按钮一直是灰的。
    现场能证明**不是产品**：同一页里后填的「标签」「关联计划」都留着值，只有最先填的
    正文框是空的 —— 那一枪打在 React 水合之前，写完就被水合按 state 恢复掉了。
    把 `/_next/static/` 的 JS 拖慢 3 秒可以把这件事稳定复现（探针现场
    `artifacts/runs/20260927-011015-551a8da/`，探针**已删**）。修法是换顺序（先点开
    「标签」当"这一页醒了"的证据，再写正文），不是放宽等待，断言一条没松；
    提交 **`753c234`**，在 3A 那个提交之前 —— 所以上面这次验收跑的就是它。

- **本次（2026-09-27 01:27 实测，草稿隔离与测试仪器那两条守卫 / 步骤 3 的两条小账）：
  `npm run test:accept` = 45 passed / 0 failed / 1 skipped（46 条，16 个文件，141.3s，
  `--workers=1`）。** 运行编号 `20260927-012731-14c78b0`，现场在
  `apps/web/artifacts/runs/20260927-012731-14c78b0/`。它的 `summary.txt` 里写着当时
  工作区有 **5 项未提交改动** —— 就是提交 **`b30ca68`** 的全部内容，跑完原样提交。
  条数从 45 涨到 46 是 `canvas-stability.spec.ts` 新增的"换个人登进来"那一条。
  那 1 条 skipped 仍然是 `experience.spec.ts` 里写明原因的那条 `test.fixme`（随笔只写
  前端内存），**它不是通过**。

  **这一轮里真正值得记的是"那条新用例先红过"这件事本身：**

  - 草稿（`features/growth/drafts.ts` 那个模块级 Map）以前**没人清**：退出登录只把
    `user` 置空，模块级状态不跟着组件走。键是 `${空间}:${层级}`，真实空间那半是各自的
    UUID、两个账户本来就不会撞 —— 所以**站在真实空间上写这条用例，不装守卫它也会绿**。
    唯一所有账户共用的是"还没进空间"的那一份：`'none'`（`provider.tsx` 的 `NO_SPACE`）
    配层级哨兵 `'goal'`。用例因此必须把画布**停在那扇窗里**（挂住
    `GET /api/workspaces/{id}`，并**先断言根还是哨兵 `'goal'`**，前提不成立就红在前提上）。
    那扇窗是真实的：点"进入工作台"之后空间详情还在路上的那几百毫秒里，占位画布的
    "新建节点"是可点的（`canCreate` 只看 `planLoading`）。
  - **红是真的跑出来的，不是推理的**：把 `clearDrafts()` 掏空重跑，那条用例红在
    "换账户之后，甲那边开着的新建弹窗跟着过来了"（期望 0、实到 1），现场
    `apps/web/artifacts/runs/20260927-012235-dev/`；装回去就绿。第一版的红报的是
    strict mode violation（两个"新建节点"，第二个在甲那条弹窗里），所以把断言顺序改成
    "先问弹窗在不在" —— 让这条测试变红时，第一句读到的就是它要说的那件事。
  - 同一轮还给 `window.__zhituCanvasLifecycle` 封了顶（50 条）。它写在全局对象上、
    正常使用里每切一次视图就涨一条；封顶会让"没涨过"变成恒真，所以"不许重挂载"那条
    用例**先确认没数到上限**再断言。

  **账这样读，别扩大：**

  - 这一轮跑的也是**前端**。后端 `backend/tests` 那 396 条**不在这一轮里重跑**，
    它们的结果仍然只属于提交 `6c47a62`。本轮没有新增后端失败可报，但**不等于**
    "后端也全过了"。
  - **PostgreSQL 未验证**，和每一轮一样只用临时 SQLite。
  - **开发模式今天仍然走不通**，原因与 3A 那一轮记的一字不差（8000 上的开发后端是
    `6c47a62` 之前启动的，`/plan` 里没有 `relations`）。这一轮又实测确认了一次：工作台
    在浏览器里直接抛 `Cannot read properties of undefined (reading 'map')`
    （`planProjection.ts` 的 `plan.relations`），**不是选择器问题**。那一个进程没有动，
    也没有批量结束任何 Node/Python 进程。

- **本次（2026-09-27 01:42 实测，布局落库那一步 / 步骤 3B）：`npm run test:accept`
  = 51 passed / 0 failed / 1 skipped（52 条，17 个文件，143.3s，`--workers=1`）。**
  运行编号 `20260927-014236-ce704a6`，现场在
  `apps/web/artifacts/runs/20260927-014236-ce704a6/`。它的 `summary.txt` 里写着当时
  工作区有 **5 项未提交改动** —— 就是提交 **`53762a2`** 的全部内容，跑完原样提交。
  条数从 46 涨到 52 是新增的 `apps/web/tests/layout.spec.ts`（6 条）。
  那 1 条 skipped 仍然是 `experience.spec.ts` 里写明原因的那条 `test.fixme`（随笔只写
  前端内存），**它不是通过**。

  **这一轮的红是先跑出来的：** 同一个文件、同一套隔离栈，把
  `provider.tsx` / `PathView.tsx` / `canvas-polish.css` 三个文件 stash 掉再跑
  （`npm run test:accept -- tests/layout.spec.ts`，运行编号 `20260927-014055-ce704a6`，
  现场同名前缀的目录），**6 条全红**，报的就是这一批要修的那几件事：拖动之后后端一直
  没收到位置、平移之后一直没收到视口、切空间之后刚才那一次拖动白拖了、后端记着的视口
  被自动 fit 顶掉（实到 `{"x":176,"y":308,"zoom":1}`）、子空间里的位置不是库里那一份
  （实到 `{"x":380,"y":30}`）。跑完把 stash 弹回，再做上面那次全量绿。

  **账这样读，别扩大：**

  - 这一轮跑的也是**前端**。后端 `backend/tests` 那 396 条**不在这一轮里重跑**，
    它们的结果仍然只属于提交 `6c47a62`。本轮没有新增后端失败可报，但**不等于**
    "后端也全过了"。
  - **PostgreSQL 未验证**，和每一轮一样只用临时 SQLite。
  - **开发模式今天仍然走不通**，原因与 3A、`b30ca68` 那两轮记的一字不差（8000 上的开发
    后端是 `6c47a62` 之前启动的，`/plan` 里没有 `relations`）。那一个进程没有动，
    也没有批量结束任何 Node/Python 进程。
  - **"刷新之后还在"不算这一轮的证据。** `localStorage` 里本来就有位置的副本，同一个
    浏览器刷新能绿得毫无意义。所以这 6 条里凡是断言"真的落库了"的，都是**另开一个空
    `localStorage` 的浏览器上下文**（换台机器的替身）去读 —— 这一条写在那个 spec 的
    文件头里。
  - **后端已有的那几条没有重写一遍**：整批拒绝、不删除未提交的行、按用户取、
    跨账号 404 在 `backend/tests/test_relations_and_layout.py` 里。前面这次跑的是前端。
  - **自动 fit 会往 `scope_viewports` 写一行**，这是**故意的**，不是 bug：它确实是用户
    看到的那一份视口。测试里因此不能只等"有一行了"，要等那一行**等于画布上的值**
    （第一版就是这么红的：等到的是 fit 自己写的那行 `panX 176`）。

- **本次（2026-09-27 02:26 实测，可恢复归档 / 步骤 3C）：`npm run test:accept -- --workers=1`
  = 52 passed / 0 failed / 1 skipped（53 条，17 个文件，148.7s）。**
  运行编号 `20260927-022634-6a59798`，现场在
  `apps/web/artifacts/runs/20260927-022634-6a59798/`。它的 `summary.txt` 里写着当时
  工作区有 **20 项未提交改动** —— 就是提交 **`5a13631`** 的全部内容，跑完原样提交。
  条数从 52 涨到 53 是 `apps/web/tests/node-delete.spec.ts` 从 3 条重写成 4 条
  （归档 → 恢复的整条闭环）。那 1 条 skipped 仍然是 `experience.spec.ts` 里写明原因的
  那条 `test.fixme`（随笔只写前端内存），**它不是通过**。

  **这一轮前端绿之前先红过一次，而且红的是测试侧 —— 那条账要留着：**

  - 第一次全量跑（运行编号 `20260927-022236-6a59798`，1 failed / 51 passed）红的是
    `canvas-stability.spec.ts` 的"一次真实的计划写入之后，画布不重建"。原因不是产品
    回归：垃圾桶按钮的 `aria-label` 从 `删除X及其子节点` 改成了 `归档X及其子节点`，
    而那条用例是按名字点的；它还只点一下，可写库的动作已经挪到确认框里了。改成
    "点垃圾桶 → 在确认框里点归档"之后绿。**它同时证明套件抓得住这次改名** ——
    一个只在别处改名字的提交不会静默溜过去。
  - 两条新用例的红是**故意造出来**的，不是等来的：把工具栏按钮上"不清归档结果"那行
    改回 `setArchiveNote(null)`，`node-delete.spec.ts` 恰好一条变红
    （`20260927-021455-6a59798`，1 failed / 3 passed），红的正是"归档那一下的结果要
    看得到"；改回后 4 passed（`20260927-021554-6a59798`）。

  **账这样读，别扩大：**

  - 这一轮**同时**跑了后端：`PYTHONUTF8=1 python -m pytest backend/tests -rs`
    = **410 passed / 0 failed / 0 skipped**（82.50s，conda 环境 `zhitu`）。比 `6c47a62`
    那一轮的 396 条多 14 条（新文件 `test_archive_restore.py` 10 条、权限矩阵补的探针、
    一条改了断言的投影用例）。这是 3C 之后**第一次**把两边合起来报，不要把它读成
    "前几轮的后端也重跑过了"。
  - **PostgreSQL 未验证**，和每一轮一样只用临时 SQLite。
  - **迁移没有在用户的开发库上跑过，也没有动那个进程。** 这一轮**重新量过**这两件事，
    不是抄前几轮的话：`data/zhitu_dev.db` 里的 `alembic_version` 仍是 `8b3ec72e4d24`
    （以只读方式打开读的），而 head 已经是 `b7d41c9f2a68`（3C 只加 `plan_nodes.purged_at`
    一列）；8000 上那个 python 进程（PID 44968）的启动时间是 **2026-09-26 16:50:38**，
    比提交 `6c47a62`（同日 **23:30:47**）早近 7 小时 —— 它**不可能**带着那份代码（后端
    没有 `--reload`）。所以**开发模式今天仍然走不通**，原因与前几轮记的是一致的，只是
    这次是当场量出来的。要不要升那个库需要用户点头，这一轮没碰，也没有批量结束任何
    Node/Python 进程。
  - **"彻底删除"这一轮只有接口，没有界面入口。** 界面上只有可恢复的归档。这一条是
    有意留的缺口，写在提交信息里：不拿"接口支持了"当成"用户能做"。

- **一次运行一个现场目录（2026-09-26 补上）：上一次的截图、trace、报告不许被下一次覆盖。**

  这件事以前是坏的：`playwright.config.ts` 的 `outputDir` 写死成 `artifacts/test-results`，
  跑两轮就是互相覆盖 —— 上面第 4 轮丢的现场正是这么丢的。现在每一次运行有自己的目录和
  **运行编号**：

  ```
  apps/web/artifacts/runs/<本机时间戳>-<提交号>/
    summary.txt      这一次的记录：运行编号、提交、工作区是否干净、命令、端口、库、耗时、失败清单
    report.json      Playwright 的 JSON 报告（机器可读的那份）
    test-results/    失败截图、trace，以及每条测试自己的输出
    backend.log      后端启动日志
    build.log        前端构建日志
    zhitu_e2e.db     只在失败时拷进来，含 -wal / -shm 侧车文件
  ```

  几条刻意的选择：

  - **`summary.txt` 里有一行「工作区」。** 运行编号里带提交号，但**提交号只是这一刻工作区的
    标签，不是证据本身** —— 跑的时候工作区可能是脏的。这一行正是为了不让一个提交号被读成
    "跑的就是这份代码"。
  - **失败时不删临时目录，并把测试库快照拷进现场目录。** 库很小，而"失败了但现场被清掉"
    是最难补救的一种损失。SQLite 的 `-wal` / `-shm` 要一起拷 —— 后端是被 kill 的，不会再做
    checkpoint，最近的写入只在这些文件里。
  - **没跑到测试那一步就如实写"没有 report.json"**，不写"0 失败"：那会把"没跑"读成"全过"。
  - 本地 `npm run test:e2e` 也会按时间戳给自己建一个目录（不会盖掉别的运行），但它没有
    `summary.txt` —— 摘要由验收脚本写，只有它知道端口、提交和数据库在哪。
  - **那 5 张"记录用"截图现在也跟着走了（2026-09-27，提交 `b2e8b13`）。** 它们原来写在
    固定的 `artifacts/*.png` 上，跑一次盖一次，上面那条"还没跟着走的"就此作废 ——
    现在测试从 `tests/support/artifacts.ts` 的 `artifactPath(name)` 取路径，目录由
    `pinRunDir()` 在任何 worker 起来**之前**写进 `process.env`（worker 是 fork 出来的、
    继承那一刻的环境；不写这一步，主进程与 worker 会各算一个时间戳，截图落进另一个
    同样像"这一轮"的目录 —— 这条是实测出来的，不是推理）。文件名仍然是固定的
    （人一眼认得出是哪一张），变的只是前面那一层目录。
  - 旧的那几轮现场（`artifacts/round-1/` 等）留在原处不动，它们是历史记录。

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

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
- 后端全套测试：**348 条通过**（`PYTHONUTF8=1 python -m pytest backend/tests`，
  conda 环境 `zhitu`，提交 `bb6a819`，2026-09-26 本机约 151 秒；348 passed / 0 failed /
  0 skipped），且**不需要任何模型 key** —— 假模型是依赖注入的注入点，conftest 里三个
  autouse guard 保证没有一条测试悄悄走了真实分支。
- 前端 `npm run typecheck` / `npm run lint` / `npm run contracts:check` 全绿。
- **Playwright 共 32 条（15 个文件）**，跑法分两种，**只有第二种是验收**：

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

- **"30 条全绿"是历史记录，不是当前的保证。** 那是套件涨到 32 条之前、示例空间还是
  默认入口时，在 `next dev` 上用多 worker 跑出来的结果（提交 `0719b90` 前后）。它不能
  描述现在的状态，原因有两个，都是量出来的：套件已经涨到 32 条；而且**同一提交、
  同一配置下串行连跑五轮，失败数在 4~6 之间摆动** —— `bb6a819`，生产模式、隔离库、
  `workers=1`：6 / 4 / 6 / 6 / 4 失败。**"串行就一定稳"这个假设是错的**，串行只是基线
  口径，不等于稳定。

  当前状态是**已定位、未清零**：每轮必红的三条是 `leaf-path:21`、`leaf-path:40`、
  `experience:12`；另外三条 `dark-theme:347`、`node-delete:44`、`workbench:12` 时红时绿。
  六条原因**全部落在测试侧或测量侧，没有一条是功能回归**，也没有一条是页面加载超时或
  接口失败。逐条现场（错误原文、失败截图、`trace.zip`）保存在 `apps/web/artifacts/`
  的 `round-1/`、`round-2/`、`round-3/`、`round-5/`，每个失败用例一个目录：

  | 用例 | 类别 | 原因 |
  | --- | --- | --- |
  | `leaf-path:21`、`experience:12` | 测试时序 | 点完「返回上级空间」立刻双击，画布还在重排，双击没落在节点上 |
  | `workbench:12` | 测试时序 | 同一族：双击没落地 → `enterSpace` 没跑 → 提示语当然不变 |
  | `node-delete:44` | 测试时序 | 同一族：`hover` 之后节点被重排移走，`opacity` 涨到 1 又掉回 0 |
  | `leaf-path:40` | 断言过期 | 上一次提交把子空间布局由纵向改成横向，断言还按纵向写 |
  | `dark-theme:347` | 测量对象失效 | 量到的元素已脱离文档（`isConnected = false`），取不到背景色 |

  三条"测试时序"的修法是**等画布稳定**（等效果出现），不是放宽 `expect.timeout`、
  也不是加重试 —— 那两者会把真实问题一起吞掉。按"示例空间测试只在功能确实移除后才
  调整"的约定，这五条留到删除示例空间那一步一并改写。

  真实空间那几条 —— `/plan` 载荷与画布节点数一致（`plan-projection.spec.ts:67`）、
  无日期节点不从时间线消失（`:208`）、建的任务真能排进日程并落库（`live-loop.spec.ts:37`）
  —— 不在上面六条里，五轮全绿，验的正是上面第 4、5 条要警惕的"界面上有、库里没有"。
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

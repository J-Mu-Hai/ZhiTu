# Development

## 环境准备

```bash
cp .env.example .env
```

**本地不需要装数据库。** `DATABASE_URL` 留空时用仓库内的 SQLite(`data/zhitu_dev.db`),
建表走 Alembic:

```bash
# 从仓库根跑,并且要 -c 指过去 —— alembic.ini 在 backend/ 下
python -m alembic -c backend/alembic.ini upgrade head
python -m alembic -c backend/alembic.ini current     # 应打印 (head)
```

线上用 PostgreSQL 时改 `DATABASE_URL` 即可,同一套模型两端都能跑(可移植性红线见
[05-DATA-MODEL.md](05-DATA-MODEL.md))。

> 这里曾经写着 `docker compose up -d db redis`。**本机没有 Docker**,而且 Redis 已从
> 依赖里移除(登录限流与熔断是进程内实现,每个 worker 各一份)。照着旧指引做的人会
> 卡在第一步,却以为是自己的环境有问题。

## 后端

必须用 conda 环境 `zhitu` —— 仓库里的 `.venv` 是 uv 建的、缺 `aiosqlite`,拿它启动会
死在数据库那一步。

```bash
conda activate zhitu
cd <仓库根目录>
uvicorn backend.api.main:app --port 8000
```

注意三点:

1. **`cd backend` 再 `uvicorn api.main:app` 也能起,但 pytest 不行。** 测试要靠
   `backend.*` 这个包名导入,而 `pytest.ini` 把 `pythonpath` 设成了仓库根。所以
   **测试一律从仓库根跑**。
2. **uvicorn 没有 `--reload`。** 改了 `backend/` 下任何文件都要手动重启,否则接口还是
   旧的 —— 服务在跑、`/ready` 正常,但新加的路由 404。这个坑很隐蔽。
3. Windows 控制台是 cp936,跑 Python 带上 `PYTHONUTF8=1`,否则中文输出乱码。

```bash
# 全套测试不需要模型 key:假 reasoner 是注入点,conftest 里还有三个 autouse 守卫
# (清空 key、让真实 httpx 出网直接失败、断言假模型确实被调用过)
PYTHONUTF8=1 python -m pytest backend/tests

python -m ruff check backend
```

> 别再加 `-q`:`pytest.ini` 的 `addopts` 里已经有一个,再给一个就是 `-qq`,
> **连 "331 passed" 那行都会消失** —— 看起来像是跑完了却什么都没说。
> 想看统计用 `python -m pytest backend/tests -o addopts="--strict-markers"`。

Agent 编排层用 openJiuwen,它**是可选的**:未安装时后端照常启动,规划请求走直连模型,
响应里的 `source` 会如实标注。见 [backend/requirements-agent.txt](../backend/requirements-agent.txt)
与 [backend/agent/README.md](../backend/agent/README.md)。

## Web

```bash
cd apps/web
npm install
npm run dev          # 5173
```

技术栈:React + Next.js + React Flow。

**后端的 CORS 只放行 5173 / 3000**(`backend/core/config.py` 的 `cors_origins`)。把前端
跑到别的端口会在浏览器里被 CORS 拦掉,注册/登录全失败 —— 而报错只在浏览器控制台,
后端日志里什么都看不到。要换端口得同时改 `CORS_ORIGINS`。

## 本地完整体验(不部署服务器)

想在不碰生产、不部署的前提下,在浏览器里把 P0–阶段 4 的功能走一遍,用
[`scripts/dev/dev-up.ps1`](../scripts/dev/dev-up.ps1)。它是 Windows PowerShell 脚本。

```powershell
# 启动(Auto:有真实 key 用真实模型,没有就脚本回放)
powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1

# 固定脚本回放(不消耗任何模型额度)
powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Mode Script

# 看一眼将执行什么,什么都不动
powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -DryRun

# 看状态 / 停止 / 重置数据库
powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Status
powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Stop
powershell -ExecutionPolicy Bypass -File scripts\dev\dev-up.ps1 -Reset   # 会先打印完整路径并要求输入 yes
```

- **独立数据库**:`data/zhitu_local_experience.db`,通过子进程环境变量 `DATABASE_URL`
  覆盖,**不改仓库根的 `.env`**。脚本只终止状态文件里记录、且命令行匹配本仓库的进程。
- **`-Mode Real / Script / Auto`**:`Auto` 在根 `.env` 有真实 `LLM_API_KEY` 时用真实
  模型(`AGENT_REASONER=auto`),否则自动切到脚本回放(`script` + 本地 demo fixture)。
  `Real` 在没有真实 key 时拒绝启动。
- 启动后地址:`http://127.0.0.1:5173/workbench`;后端 `http://127.0.0.1:8000/ready`。
  日志与 PID 状态在 `logs/local-experience/`(Git 忽略)。

### 脚本回放的体验顺序

fixture 在 [`scripts/dev/fixtures/local-demo-script.json`](../scripts/dev/fixtures/local-demo-script.json)。
**它不是产品能力** —— 界面上来源徽标会写「脚本回放」。按这个顺序对话:

1. 说一句**没有截止日期**的事实(如「我现在排名 38」)→ 看信息节点提案;
2. 再说一句让你先定方向 → 出现一张问题卡,选一个选项并提交;
3. 提交后出现**战略选择**提案(优先/暂缓/依据/风险),点确认;
4. 再让它往下走 → 它会先请求**只读排期模拟**,然后提出本周重点与关系建议;
5. 每一步都点「确认,写入计划」才会真正落库;刷新页面看节点/关系是否还在。

> 本地体验**不验证** HTTPS、Nginx、Vercel、生产 PostgreSQL 或线上 CORS。
> 那几件事只能在真实部署上验,见 [08-DEPLOYMENT.md](08-DEPLOYMENT.md)。

## 移动端

```bash
cd apps/mobile
flutter pub get
flutter run
```

`android/` 与 `ios/` 目前只有占位,首次需要在本机生成:

```bash
cd apps/mobile && flutter create . --platforms=android,ios
```

## 原型

原型放在 [../prototypes/](../prototypes/) 下,**不进生产构建**。
在原型里怎么改都行;验证成功的东西才迁移到 `apps/`。

## 团队节奏

```
Mother Demo → Parallel Exploration → Integration → Next Version
```

**每 2~3 天 Integration。**

## 改动规则

- **数据契约的权威在 [backend/contracts/](../backend/contracts/) 的 Pydantic 模型里。**
  改动顺序是:改模型 → `python -m backend.contracts.schema` 重新生成 →
  看一眼 diff。**不要手改 [shared/schemas/domain.schema.json](../shared/schemas/)**
  —— 它不在运行路径上,改了不会有任何东西生效,只会让下次 `pytest` 变红。
  详见 [shared/schemas/README.md](../shared/schemas/README.md)。
- **改完契约要顺手跑 `cd apps/web && npm run contracts:check`。** 后端改契约时
  `pytest` 会逼着人重新生成 schema,但 [apps/web/src/lib/backend.ts](../apps/web/src/lib/backend.ts)
  里那批手写的 interface 没有任何东西管 —— 它要么漏写一个后端给了的字段(前端
  够不着,且无处报错),要么写着一个永远收不到的字段(读到的恒为 `undefined`,
  TS 查不出来)。这个脚本逐字段对照两者,不生成代码,只报告差在哪。
- 数据库结构变化走 Alembic(`backend/migrations/`,batch 模式)。注意 autogenerate
  **检测不到 CHECK 约束的变化**,改 CHECK 必须人工审迁移。
- 产品行为变化后,更新文档
- 大改动之前先说明影响范围,见 [../代码ai的约束.md](../代码ai的约束.md)

## 待补全

- [ ] 代码风格与 lint 规则(当前只用 `ruff check backend` 的默认规则)
- [ ] 分支模型与提交信息规范
- [ ] 测试策略:单测 / 集成测试的边界
- [ ] 部署流程(腾讯云 + Vercel,尚未开始)
- [ ] 前端类型从契约产物派生(`json-schema-to-typescript`),当前 `apps/web/src/types/`
      是手写的,与产物之间没有自动比对

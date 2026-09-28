# 知途生产部署交接单（交给 Pi 执行）

更新时间：2026-09-28

## 0. 最终目标

采用以下生产结构，不把后端或密钥放进 Vercel：

```text
浏览器 → Vercel（apps/web，Next.js）
浏览器 → HTTPS API 域名 → 腾讯轻量服务器
                          ├─ Nginx（TLS / 反向代理）
                          ├─ FastAPI + Uvicorn（单 worker）
                          ├─ OpenJiuwen + DeepSeek
                          └─ PostgreSQL（仅监听本机）
```

## 1. 已知环境与当前代码状态

### 本地仓库

- 路径：`E:\代码\ZhiTu`
- Git 远程：`https://github.com/J-Mu-Hai/ZhiTu.git`
- 当前分支：`master`
- 当前 HEAD（开始本轮修改前）：`48cc224`
- 当前有 7 个源码/测试文件未提交，都是最新两项产品修改，**必须保留，禁止 reset/checkout 丢弃**：
  - 手工创建节点默认视觉独立，不再因为 `parentId` 自动连根节点；`parentId` 仍表示 NodeSpace 归属。
  - AI 创建的节点仍自动显示结构连线。
  - 节点详情移除优先级、截止日期、预计工时，只保留内容编辑等核心入口。
- `npm run typecheck`、`npm run lint`、`npm run build` 已通过。
- 针对性浏览器验收已通过：节点正文相关 6/6，手工节点独立与刷新持久化通过。
- 全量 109 条验收刚启动后被用户中断：运行编号
  `20260928-202846-48cc224`，只跑完最前面 5 条且通过；**这不算完整验收，必须重跑**。

### 腾讯轻量服务器

- 公网 IPv4：`82.156.143.94`
- 系统：OpenCloudOS 9.4，x86_64
- 规格：4 核 / 4GB RAM / 40GB 系统盘
- 当前磁盘：已用 14GB，可用 27GB
- 当前内存：可用约 2.8GB
- 当前 Swap：0（部署前应增加 2GB）
- 系统 Python：3.11.6。**不要替换系统 Python**，`dnf` 依赖它；项目使用并存的 Python 3.12 虚拟环境。
- 服务器区域、域名、备案状态尚未确认。涉及 DNS/TLS 前必须询问用户。

### 密钥安全

- 用户曾在聊天里公开过一枚 DeepSeek key。**旧 key 视为泄露，生产环境禁止继续使用。**
- 让用户在 DeepSeek 控制台吊销旧 key、创建新 key，并由用户自己写入服务器 `/srv/zhitu/.env`。
- 不要求用户把 SSH 密码、私钥、新 LLM key、数据库密码贴进聊天。

## 2. 严格执行顺序

### 阶段 A：冻结并发布代码

1. 阅读 `代码ai的约束.md`、`docs/CURRENT-DESIGN.md`、`docs/08-DEPLOYMENT.md`。
2. 检查 `git status` 和当前 diff，保留用户改动，不做 destructive git 操作。
3. 运行：

   ```powershell
   cd E:\代码\ZhiTu\apps\web
   npm run typecheck
   npm run lint
   npm run contracts:check
   cd E:\代码\ZhiTu
   node scripts/dev/accept-e2e.mjs --workers=1
   ```

4. 完整验收必须明确报告 passed / failed / skipped。中断、没跑到测试、只跑子集都不能说成通过。
5. 若旧测试仍假设“所有用户节点必有父子结构线”，先判断它是否与新产品规则冲突；只更新过期断言，不掩盖真实功能问题。
6. 完整验收通过后提交当前 7 个文件，提交信息可用：

   ```text
   feat(canvas): 手工节点默认独立并精简节点详情
   ```

7. 推送到 GitHub `master`。推送前再次确认 `.env`、数据库、备份和密钥没有被 Git 跟踪。

### 阶段 B：服务器只读审计（先审计，后安装）

在服务器执行并记录输出：

```bash
ss -lntp
systemctl --failed
getenforce
firewall-cmd --state || true
dnf repolist
dnf list --available 'python3.12*' || true
docker --version || true
git --version || true
nginx -v || true
psql --version || true
lsblk
```

先确认现有端口和服务，不能批量停止 Python/Node/数据库进程。

### 阶段 C：基础系统与 Swap

1. OpenCloudOS 使用 `dnf`，不要执行 Ubuntu 的 `apt` 命令。
2. 安装准确包名前先用 `dnf info/search` 核实；目标组件为 Git、编译工具、Python 3.12、PostgreSQL、Nginx、Certbot。
3. 不替换 `/usr/bin/python3`，项目虚拟环境明确用 `python3.12 -m venv`。
4. 创建 2GB Swap 前检查 `/swapfile` 不存在且磁盘空间充足。推荐过程：

   ```bash
   fallocate -l 2G /swapfile
   chmod 600 /swapfile
   mkswap /swapfile
   swapon /swapfile
   echo '/swapfile none swap sw 0 0' >> /etc/fstab
   swapon --show
   ```

5. 对 `/etc/fstab` 的追加必须避免重复；如果文件里已有 `/swapfile` 就不要再追加。

### 阶段 D：PostgreSQL

1. 使用服务器本机 PostgreSQL，MVP 阶段不需要 Vercel 数据库。
2. 初始化并启用 PostgreSQL；不同 OpenCloudOS 包版本命令可能不同，必须按实际包确认。
3. 创建独立数据库和最小权限用户，例如数据库 `zhitu`、用户 `zhitu`。
4. PostgreSQL 只监听 `127.0.0.1`，不要向公网开放 5432。
5. 数据库密码由用户在服务器本地生成和填写，不贴进聊天。连接串格式：

   ```text
   postgresql+asyncpg://zhitu:<URL编码后的密码>@127.0.0.1:5432/zhitu
   ```

6. 配置每日 `pg_dump`，至少保留最近 7 份；本机备份只是第一层，后续应复制到对象存储或另一台设备。

### 阶段 E：部署后端

1. 创建非 root 系统用户 `zhitu`，代码目录 `/srv/zhitu`。
2. 从 GitHub clone/pull 已验收提交。
3. 用 Python 3.12 创建 `/srv/zhitu/.venv`。
4. 先装 `backend/requirements.txt`，再装 `backend/requirements-agent.txt`。
5. OpenJiuwen 安装后必须验证：

   ```bash
   /srv/zhitu/.venv/bin/python -c "import sqlalchemy, openjiuwen; print(sqlalchemy.__version__)"
   ```

6. 4GB 内存只启一个 Uvicorn worker。OpenJiuwen 很重，Swap 是防止安装/预热时 OOM，不是性能扩容。
7. 创建 `/srv/zhitu/.env`，权限 `600`，属主 `zhitu`。生产核心配置：

   ```env
   APP_ENV=production
   APP_SECRET_KEY=<openssl rand -hex 32 的稳定值>
   DATABASE_URL=postgresql+asyncpg://zhitu:<密码>@127.0.0.1:5432/zhitu
   CORS_ORIGINS=https://<前端正式域名>,https://<项目>.vercel.app

   LLM_API_KEY=<新生产密钥>
   LLM_MODEL=deepseek-chat
   LLM_BASE_URL=https://api.deepseek.com
   AGENT_MAX_TOKENS=8192
   LLM_TIMEOUT_SECONDS=30
   AGENT_REASONER=auto
   ```

8. `APP_SECRET_KEY` 上线后保持稳定；随意更换会让现有登录令牌失效。
9. 在启动服务前执行迁移：

   ```bash
   cd /srv/zhitu
   .venv/bin/python -m alembic -c backend/alembic.ini upgrade head
   .venv/bin/python -m alembic -c backend/alembic.ini current
   ```

10. 复用并按 OpenCloudOS 核对 `scripts/deploy/zhitu-api.service`，Uvicorn 只监听 `127.0.0.1:8000`、`--workers 1`。
11. 启动后先在服务器本机验证：

    ```bash
    curl -sS http://127.0.0.1:8000/health
    curl -sS http://127.0.0.1:8000/ready
    journalctl -u zhitu-api -n 100 --no-pager
    ```

### 阶段 F：域名、Nginx 与 HTTPS

执行前向用户确认：

- 服务器是否在中国大陆；
- 计划使用的根域名；
- ICP 备案是否完成。

推荐：

- 前端：`app.example.com` → Vercel
- 后端：`api.example.com` → A 记录 `82.156.143.94`

若服务器在中国大陆且域名未备案，先停止域名上线动作并说明备案阻塞，不能用 HTTP API 凑合：HTTPS 的 Vercel 页面会拦截 HTTP 后端。

Nginx 注意：

- OpenCloudOS 使用 `/etc/nginx/conf.d/`，不要机械照抄 Debian 的 `sites-available`。
- 反向代理到 `http://127.0.0.1:8000`。
- `proxy_read_timeout` / `proxy_send_timeout` 至少 120 秒。
- `client_max_body_size 1m`。
- 若 SELinux 为 Enforcing，Nginx 代理本机网络可能需要：

  ```bash
  setsebool -P httpd_can_network_connect 1
  ```

- 先 `nginx -t`，再 reload。
- 使用 Certbot 为 API 域名签证书。
- 腾讯轻量防火墙和系统 firewalld 只开放 22、80、443；不开放 8000、5432。

外部验收：

```bash
curl -sS https://api.example.com/ready
```

响应必须包含 `ready: true` 与 `env: production`。

### 阶段 G：Vercel 前端

1. Vercel 导入 `J-Mu-Hai/ZhiTu`。
2. Root Directory 必须设置为 `apps/web`。
3. Framework 让 Vercel识别为 Next.js。
4. Production 环境变量只需：

   ```env
   NEXT_PUBLIC_API_BASE_URL=https://api.example.com
   ```

5. `LLM_API_KEY`、数据库连接串、`APP_SECRET_KEY` 绝不放到 Vercel。
6. 部署后获得 `*.vercel.app` 地址；把实际生产域名和该 Vercel 域名加入服务器 `CORS_ORIGINS`，重启 API。
7. 环境变量修改只作用于新部署，修改后必须 Redeploy。
8. 再绑定正式前端域名 `app.example.com`。

### 阶段 H：生产验收

按顺序执行，任一失败先停下定位：

1. `https://api.example.com/ready`：200、`ready=true`、`env=production`。
2. 从 Vercel 正式地址注册新测试账号并登录（同时验证 HTTPS + CORS）。
3. 创建全新空空间，确认没有演示数据。
4. 双击空白创建手工节点：默认独立、刷新后仍在。
5. 打开节点详情：没有优先级、截止日期、预计工时三项；正文能自动保存。
6. 与 AI 连续对话，提供基础事实；AI 能产生信息节点/规划节点提案。
7. 确认提案后画布生长，AI 规划节点带结构线，手工节点不被自动连根。
8. AI 回复来源必须显示 OpenJiuwen；若显示 `direct_llm` 或规则降级，不能把它报告为 OpenJiuwen 验收通过。
9. 刷新浏览器，节点、关系、正文、对话仍在。
10. `systemctl restart zhitu-api` 后再次刷新，数据仍在。
11. 手机宽度检查工作台、AI 抽屉、时间线和任务视图。
12. 检查浏览器控制台、Nginx 日志、`journalctl`，不应有 CORS、500、502、504。

## 3. 明确禁止事项

- 不要运行 `git reset --hard`、`git checkout --` 丢弃当前未提交修改。
- 不要把现有 `docker-compose.yml` 当生产配置：它包含废弃 Redis、`--reload` 和开发前端。
- 不要替换 OpenCloudOS 的系统 Python 3.11。
- 不要公开 8000、5432。
- 不要把 SQLite 开发库直接当生产数据库。
- 不要把密钥写入 Git、Vercel前端变量、命令输出或交付报告。
- 不要在迁移前启动新版本 API。
- 不要把中断测试、局部测试、规则兜底或直连模型描述成完整生产验收。
- 不要清理服务器现有服务或数据，除非先精确识别目标并得到用户确认。

## 4. Pi 开始时必须先向用户确认的内容

只问以下三项，不索要密码：

1. 服务器所在地域（中国大陆 / 香港 / 其他）；
2. 根域名与计划的前端、API 子域名；
3. 域名是否已完成 ICP 备案。

确认后先完成阶段 A，再进行服务器阶段 B。每个阶段输出真实命令、退出码和验收结果，不把“计划执行”写成“已经完成”。

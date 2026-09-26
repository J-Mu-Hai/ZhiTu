# 知途 Web

知途当前正式 Web 入口，基于 Next.js、React 和 React Flow。该实现由 `prototypes/demo3` 验证后提升而来，保留组长版本的产品结构与交互。

## 本地启动

需要 Node.js 22 或更新版本。

```powershell
cd apps/web
npm install
npm run dev
```

打开 <http://127.0.0.1:5173/workbench>。

端口必须是 **5173**.后端的 CORS 只放行 5173 / 3000：换到别的端口会在浏览器里被 CORS
拦掉，注册/登录全失败，而报错只在浏览器控制台，后端日志里什么都看不到。

## 主题

**当前是暖白主题，不是深色。** 颜色集中在 `src/app/dark-theme.css`。这个文件名是历史
遗留 —— 它的**最后一层**
(`/* Final light-theme layer. Keep this at EOF so legacy dark rules cannot win. */`)
把整套变量覆盖成暖白(`--bg:#f8f6ef`、`--surface:#ffffff`、`--text:#24384d`),上面残留的
深色规则全部被它压住。改颜色请改那一层；在它之前插规则不会生效。

React Flow 时间线的模块样式在 `src/components/workbench/TimelineView.module.css`。

## 当前交互

1. 未登录访问正式页面时进入登录页。
2. 工作台根路径展示学业成绩、科研能力、综合经历、个人成长四个分类。
3. 单击分类进入它的专属路径，当前分类作为中心，已有内容作为下方树叶。
4. 点击“添加树叶”，填写名称、类型与说明。
5. 任意树叶可以通过右上角箭头或双击继续进入，成为下一层中心；通过面包屑或返回按钮回到上级。
6. “我的”页面可以编辑个人资料、目标方向和目标年份，也可以退出并切换账户。

## 质量检查

```powershell
npm run lint
npm run typecheck
npm run contracts:check   # 前端手写的 interface 与后端契约是否还对得上
npm run build
npm run test:e2e
```

`test:e2e` 用的端口固定是 5173，和后端 `CORS_ORIGINS` 里列的那个一致 —— 换一个端口
的表现是"登录页一直不跳转"（见 `playwright.config.ts` 顶部）。**本地已经在跑
`npm run dev` 时它会直接复用那台服务器**（`reuseExistingServer`），此时不需要先
`npm run build`；CI 里没有现成的服务器，它会跑 `next start`，那才需要先构建。

Playwright 在 Windows 上会自动选择 Chrome 或 Edge；也可通过 `CHROME_PATH` 指定浏览器。

## 数据边界（2026-09-26 校准）

这一块此前写的是"当前账户系统只用于前端产品验证，不是正式身份认证"、"对话是 Mock
响应，尚未接入真实 AI"。**那两句话已经不成立**,留着它们会让读到的人照着过时的约束
做决定。现在的真实情况是:

- **后端已接入。** `src/lib/api.ts` 是统一的 API 客户端(`apiFetch` + bearer token),
  登录/注册走 `POST /api/auth/*`。token 存在 `localStorage`。
- **迁移尚未完成。** `src/features/growth/provider.tsx` 与 `workspaces.ts` 里仍有
  `localStorage` 状态,两条来源并存。哪些视图已经读后端、哪些还在读本地,`grep
  localStorage` 是最快的答案。
- **模型输出只是提案。** 计划变更要在界面上点击确认才落库。界面上有一个徽标显示这次
  回复是谁生成的(openJiuwen / 直连模型 / 本地规则),它来自响应的 `source` 字段 ——
  不是装饰。
- 空间文件只在浏览器会话中创建本地对象地址，不上传服务器。
- “树叶”是 `GrowthNode` 的界面称呼。

## 验证到什么程度（2026-09-26）

这一节此前写着"本文件描述的 Web 改动尚未经过任何构建或端到端验证"。那句话在当时
是真的,现在不是了 —— 但也不是"全都验过了"。分开说:

**有证据的:**

- `npm run typecheck`、`npm run lint`、`npm run contracts:check` 在 2026-09-26 全绿。
- **`npm run build` 通过**:打印出全部 12 条路由,写出 `.next/BUILD_ID`。`next dev`
  用的是 `.next-dev`(`next.config` 里的 `distDir`),所以构建和正在运行的 dev server
  不抢同一个目录 —— 构建之后 `/workbench` 仍然 200。
- **Playwright 30 条全绿**,连续四轮(每轮 1.1~1.3 分钟),打的是**真实后端**
  加本机真实的 `next dev`。其中 `tests/live-loop.spec.ts` 那两条最硬:在界面上建带
  工时的任务 → 排期 → 「今天」标记完成 → 刷新后那条记录还在,中间每一步都另取一遍
  `GET /api/...` 的真值来比,所以"只改内存的假保存"过不了它。
- 复盘 → 调整提案这条路径在真实浏览器里看到过:模型提出 5 项变更,卡片渲染出来,
  「先不要 / 确认，写入计划」两个按钮都在。**但"点确认之后计划真的更新了"这一步是在
  接口层验的**(`scripts/accept_stage8.py` 与 `backend/tests/test_proposal_confirm.py`),
  不是在这台浏览器里点出来的。

**没有证据的:**

- 只在这台机器的 Chrome/Edge 上跑过。其它浏览器没测。
- CI 那条路径没跑过:本地永远复用 5173 上的 `next dev`,而 CI 里要跑的是
  `next start`(构建产物)。
- 真实网络延迟、多用户并发的表现没有测过;PostgreSQL 只在本机 SQLite 上验过行为,
  没有对着真库跑过。

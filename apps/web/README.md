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
2. 注册/登录之后落到**成长空间列表**（`/spaces`）—— 一个刚注册的账户一个空间都没有，
   页面不会给他一份别人的计划。在列表里新建一个空间，填名称和「想在这里推进什么」。
3. 建完直接进工作台，画布上**只有这个空间的根目标**，没有别的节点。
4. 「新建节点」建子节点；双击节点进入它的子空间，成为下一层中心；面包屑或返回按钮回到上级。
5. 工作台有四个视图（路径 / 时间线 / 任务 / 排期），它们画的是**同一份** `/plan`。
6. 「我的」页面可以编辑个人资料、目标方向和目标年份，也可以退出并切换账户。

**下面这一条连同示例空间一起删掉了，别再照着写**：根路径展示"学业成绩、科研能力、
综合经历、个人成长"四个分类，以及"添加树叶"这个叫法。那四个分类是示例空间那份演示数据
里写死的（后端没有 `category` 这个字段），"树叶"是那棵树上的说法。现在用户自己建的
节点就叫**节点**。

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
- **演示空间已经整个删掉了。** `?workspace=primary` 那份存在浏览器里的保研演示数据
  （整棵树、四分类、四条预置对话、两篇随笔、写死的假回复和假「今天」观察）连同
  `src/mock/` 一起没有了。现在**只有一种空间**：后端的、属于这个账户的。
  `SpaceKind` 是 `'real' | 'none'`，`none` 是"还没打开任何空间"这个**正确的空状态**，
  不是"示例状态"。
- **`localStorage` 里还剩什么。** 只剩三样，都不是业务主存储：令牌
  （`src/lib/api.ts`）、当前选中的空间 id、画布上的节点位置与视口。**计划、对话、随笔
  的正本都在后端**。`grep localStorage` 仍然是最快的答案。
- **已知的内存态缺口（未修）**：随笔（`publishJournal`）与空间文件（`addFiles`）目前
  **只写前端内存**——后端还没有随笔表、随笔接口和资源上传接口。刷新之后新写的随笔
  会消失，附件也只是浏览器会话里的对象地址。`tests/experience.spec.ts` 里有一条写明
  原因的 `test.fixme` 钉住前者；`level-navigation.spec.ts` 只证明文件**在同一次会话内**
  按层归属，没有断言跨刷新还在。
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
- **`npm run test:accept` = 31 passed / 1 skipped / 0 failed**（提交 `1d6e30f`；跑的时候
  那份代码还没提交，工作区里那 38 项改动就是该提交的全部内容，跑完原样提交，
  之后 `git status --porcelain` 为空；2026-09-26 22:30 前后，`--workers=1`）。
  这是**正式验收**那条路：production
  构建 + 独立 `next start`（5273）+ 隔离测试后端（8100，临时 SQLite，**没有模型 key**），
  不复用 5173 上的 `next dev`、也不碰 `data/zhitu_dev.db`。连跑三轮同一结果，再换一个
  **全新库**跑一次也是同一结果。
  - 那 1 条 skipped 是 `experience.spec.ts` 里写明原因的 `test.fixme`（随笔只写内存）。
    **它不是通过。**
  - 没有模型 key 这件事本身被验到了：`workbench.spec.ts` 与 `conversation-hub.spec.ts`
    都在这条栈上发消息，断言的是**用户自己那条**被写入并在刷新后还在 —— 断言回复内容
    等于把"模型这次答得怎么样"混进"消息存没存下来"里。
  - 最硬的两条仍然是 `tests/live-loop.spec.ts`：在界面上建带工时的任务 → 排期 →
    「今天」标记完成 → 刷新后那条记录还在，每一步都另取一遍 `GET /api/...` 的真值来比，
    所以"只改内存的假保存"过不了它。
- **"Playwright 30 条全绿"是历史记录**（提交 `0719b90` 前后、示例空间还是默认入口时，
  在 `next dev` 上多 worker 跑的），不代表现在，也不代表这一版。它之后有一段更该被记住的
  实测：同一提交串行连跑五轮，失败数在 4~6 之间摆动（见 `docs/08-DEPLOYMENT.md` 第四节）。
  这一版把那六条连测试一起改掉了，才有上面那个 0。
- 复盘 → 调整提案这条路径在真实浏览器里看到过:模型提出 5 项变更,卡片渲染出来,
  「先不要 / 确认，写入计划」两个按钮都在。**但"点确认之后计划真的更新了"这一步是在
  接口层验的**(`scripts/accept_stage8.py` 与 `backend/tests/test_proposal_confirm.py`),
  不是在这台浏览器里点出来的。

**没有证据的:**

- 只在这台机器的 Chrome/Edge 上跑过。其它浏览器没测。
- CI 那条路径没跑过:本地的 `npm run test:e2e` 复用 5173 上的 `next dev`,而验收栈跑的是
  `next start`(构建产物)。**这两者结论不一样** —— 上面那个 0 只属于验收栈。
- 真实网络延迟、多用户并发的表现没有测过;PostgreSQL 只在本机 SQLite 上验过行为,
  没有对着真库跑过。
- 后端测试**全绿**：350 条全部通过（提交 `858c653`，2026-09-26 **22:53** 本机 ——
  这个钟点落在免打扰时段里）。上一版不是：348 条里 347 通过，红的 `test_reminders.py::test_user_returned_after_a_gap`
  在 22:00–08:00 之间必红 —— 那个用例拿真实时钟当输入，而"免打扰"是一条挂在钟点上的规则，
  于是它验的是"我们碰巧在几点跑的"。修法是给"此刻"加了一个可替换的依赖
  （`backend/api/dependencies/clock.py` 的 `get_now`），不是放宽断言、也不是让规则为测试
  让路；三条新断言都做过反向验证（改坏规则 → 变红 → 改回）。详见
  `docs/08-DEPLOYMENT.md` 第四节。这一条属于后端，和前端的 31/1/0 是两笔账。

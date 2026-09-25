# 知途 Web

知途当前正式 Web 入口，基于 Next.js、React 和 React Flow。该实现由 `prototypes/demo3` 验证后提升而来，保留组长版本的产品结构与交互，并统一为近黑成长画布、分层深灰表面和四类成长色。路径／时间线／任务三视图、浮动对话、随笔、Today 和个人中心均使用同一套深色视觉系统。

## 本地启动

需要 Node.js 22 或更新版本。

```powershell
cd apps/web
npm install
npm run dev
```

打开 <http://127.0.0.1:5173/workbench>。

## 当前交互

1. 未登录访问正式页面时进入登录页，可注册一个本地演示账户。
2. 注册信息会生成该账户自己的目标计划；不同账户的 Growth State、随笔、对话与偏好相互隔离。
3. 工作台根路径展示学业成绩、科研能力、综合经历、个人成长四个分类。
4. 单击分类进入它的专属路径，当前分类作为中心，已有内容作为下方树叶。
5. 点击“添加树叶”，填写名称、类型与说明。新树叶会同步进入任务和时间线使用的 Growth State。
6. 任意树叶可以通过右上角箭头或双击继续进入，成为下一层中心；通过面包屑或返回按钮回到上级。
7. “我的”页面可以编辑个人资料、目标方向和目标年份，也可以退出并切换账户。
8. 节点选择、时间线拖动、任务完成、空间文件和本地 Mock 对话继续沿用组长 Demo3 的行为。

深色主题集中在 `src/app/dark-theme.css`，React Flow 时间线的模块样式位于 `src/components/workbench/TimelineView.module.css`。修改颜色时优先调整这两处，避免在页面组件中散落硬编码颜色。

## 质量检查

```powershell
npm run lint
npm run typecheck
npm run build
npm run test:e2e
```

Playwright 在 Windows 上会自动选择 Chrome 或 Edge；也可通过 `CHROME_PATH` 指定浏览器。

## 数据边界

- 账户、个人资料和工作台状态按用户 ID 保存在浏览器 `localStorage`，刷新后保留。
- 当前账户系统只用于前端产品验证，不是正式身份认证；上线时需要由后端会话、密码存储和数据库替换。
- 空间文件只在本次浏览器会话中创建本地对象地址，不上传服务器。
- 对话是 Mock 响应，尚未接入真实 AI。
- “树叶”是 `GrowthNode` 的界面称呼，不是新的共享数据类型，本次未修改 `shared/schemas/`。
- 后端、持久化与模型接入需要在下一阶段按根目录设计文档继续实现。

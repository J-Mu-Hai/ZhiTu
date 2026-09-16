# Information Architecture

> 状态:由 [01-PRODUCT-DESIGN.md](01-PRODUCT-DESIGN.md) 的 Core Modules 推导而来,尚未定稿。

## 双端分工

| | Web(Think & Plan) | Mobile(Act & Sense) |
| --- | --- | --- |
| 回答的问题 | 我要去哪里,怎么去 | 我现在最应该做什么 |
| 主界面 | Growth Space | Today |
| 典型时长 | 长,坐下规划 | 短,随时打开 |
| 核心动作 | 对话、看 View、直接操作节点 | 执行、勾选、记录、被提醒 |

## 一级入口

### Web

| 入口 | 落点 |
| --- | --- |
| Growth Space(Graph / Timeline / Plan) | `apps/web/src/app/features/growth` |
| Conversation(AI Dock,非独立页面) | `apps/web/src/app/features/conversation` |
| Journal | `apps/web/src/app/features/journal` |
| Growth(长期报告) | 待定 —— 见下方问题 |
| Me | `apps/web/src/app/features/profile` |

### Mobile

| 入口 | 落点 |
| --- | --- |
| Today | `apps/mobile/lib/screens/today` |
| Plan | `apps/mobile/lib/screens/plan` |
| Conversation | `apps/mobile/lib/screens/conversation` |
| Journal | `apps/mobile/lib/screens/journal` |
| Me | `apps/mobile/lib/screens/profile` |

## 关键跳转

- Mobile 的 Today 通知 → 直接进入对应动作的执行反馈,不要先落到首页
- Web 的 Growth Space 选中节点 → Conversation 自动获得 `currentObject`
- Journal 的记录 → 可被提升为 Growth Event / Memory(需要用户可见)

## 待确认

- [ ] Web 是否有 Today?两端职责重叠到什么程度?
- [ ] Growth(长期报告)是 Web 一级入口,还是并入 Me?
- [ ] Mobile 是否需要完整 Graph?(见 [CURRENT-DESIGN.md](CURRENT-DESIGN.md) 未决问题)
- [ ] 导航层级:Graph / Timeline / Plan 之间怎么切换而不丢失上下文

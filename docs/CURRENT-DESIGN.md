# Current Design

Version: 0.4
Updated: 2026-09-25

> 本文是**每天真正要看的文档**,保持 100~200 行。
> 与编号文档冲突时,以本文为准。

## Current Product

大学生成长规划智能体。

## Current Core Loop

```
Conversation
     ↕
Growth Space
     ↕
  Execution
```

## Current Web Demo

已决定:

- React
- Next.js
- React Flow
- Graph / Timeline / Plan
- AI Dock
- 四个成长分类单击进入各自路径
- 子路径以当前节点为根，内容以“树叶”向右展开，按内容实测高度保留间距
- 树叶可填写名称、类型和说明,并可继续进入下一级空间
- Web 统一采用暖白画布、白色分层表面和四类成长色，覆盖路径、时间线、任务、对话与个人页面
- 品牌统一使用浅底蓝灰双叶芽矢量图，覆盖页签、手机主屏幕、导航、登录、加载与 AI 对话身份。节点标记固定在标题左侧，正文完整换行；根与子路径采用按实测高度留白的横向布局、共用出线通道与圆角连线，保留用户手动拖动位置。
  （`dark-theme.css` 的文件名是历史遗留：它的最后一层把变量覆盖成暖白，残留的深色规则全被压住）
- 登录与注册是正式 Web 入口；每个本地账户拥有独立的 Growth State 和个人资料
- 本地预览注册仅填写中国大陆手机号和密码；旧邮箱账号仍可登录。尚无短信核验或密码找回，不用于公开注册。兼容期 API 与数据库的 `email` 字段承载手机号或旧邮箱登录标识，不生成虚假邮箱、不迁移旧数据。登录注册页面统一暖白主题。
- “我的”支持编辑姓名、学校、专业、年级、排名、目标年份、目标方向和个人介绍

`prototypes/demo3` 已完成体验验证并提升为正式入口 `apps/web`。原 `apps/web` 骨架保存在 `prototypes/web-skeleton` 供架构对照。

当前边界(2026-09-26 校准):

- **后端身份、数据库与真实模型已接入。** 登录/注册走 `POST /api/auth/*`,凭据是不透明
  bearer token(库表 `auth_sessions`),数据落在 `backend/db/models/` 定义的整套表里,
  规划请求经 `backend/agent/runtime/` 调模型。此前这里写的是"尚未接入",那话在当时是
  真的,现在不是了 —— 留着一句过时的自我介绍,读到它的人会照着已经不存在的约束做决定。
- **Web 端正在从浏览器本地存储迁到后端 API。** 迁移**尚未完成**:`apps/web/src/features/`
  下仍有 localStorage 状态,`grep` 一下 `localStorage` 就能看到当前边界在哪。
- **模型输出只是提案。** 计划变更由用户在界面上点击确认后才落库;模型看不到真实 UUID,
  只见 `n1..nK` 句柄,由服务端映射回来。
- 走的是哪条推理路径会**如实写在响应的 `source` 里**(openjiuwen / direct_llm /
  rule_fallback / unavailable),界面据此显示徽标。

定位:Think & Plan

## Current Mobile

Flutter

定位:Act & Sense

## Current Backend

FastAPI

Agent:openJiuwen

## Current Data Model

**Status: Experimental**

目前:

```
Growth State
├── Node
├── Edge
├── Timeline
└── Execution
```

## Current Interaction

用户可以:

1. 单击学业成绩、科研能力、综合经历、个人成长进入专属路径
2. 围绕中心主题添加树叶并继续进入子路径
3. 对话修改计划
4. 直接修改计划

这些方式同等地位,都必须落到同一份 Growth State。

## 当前 Web 界面（2026-09-28）

- 成长空间使用紧凑的响应式卡片网格；空间数量不设界面上限，页面纵向滚动。
- 首页先展示来自真实排期场次的本周概览，再展示今天的执行记录与观察；周视图不虚构时钟时间或完成状态。
- 随笔使用“可收起输入 + 左侧列表 + 右侧全文阅读”的布局。后端随笔表尚未实现，界面明确提示本次会话限制。
- 工作台保留路径、时间线、任务、排期四个悬浮视图入口。AI Dock 缩窄为辅助区域；主工具条只保留撤销、重做和空间文件，新建节点、建关系与归档收进“更多”。
- 窄屏下空间网格、随笔双栏和 Today 双栏均退化为单栏；AI Dock 变为底部浮层。
- 统一动效与生命感规范见 [12-MOTION-AND-LIVENESS-SYSTEM.md](12-MOTION-AND-LIVENESS-SYSTEM.md)。普通状态保持安静，动画必须对应真实状态，并支持 reduced motion。

## Current Unresolved Questions

- [ ] Graph 是否允许一个节点属于多个 Goal?
- [ ] Edge 应该有哪些类型?
- [ ] AI 修改计划是否必须用户确认?
- [ ] Timeline 冲突如何表达?
- [ ] Mobile 是否需要完整 Graph?
- [ ] Growth Asset 最终定义是什么?
- [ ] Web 端是否也有 Today?(见 [02-INFORMATION-ARCHITECTURE.md](02-INFORMATION-ARCHITECTURE.md))

## Current Development Stage

Phase 1:**Product Exploration**

## Team Rule

```
Mother Demo
    ↓
Parallel Exploration
    ↓
  Integration
    ↓
 Next Version
```

**每 2~3 天 Integration。**

## 变更记录

| 日期 | 变更 | 原因 |
| --- | --- | --- |
| 2026-09-16 | 建立仓库骨架 | 项目启动 |
| 2026-09-16 | 确立核心模型:目标—规划—执行—感知—调整—成长沉淀 | 产品定义收敛 |
| 2026-09-24 | Demo3 提升为正式 Web；加入分类下钻与树叶路径 | 让四类成长维度可以独立、递归地丰富内容 |
| 2026-09-25 | Web 从白色视觉切换为完整深色工作台，并统一时间线与内容页 | 降低空旷感，强化节点、路径和内容层级 |
| 2026-09-25 | 恢复登录／注册入口，加入账户级计划隔离和可编辑个人资料 | 让产品从单人静态 Demo 进入多用户产品结构 |
| 2026-09-28 | 重整成长空间、首页、随笔和工作台布局，缩小 AI Dock 并保留四个计划视图 | 降低按钮密度，让真实内容成为页面视觉中心 |
| 2026-09-28 | 建立统一 Motion & Liveness System | 用克制、可解释的状态反馈赋予产品生命感，避免页面各自添加装饰动画 |

# 对话与节点管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让个人对话替代预置示例，支持对话改名与标签，并为路径节点提供可发现的递归删除能力。

**Architecture:** `Conversation` 增加可选标签，`ConversationHub` 根据是否存在个人对话决定是否展示预置内容，并通过现有账号工作区持久化编辑结果。节点删除作为 `PlanAction` 进入 `growthReducer`，递归删除节点和边；Provider 同步清理文件、位置与关联引用，`PathView` 只负责触发删除。

**Tech Stack:** Next.js 15、React 19、TypeScript、React Flow、Playwright。

---

### Task 1: 个人对话替换示例并支持标题与标签

**Files:**
- Modify: `apps/web/src/types/growth.ts`
- Modify: `apps/web/src/features/conversation/ConversationHub.tsx`
- Modify: `apps/web/src/app/dark-theme.css`
- Test: `apps/web/tests/conversation-management.spec.ts`

- [x] 写 Playwright 测试：首次进入显示四条示例，创建个人对话后只剩个人对话。
- [x] 运行测试，确认因示例仍显示而失败。
- [x] 给 `Conversation` 增加 `tags?: string[]`，创建对话时解析最多五个非空标签。
- [x] 新建个人对话时移除 `isExample` 对话，并在已有个人对话时隐藏独立的 `admission` 示例。
- [x] 增加“编辑对话”按钮和弹窗，保存后同步更新标题与标签。
- [x] 运行测试，确认创建、改名、标签和刷新持久化全部通过。

### Task 2: 递归删除成长节点

**Files:**
- Modify: `apps/web/src/types/growth.ts`
- Modify: `apps/web/src/features/growth/reducer.ts`
- Modify: `apps/web/src/features/growth/provider.tsx`
- Test: `apps/web/tests/node-delete.spec.ts`

- [x] 写 reducer 测试：删除父节点后，父节点、全部后代和相关边消失，无关节点保留。
- [x] 运行测试，确认 `DELETE_NODE` 尚未实现。
- [x] 增加 `DELETE_NODE` action，并用父子关系收集后代集合。
- [x] Provider 增加 `deleteNode`，同步清理选择状态、位置、文件、提案及对话/随笔的失效关联。
- [x] 运行 reducer 测试，确认递归删除通过。

### Task 3: 节点悬停删除入口与回归验证

**Files:**
- Modify: `apps/web/src/components/growth/PathView.tsx`
- Modify: `apps/web/src/app/experience.css`
- Modify: `apps/web/src/app/dark-theme.css`
- Test: `apps/web/tests/node-delete.spec.ts`

- [x] 写浏览器测试：新增父树叶和子树叶，返回父空间，悬停显示垃圾桶并点击删除。
- [x] 在非中心节点右上角添加 `Trash2` 按钮，阻止点击冒泡并调用 `deleteNode`。
- [x] 将“进入空间”按钮移到右下角，避免与删除按钮重叠。
- [x] 验证删除后节点从路径、任务和本地工作区中消失。
- [x] 运行 lint、typecheck、生产构建和完整 Playwright 回归测试。

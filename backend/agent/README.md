# backend/agent

Intelligence 层的落点。编排用 **openJiuwen**。

**这是唯一允许调用模型的地方** —— `backend/api/` 不得直接调用。

| 目录 | 放什么 |
| --- | --- |
| [agents/](agents/) | 角色实现。**MVP 先只做 Main Agent** |
| [workflows/](workflows/) | 编排、状态机、Replan 流程、重试策略 |
| [prompts/](prompts/) | 提示词模板,与代码分离,便于版本对比 |
| [tools/](tools/) | Agent 可调用的工具及其权限边界 |
| [memory/](memory/) | 短期会话记忆与长期成长记忆 |
| [schemas/](schemas/) | 模型输出的结构化校验,失败时的回退策略 |

设计与职责边界见 [docs/06-AGENT-DESIGN.md](../../docs/06-AGENT-DESIGN.md)。

## 输出契约

Agent 的输出是 `reply` + `PlanAction[]`,不是大段不可解析的计划文本。

`PlanAction` 的取值见 [docs/05-DATA-MODEL.md](../../docs/05-DATA-MODEL.md)。

## 关于 Multi-Agent

`agents/` 下预留了 `planner` / `coach` / `analyzer` 三个目录,**但不是现在就要实现三个 Agent**。

拆分时机:

- MVP 阶段 → 只有 Main Agent
- Planning 复杂度上来了 → 拆 `planner`
- 行为分析越来越复杂 → 拆 `analyzer`
- 主动陪伴逻辑独立了 → 拆 `coach`

不要为了"我们用了 Multi-Agent"强行 Multi-Agent。

## 待补全

- [ ] openJiuwen 的接入方式与部署形态
- [ ] 模型调用的统一封装(重试、超时、成本记录)
- [ ] 提示词的组织与版本管理
- [ ] 结构化输出校验失败时的回退路径
- [ ] 工具调用的权限模型

# backend/agent

Intelligence 层的落点。编排用 **openJiuwen**。

**这是唯一允许调用模型的地方** —— `backend/api/` 不得直接调用。

| 目录 | 放什么 |
| --- | --- |
| [runtime/](runtime/) | Reasoner 接缝与它的三个实现(openjiuwen / direct_llm / rule_fallback) |
| [prompts/](prompts/) | 提示词模板,与代码分离,便于版本对比 |

设计与职责边界见 [docs/06-AGENT-DESIGN.md](../../docs/06-AGENT-DESIGN.md)。

## 输出契约

Agent 的输出是 `reply` + 一份**提案**(计划变更的列表),不是大段不可解析的计划文本。

模型**只提案,不写入**。提案经 `backend/services/proposal_service.py` 校验、预览,由
用户在界面上点击确认后才落库 —— 这条不靠约定,靠结构:模型看不到也产不出真实 UUID,
只见 `n1..nK` 这样的句柄,服务端再映射回来。所以一个被提示注入的模型在物理上无法
指涉别的空间的节点。

## runtime/ 的三条路

`build_reasoner()` 按 `AGENT_REASONER` 选实现,细节见 [runtime/__init__.py](runtime/__init__.py):

| 取值 | 行为 |
| --- | --- |
| `rule` | 规则兜底,不问模型 |
| `direct` | 直连 DeepSeek,绕开 SDK |
| `openjiuwen` | openJiuwen 的 `Workflow`;它不可用时**降级并留日志** |
| `auto`(默认) | 装了 openJiuwen 就用它,否则直连;没 key 则规则兜底 |

**降级必须留痕。** 走了哪条路如实写在响应的 `source` 里,界面据此显示徽标
(「AI 规划 · openJiuwen」/「直连模型」/「本地规则 · 模型不可用」)。把直连说成
"AI 规划 · openJiuwen" 是撒谎,而这件事恰恰最容易在一次"SDK 版本不兼容"里悄悄发生。

`openjiuwen` 是可选依赖(见 [requirements-agent.txt](../requirements-agent.txt))。
未安装时后端照常启动,只是不走它。

## 硬约束

- `reason()` 对上游失败(超时 / 401 / 非 JSON / 字段缺失)**永不抛异常**,而是返回
  `degraded=True` 的结果。只有结构性 bug 才抛 —— 见 [runtime/base.py](runtime/base.py)。
- `import openjiuwen` 绝不在模块导入时执行(`importlib.util.find_spec` 判断 + 缓存),
  否则每次冷启动都要付几秒的注册开销,哪怕这次请求根本不走模型。
- 规则兜底**不编造计划内容**。它只做三件事:提出澄清问题、明确拒绝、或执行用户
  说出口的显式命令。

## 关于 Multi-Agent

MVP 只有一个 Reasoner 接缝,**不拆多个 Agent**。将来真要拆,拆分时机是:

- Planning 复杂度上来了 → 拆 `planner`
- 行为分析越来越复杂 → 拆 `analyzer`
- 主动陪伴逻辑独立了 → 拆 `coach`

不要为了"我们用了 Multi-Agent"强行 Multi-Agent。

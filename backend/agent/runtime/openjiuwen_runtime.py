"""openJiuwen 适配器 —— 用它的 `Workflow` 跑单步规划,用它的 `Model` 客户端调 DeepSeek。

## 这里的"使用 openJiuwen"具体指什么

不是"仓库里有一个 `import openjiuwen`"。是一次规划请求实际经过了 openJiuwen 的这几层:

- `Workflow(card=WorkflowCard(...))` + `Start` + `LLMComponent` + `End` 组成的那张图;
- `LLMCompConfig` 里的 `ModelClientConfig(client_provider=ProviderType.DeepSeek, ...)`
  —— **DeepSeek 是 openJiuwen 的一等 provider**,不是靠 OpenAI 兼容层绕过去的;
- `Runner.run_workflow(flow, inputs)` 执行的这张图;
- 模型输出由 openJiuwen 的 `OutputFormatter` 按我们声明的 `output_config` 抽成结构化字段。

响应里的 `source` 因此写成 `openjiuwen`,界面显示"AI 规划 · openJiuwen"。

## 三处刻意的设计决定

### 一、`import openjiuwen` 绝不在这层之外发生,而且必须是懒的

导入这个包会注册连接器池、文档解析器(milvus / chroma / PDF / Word / 图片……),
实测要几秒并连带拖起一堆重量级依赖。放在模块顶端意味着**每次冷启动**都付这个代价,
哪怕这次请求根本不走模型(比如读一次 `/plan`)。所以:

- `available()` 用 `importlib.util.find_spec` 判断(不执行导入),结果缓存;
- 真正的 `import` 发生在第一次调用模型时,发生在 `reason()` 内部。

### 二、提示词**不经过** `PromptTemplate` 的插值,而是原样写进组件配置

这是实测得出的结论,不是审美。`LLMCompConfig.template_content` 里的内容会被
`PromptTemplate` 按 `{{...}}` 插值,而我们的提示词里有大量 JSON 花括号(节点表、
`output_config` 的形状示例、用户自己写的 JSON 片段)。把那段文本塞进插值模板,
轻则被无声吃掉几个片段,重则把用户数据当成占位符展开 —— 两类错误都不会报错。

所以每一轮的 `LLMCompConfig` 是按这一轮的提示词**现构造**的,里面是字面量。
代价是每次调用新建一个组件对象,但 `ModelClientConfig.use_shared_llm_http_client`
默认为真是进程级的 —— 连接池没有被丢掉的,丢掉的只是一个数据类实例。

### 三、`output_config` 声明 `brief` / `actions` 为 `required: False`

openJiuwen 的 `_extract_configured_fields` 在字段缺失时看的是**原始 dict 里有没有写
`required`**(`field_config.get("required", True)`),不写就是必填。而"模型这轮只是
回答了一句话,没有提任何变更"是完全正常的一种回复 —— 把它当失败,用户会在每次
闲聊之后看到一次"模型输出格式不对"。所以那两个字段显式写 `False`。

## 失败一律降级,不抛

`Reasoner` 的硬契约(见 `base.py`):网络超时、401、SDK 内部报错、输出不是 JSON ——
全是可预期的上游失败,返回 `degraded=True`。这里因此把**所有** `Exception` 都收住,
只留 `DegradedReason` 去区分"用户该看到什么 + 重试有没有用"。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import time
import uuid
from typing import Any

from backend.agent.prompts.goal_reasoning import (
    GOAL_REASONING_PROMPT_VERSION,
    GOAL_REASONING_SYSTEM_PROMPT,
    STRATEGIC_INTAKE_PROMPT_VERSION,
    STRATEGIC_INTAKE_SYSTEM_PROMPT,
)
from backend.agent.prompts.planning import PROMPT_VERSION, SYSTEM_PROMPT
from backend.agent.prompts.v1_strategy import (
    V1_STRATEGY_PROMPT_VERSION,
    V1_STRATEGY_SYSTEM_PROMPT,
)
from backend.agent.prompts.v1_strategy_synthesis import (
    V1_STRATEGY_SYNTHESIS_PROMPT_VERSION,
    V1_STRATEGY_SYNTHESIS_SYSTEM_PROMPT,
)
from backend.agent.prompts.v1_timeline_alignment import (
    V1_TIMELINE_ALIGNMENT_PROMPT_VERSION,
    V1_TIMELINE_ALIGNMENT_SYSTEM_PROMPT,
)
from backend.agent.prompts.v1_timeline_repair import (
    V1_TIMELINE_REPAIR_PROMPT_VERSION,
    V1_TIMELINE_REPAIR_SYSTEM_PROMPT,
)
from backend.agent.prompts.v1_weekly_plan import (
    PROMPT_VERSION as V1_WEEKLY_PLAN_PROMPT_VERSION,
    V1_WEEKLY_PLAN_SYSTEM_PROMPT,
)
from backend.agent.runtime.base import ReasoningResult, TurnContext
from backend.agent.runtime.response import (
    ANALYSIS_FIELD_ORDER,
    BRIEF_FIELD_ORDER,
    PayloadInvalid,
    payload_to_result,
    render_turn,
)
from backend.core.config import Settings
from backend.db.models.enums import DegradedReason, ModelSource

logger = logging.getLogger(__name__)

# OpenJiuwen 0.1.x 的 JSON formatter 对配置为 string 的字段不接受 JSON null。
# 这条指令追加在 SDK 自动生成的 schema 提示中，覆盖所有回合而不只靠某一份业务提示词。
_STRICT_JSON_INSTRUCTION = (
    "Return valid JSON only, conforming to the schema below. "
    "Never emit JSON null: omit optional text fields or use an empty string \"\"; "
    "use [] for optional arrays. "
    "Schema: ${json_schema}.\nQuestion: ${query}."
)

#: 这张图的 id。openJiuwen 用 (id, version) 生成工作流的注册键。
WORKFLOW_ID = "zhitu_planning"
WORKFLOW_VERSION = "1.0"

#: brief 里每个字段长什么样。写进 description 而**不是**声明成嵌套的 `properties`,
#: 原因是 SDK 的校验器只认六种具体类型(object / array / string / integer / boolean /
#: number,**没有 any**,也不支持联合类型):`value` 在这六个字段里分别是字符串、
#: ISO 日期字符串、整数分钟、字符串列表,声明其中任何一个都会把另外几个判成非法,
#: 模型一次笔误就整轮失败。所以形状靠文字说清楚,类型校验只到"是个对象"为止,
#: 真正的取值收敛仍然由 `response.py` 的 `clean_value` 负责(它才认识每个字段的语义)。
_BRIEF_CLAIM_SHAPE = "形如 {value, source},source 取 user_stated 或 model_assumed"

#: brief 六个字段各自给模型看的说明。
_BRIEF_FIELD_DESCRIPTIONS: dict[str, str] = {
    "goal": f"用户想达成的目标。{_BRIEF_CLAIM_SHAPE}",
    "deadline": f"截止日期,ISO 格式 YYYY-MM-DD。{_BRIEF_CLAIM_SHAPE}",
    "weekly_available_minutes": f"每周可投入的分钟数(整数,6 小时写 360)。{_BRIEF_CLAIM_SHAPE}",
    "current_level": f"用户现在的基础。{_BRIEF_CLAIM_SHAPE}",
    "success_criteria": f"怎样算成功。{_BRIEF_CLAIM_SHAPE}",
    "constraints": f"现实限制,value 是字符串列表。{_BRIEF_CLAIM_SHAPE}",
}

#: analysis 七栏各自给模型看的说明。与提示词里那一段说的是同一件事,但**这张声明
#: 才是 SDK 真正会拿去约束输出的那一边** —— 只写在提示词里的话,栏名写错了模型
#: 也照给,然后被 pop 掉。
_ANALYSIS_FIELD_DESCRIPTIONS: dict[str, str] = {
    "known": "这一轮真的读到的事实(带节点记号)",
    "unknowns": "还缺什么才能判断",
    "evidence": "用户给的依据,必须带来源与日期",
    "assumptions": "没有依据时替用户假设了什么",
    "diagnosis": "判断与推理",
    "strategy_options": "可选的走法,每种说清代价",
    "risks": "可能出问题的地方(不写'已经排好日程')",
}

#: `analysis` 里**不是数组**的那两个键。它们单独列出来,因为在上面那张
#: `properties` 表里它们不是 `array` 而是标量 —— 而那张表是**由 `ANALYSIS_FIELD_ORDER`
#: 现推的**,数组的假设写在循环里。
#:
#: `narrative` 就是 §2.2 的正文。**它必须在这里**,理由和整个 `analysis` 键必须
#: 在 `OUTPUT_CONFIG` 里是同一条(见下面的长注释):没列进 `properties` 的键会被
#: `_extract_configured_fields` pop 掉,而 openJiuwen 是装了 SDK 时的默认路径 ——
#: 于是模型认真写的正文会在落库前消失,不报错、不打日志。
_ANALYSIS_SCALAR_DESCRIPTIONS: dict[str, str] = {
    "confidence_note": "一句话的可信度说明,不是分数",
    "narrative": (
        "这次判断的正文:完整讲清你怎么想的,可以很长(上限 20000 字)。"
        "七栏是摘要,这里才是推理本身"
    ),
}

# 模型在尚未掌握某项信息时，偶尔会用 JSON ``null`` 表达“未知”。这些字段都是
# 可选的说明文字；若让 SDK 把一个空值升级成整轮执行异常，用户就会看到“模型不可用”。
# 因此嵌套文本字段显式接受 null，后续解析器再把它归一为空字符串。
_NULLABLE_STRING_SCHEMA = {"type": ["string", "null"]}


def _enable_sdk_nullable_string_schema(llm_comp_module: Any) -> None:
    """Teach openJiuwen 0.1.x about the JSON-Schema union used above.

    Its built-in validator accepts only one type name even though its schema
    generator faithfully emits ``[\"string\", \"null\"]``.  This narrowly scoped
    compatibility shim applies only to that union, leaving every other SDK
    validation rule unchanged.
    """
    validator = llm_comp_module.ValidationUtils
    if getattr(validator, "_zhitu_nullable_string_enabled", False):
        return

    original_validate_type = validator.validate_type

    def validate_type(instance: Any, expected_type: Any) -> None:
        if expected_type == ["string", "null"]:
            if instance is None or isinstance(instance, str):
                return
            validator.raise_invalid_params_error(
                error_msg=f"expected type string or null but got {type(instance)}"
            )
        original_validate_type(instance, expected_type)

    validator.validate_type = staticmethod(validate_type)
    validator._zhitu_nullable_string_enabled = True

#: 给模型看的输出形状。**它同时是给 SDK 的抽取声明**:
#: 只有在这里列出的键会被带回来,模型多写的字段由 SDK 丢掉,
#: 少写 `brief` / `actions` 不算失败(`required: False`,理由见模块开头)。
#:
#: brief 下面那六个 `properties` **不是为了校验,是为了保住数据**。
#: `OutputFormatter._extract_configured_fields` 会把任何 dict 型已声明字段里
#: 没列进 `properties` 的键逐个 pop 掉(只 pop 一层)。这里不列,模型辛苦读出来的
#: 条件就会在落库前被清成 `{}`:实测模型原文里有完整的 goal / deadline /
#: success_criteria,拿到的却是空对象,于是 scheduler 拿默认容量档案排了 46 场,
#: 而不是用户亲口说的每周 6 小时 —— 那正好违反"不许替用户决定每周能投入多少"。
#:
#: 六个字段都**不设 `required`**:条件是分几轮问出来的,只有一条 goal 时也必须能存下来。
#: 一旦设了 required,模型早问一句就会被判失败,整轮回复陪葬。
OUTPUT_CONFIG: dict[str, Any] = {
    "reply": {"type": "string", "required": True, "description": "给用户看的那句话"},
    "brief": {
        "type": "object",
        "required": False,
        "description": "从对话里读出的规划条件",
        "properties": {
            field: {"type": "object", "description": _BRIEF_FIELD_DESCRIPTIONS[field]}
            for field in BRIEF_FIELD_ORDER
        },
    },
    # actions **刻意不声明 `items`**:声明了 SDK 就会逐条严格校验,一条不合格
    # 就让整轮失败。而这里的取舍相反 —— 十条里有一条不合法,应该留下另外九条,
    # 把那条的错误码单独报给用户(见 `response.py` 的 `parse_actions`)。
    "actions": {"type": "array", "required": False, "description": "提议的计划变更"},
    # questions 与 actions 同样的取舍:只声明成一个数组,**不声明 `items`** ——
    # 一道题写坏不该让整轮的回复都失败。逐题校验在 `response.parse_questions`,
    # 它是与 SDK 无关的那一层,直连与脚本化两条路都走它。
    "questions": {
        "type": "array",
        "required": False,
        "description": "向用户提的问题(0–2 个),与 actions 分开",
    },
    # 有界循环的协议键。**必须声明** —— 否则 openJiuwen 那条路会在到解析器之前
    # 把它们 pop 掉,模型请求的工具永远执行不了。
    "toolRequests": {
        "type": "array",
        "required": False,
        "description": "请求调用的只读工具(0–2 个)",
    },
    "stopReason": {
        "type": "string",
        "required": False,
        "description": "ready_to_propose / need_user_answer / insufficient_evidence / budget_exhausted / failed",
    },
    # analysis 同 `brief`:七栏里没列进 `properties` 的键会被 SDK pop 掉。
    #
    # **它必须在这里。** 这一条是真实模型验收抓出来的:提示词从 C 批起就要求模型给
    # `analysis`、`response.py` 也一直在解析它,唯独这份声明漏了 —— 而 `reason()` 那条
    # 路拿到的载荷是**由这张声明重建**的,不在声明里的键根本到不了解析器。于是模型
    # 每一轮都认真给了七栏判断,`node_analyses` 却一行都没有,而且全程没有任何异常
    # 或日志。直连那条路是直接把模型原文交给解析器的,所以只有 openJiuwen 这条路会犯,
    # 而这恰好是装了 SDK 时的**默认**路径。
    #
    # 七个数组都**不设 `required`**(理由同 brief),`analysis` 整键也是可选的:
    # "这一轮只是打招呼"是正常情况,不能被判成失败。
    #
    # **不只有数组。** `confidence_note` 与 `narrative` 是标量,它们同样必须逐个列出
    # —— 这条曾经漏过一次:`confidence_note` 从 C 批起就被 `parse_analysis` 接受,
    # 却一直不在这张表里,于是 openJiuwen 那条路上它每次都被 pop 掉,而直连那条路
    # 一切正常。所以那张 `_ANALYSIS_SCALAR_DESCRIPTIONS` 不是装饰:它是这段声明的
    # 必要部分,漏一个键就是静默丢一份数据。
    "analysis": {
        "type": "object",
        "required": False,
        "description": "自己对这块内容的判断,没有实质判断时整个键都不要给",
        "properties": {
            **{
                field: {
                    "type": "array",
                    "description": _ANALYSIS_FIELD_DESCRIPTIONS[field],
                }
                for field in ANALYSIS_FIELD_ORDER
            },
            **{
                field: {**_NULLABLE_STRING_SCHEMA, "description": description}
                for field, description in _ANALYSIS_SCALAR_DESCRIPTIONS.items()
            },
        },
    },
    # 目标推理回合的地图操作。**必须声明** —— 与 analysis 同一条理由:openJiuwen
    # 那条路拿到的对象是照这份声明重建的,不在这里的键会在到解析器之前被 pop 掉。
    # nodes / links 只声明成数组,不声明 items:一条写坏不该让整轮失败,逐条校验
    # 在无关 SDK 的 `response.parse_reasoning_map` 里做。
    "reasoningMap": {
        "type": "object",
        "required": False,
        "description": "目标推理地图操作(仅目标推理回合使用)",
        "properties": {
            "phase": _NULLABLE_STRING_SCHEMA,
            "turnAction": _NULLABLE_STRING_SCHEMA,
            "focus": _NULLABLE_STRING_SCHEMA,
            "focusReason": _NULLABLE_STRING_SCHEMA,
            "nodes": {"type": "array"},
            "links": {"type": "array"},
        },
    },
    # 阶段 12:战略 intake 决策。**必须声明** —— 同 reasoningMap:openJiuwen 那条路
    # 是照这份声明重建对象的,不在声明里的键会到不了解析器。
    "intakeDecision": {
        "type": "object",
        "required": False,
        "description": "战略 intake 决策(仅 strategic_intake 回合使用)",
        "properties": {
            "action": _NULLABLE_STRING_SCHEMA,
            "question": _NULLABLE_STRING_SCHEMA,
            "decisionScope": _NULLABLE_STRING_SCHEMA,
            "whyThisMatters": _NULLABLE_STRING_SCHEMA,
            "quickReplies": {"type": "array"},
        },
    },
    # 规划智能体重构 V1(P2):战略判断回合。**必须声明** —— 与上面两组同一条理由:
    # openJiuwen 那条路拿到的对象是照这份声明重建的,不在声明里的键会到不了解析器。
    # nodeUpdates 只声明成数组,不声明 items:一条写坏不该让整轮失败,逐条校验在
    # 与 SDK 无关的 `response.parse_v1_assessment` 里做。
    "v1Assessment": {
        "type": "object",
        "required": False,
        "description": "V1 战略判断(仅 v1_strategy 回合使用)",
        "properties": {
            "globalAssessment": _NULLABLE_STRING_SCHEMA,
            "strategicThesis": _NULLABLE_STRING_SCHEMA,
            "userUnderstanding": _NULLABLE_STRING_SCHEMA,
            "questionExample": _NULLABLE_STRING_SCHEMA,
            "keyDimensions": {"type": "array"},
            "nodeUpdates": {"type": "array"},
            "responseMode": _NULLABLE_STRING_SCHEMA,
            "criticalQuestion": _NULLABLE_STRING_SCHEMA,
            # R2:候选方向必须带解释一起出现。
            "decisionContext": _NULLABLE_STRING_SCHEMA,
            "provisionalRecommendation": _NULLABLE_STRING_SCHEMA,
            "optionImpact": {"type": "array"},
            "candidateDirections": {"type": "array"},
            "focusKey": _NULLABLE_STRING_SCHEMA,
            "focusReason": _NULLABLE_STRING_SCHEMA,
            "question": _NULLABLE_STRING_SCHEMA,
            "strategyTradeoff": _NULLABLE_STRING_SCHEMA,
            "strategyReady": {"type": "boolean"},
        },
    },
    # 规划智能体重构 V1(P3):粗时间架构回合。**必须声明** —— 同上面几组:
    # openJiuwen 那条路照声明重建对象,不在声明里的键会到不了解析器。
    "v1Timeline": {
        "type": "object",
        "required": False,
        "description": "粗时间架构(仅 v1_timeline 回合使用)",
        "properties": {
            "summary": _NULLABLE_STRING_SCHEMA,
            "phases": {"type": "array"},
        },
    },
    # 规划智能体重构 V1:时间架构共创回合。必须声明 —— 与上面几组同一条理由。
    "v1TimelineAlignment": {
        "type": "object",
        "required": False,
        "description": "时间假设 + 至多一个战略级问题(仅 v1_timeline_alignment 回合使用)",
        "properties": {
            "summary": _NULLABLE_STRING_SCHEMA,
            "totalSpan": _NULLABLE_STRING_SCHEMA,
            "cadence": _NULLABLE_STRING_SCHEMA,
            "phaseCount": {"type": "integer"},
            "biggestRisk": _NULLABLE_STRING_SCHEMA,
            "assumptions": {"type": "array"},
            "question": _NULLABLE_STRING_SCHEMA,
            "options": {"type": "array"},
        },
    },
    # 规划智能体重构 V1:战略合成回合的**窄契约**。必须声明 —— 与上面几组同一条
    # 理由:openJiuwen 那条路照声明重建对象,不在声明里的键会到不了解析器。
    # 只声明这四个结构 + tradeoff:输出面越窄,真实模型的结构化合规率越高。
    "v1Strategy": {
        "type": "object",
        "required": False,
        "description": "四条战略结构 + 取舍(仅 v1_strategy_synthesis 回合使用)",
        "properties": {
            "mainLine": {**_NULLABLE_STRING_SCHEMA, "description": "主线:最优先投入什么"},
            "parallelLine": {**_NULLABLE_STRING_SCHEMA, "description": "并行线:可同时做但不挤占主线"},
            "deferOrAvoid": {**_NULLABLE_STRING_SCHEMA, "description": "暂缓/放弃:当前不值得做什么"},
            "riskControl": {**_NULLABLE_STRING_SCHEMA, "description": "风险控制:检查点或备用路径"},
            "tradeoff": {**_NULLABLE_STRING_SCHEMA, "description": "这版战略的取舍(可空)"},
        },
    },
    "v1WeeklyPlan": {
        "type": "object",
        "required": False,
        "description": "阶段三周任务明细(仅 v1_weekly_plan 回合使用)",
        "properties": {"summary": _NULLABLE_STRING_SCHEMA, "tasks": {"type": "array"}},
    },
}

def component_outputs_schema() -> dict[str, str]:
    """LLM 组件自己的输出引用表:声明过的每个键各取一份。

    `${reply}` 是 openJiuwen 的引用语法,指的是**这个组件自己**输出字典里的那个键。
    少了它,`End` 收到的是 None,而 `WorkflowOutput.result` 会安静地变成 None ——
    一次成功的模型调用就此消失,日志里什么异常都没有。

    **由 `OUTPUT_CONFIG` 现推,不手抄。** 这两处以前是逐键写死的,于是 C 批给提示词
    与解析器都加上了 `analysis`,却没人想起这里 —— 模型给的七栏判断在组件出口就被
    丢掉,`payload_to_result` 读到的永远是没有那个键的对象,`node_analyses` 一行都
    没有。它不报错、不打日志,只是安静地少一块。

    单列成函数是为了能在**不导入 SDK** 的前提下被测试 —— 见
    `tests/test_openjiuwen_adapter.py`(那个文件刻意不 import openjiuwen)。
    """
    return {field: f"${{{field}}}" for field in OUTPUT_CONFIG}


def end_inputs_schema() -> dict[str, str]:
    """`End` 节点的输入引用表:把组件输出里的每个键都交给它。同样由声明现推。"""
    return {field: f"${{planning.{field}}}" for field in OUTPUT_CONFIG}


#: 进程级的 Runner 启动状态。`Runner.resource_mgr` 是全局的,按请求重复 start
#: 是生命周期 bug;而 `reason()` 可能在任何时候被第一个调用,所以这里自己做一次
#: 幂等的启动,不要求调用方先 warm_up 过。
_runner_lock = asyncio.Lock()
_runner_started = False

#: `find_spec` 的结果缓存。None 表示还没查过。
_available: bool | None = None


def available() -> bool:
    """openJiuwen 装没装。**只查 spec,不执行导入。**

    这个判断要便宜到可以在每次装配 reasoner 时调用 —— 它决定界面上那个徽标写
    "AI 规划 · openJiuwen" 还是"直连模型"。为了它拖起几秒的导入不值得。
    """
    global _available
    if _available is None:
        try:
            _available = importlib.util.find_spec("openjiuwen") is not None
        except (ImportError, ValueError):
            # `find_spec` 在父包损坏时抛 ImportError,在被当作命名空间包的一部分时抛
            # ValueError。两者都意味着"用不了",不是"程序错了"。
            _available = False
    return _available


def reset_availability_cache() -> None:
    """把 `available()` 的缓存丢掉。测试用 —— 生产代码里没有理由调用它。"""
    global _available
    _available = None


async def warm_up() -> bool:
    """把 Runner 与 openJiuwen 的导入开销前置到启动阶段。

    返回是否真的可用。**失败不抛**:openJiuwen 起不来是一件"这条路暂时走不通"的
    事,不是"服务不能启动"的事 —— 产品还有直连那条路。
    """
    if not available():
        return False
    try:
        module = importlib.import_module("openjiuwen.core.runner.runner")
    except Exception:
        logger.exception("openJiuwen 可用但导入失败,将不使用它")
        return False
    try:
        await _ensure_runner_started(module.Runner)
    except Exception:
        logger.exception("openJiuwen Runner 启动失败,将不使用它")
        return False
    return True


async def _ensure_runner_started(runner: Any) -> None:
    global _runner_started
    if _runner_started:
        return
    async with _runner_lock:
        if _runner_started:
            return
        await runner.start()
        _runner_started = True


def reset_runner_state() -> None:
    """把"Runner 已启动"这个标记清掉。测试用。"""
    global _runner_started
    _runner_started = False


class OpenJiuwenReasoner:
    """把一次规划请求交给 openJiuwen 的 Workflow 执行。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def reason(self, turn: TurnContext) -> ReasoningResult:
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        prompt_version = _prompt_version(turn)

        if not self._settings.llm_api_key:
            return self._degraded(
                DegradedReason.NO_API_KEY,
                retryable=False,
                request_id=request_id,
                reply="还没有配置模型密钥,现在没法生成计划。",
                prompt_version=prompt_version,
            )

        if not available():
            # 装配时查过一次,这里再查是为了"装配之后环境变了"这种情况 ——
            # `available()` 是缓存过的,所以它不花钱。
            return self._degraded(
                DegradedReason.MODEL_UNAVAILABLE,
                retryable=False,
                request_id=request_id,
                reply="openJiuwen 运行时不可用,这次没能生成计划。",
                prompt_version=prompt_version,
            )

        try:
            flow, runner = self._build_flow(turn)
            await _ensure_runner_started(runner)
            output = await runner.run_workflow(flow, {})
        except Exception as exc:
            return self._failed(
                exc, request_id=request_id, started=started, prompt_version=prompt_version
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        return self._parse(
            output, request_id=request_id, latency_ms=latency_ms, prompt_version=prompt_version
        )

    # ---------------------------------------------------------------------------
    def _build_flow(self, turn: TurnContext) -> tuple[Any, Any]:
        """按这一轮的提示词现搭这张图,返回 (flow, Runner)。

        openJiuwen 的导入就发生在这里 —— 见模块开头关于懒加载的说明。
        """
        workflow_module = importlib.import_module("openjiuwen.core.workflow")
        llm_comp_module = importlib.import_module(
            "openjiuwen.core.workflow.components.llm.llm_comp"
        )
        runner_module = importlib.import_module("openjiuwen.core.runner.runner")
        llm_module = importlib.import_module("openjiuwen.core.foundation.llm")
        _enable_sdk_nullable_string_schema(llm_comp_module)

        settings = self._settings
        config = llm_comp_module.LLMCompConfig(
            model_client_config=llm_module.ModelClientConfig(
                # DeepSeek 是 openJiuwen 的一等 provider,不需要借 OpenAI 兼容层。
                client_provider="DeepSeek",
                api_key=settings.llm_api_key,
                api_base=settings.llm_base_url,
                timeout=settings.llm_timeout_seconds,
                verify_ssl=True,
            ),
            model_config=llm_module.ModelRequestConfig(
                model_name=settings.llm_model,
                temperature=0.4,
                max_tokens=settings.agent_max_tokens,
            ),
            # 字面量,不是插值模板 —— 见模块开头的第二处设计决定。
            template_content=[
                {"role": "system", "content": _system_prompt(turn)},
                {"role": "user", "content": render_turn(turn)},
            ],
            response_format={"type": "json", "jsonInstruction": _STRICT_JSON_INSTRUCTION},
            output_config=OUTPUT_CONFIG,
        )

        flow = workflow_module.Workflow(
            card=workflow_module.WorkflowCard(
                id=WORKFLOW_ID, name="知途规划", version=WORKFLOW_VERSION
            )
        )
        flow.set_start_comp("start", workflow_module.Start())
        flow.add_workflow_comp(
            "planning",
            workflow_module.LLMComponent(config),
            # 两张表都由 `OUTPUT_CONFIG` 现推,理由见 `component_outputs_schema`。
            outputs_schema=component_outputs_schema(),
        )
        flow.set_end_comp(
            "end",
            workflow_module.End(),
            inputs_schema=end_inputs_schema(),
        )
        flow.add_connection("start", "planning")
        flow.add_connection("planning", "end")
        return flow, runner_module.Runner

    def _parse(
        self,
        output: Any,
        *,
        request_id: str,
        latency_ms: int,
        prompt_version: str = PROMPT_VERSION,
    ) -> ReasoningResult:
        """从 `WorkflowOutput` 里取出模型那个对象。"""
        try:
            payload = _payload_of(output)
            return payload_to_result(
                payload,
                source=ModelSource.OPENJIUWEN,
                request_id=request_id,
                prompt_version=prompt_version,
                model_name=self._settings.llm_model,
                latency_ms=latency_ms,
            )
        except PayloadInvalid as exc:
            logger.warning("openJiuwen 那条路的输出无法解析: %s", exc)
            return self._degraded(
                DegradedReason.MODEL_OUTPUT_INVALID,
                retryable=True,
                request_id=request_id,
                reply="模型这次的回答没能解析出结果,可以再试一次。",
                latency_ms=latency_ms,
                prompt_version=prompt_version,
            )

    def _failed(
        self,
        exc: Exception,
        *,
        request_id: str,
        started: float | None,
        prompt_version: str = PROMPT_VERSION,
    ) -> ReasoningResult:
        """把 SDK 抛出来的东西翻译成"用户该看到什么 + 重试有没有用"。

        openJiuwen 把上游的 401 / 429 / 超时都包在它自己的 `BaseError` 里,类型分不出来,
        所以这里按**文本**认一认常见的几种。认不出来就按"暂时不可用"归类 ——
        宁可让用户点一次注定失败的重试,也不要把一次超时说成"密钥错了"。
        """
        text = f"{type(exc).__name__}: {exc}"
        lowered = text.lower()
        logger.warning("openJiuwen 调用失败: %s", text[:500])

        if "401" in lowered or "unauthorized" in lowered or "api key" in lowered:
            reason, retryable = DegradedReason.MODEL_AUTH_FAILED, False
            reply = "模型密钥无效或已过期,请联系管理员。"
        elif "429" in lowered or "rate limit" in lowered:
            reason, retryable = DegradedReason.MODEL_RATE_LIMITED, True
            reply = "模型服务限流了,过一会儿再试。"
        elif "timeout" in lowered or "timed out" in lowered:
            reason, retryable = DegradedReason.MODEL_TIMEOUT, True
            reply = "这次响应太慢了,没能拿到结果。可以再试一次。"
        else:
            reason, retryable = DegradedReason.MODEL_UNAVAILABLE, True
            reply = "模型服务暂时不可用,可以再试一次。"

        return self._degraded(
            reason,
            retryable=retryable,
            request_id=request_id,
            reply=reply,
            started=started,
            prompt_version=prompt_version,
        )

    def _degraded(
        self,
        reason: DegradedReason,
        *,
        retryable: bool,
        request_id: str,
        reply: str,
        started: float | None = None,
        latency_ms: int | None = None,
        prompt_version: str = PROMPT_VERSION,
    ) -> ReasoningResult:
        if latency_ms is None:
            latency_ms = int((time.monotonic() - started) * 1000) if started else None
        return ReasoningResult(
            reply=reply,
            source=ModelSource.UNAVAILABLE,
            degraded=True,
            degraded_reason=reason,
            retryable=retryable,
            request_id=request_id,
            prompt_version=prompt_version,
            model_name=self._settings.llm_model,
            latency_ms=latency_ms,
        )


def _system_prompt(turn: TurnContext) -> str:
    if turn.purpose == "strategic_intake":
        return STRATEGIC_INTAKE_SYSTEM_PROMPT
    if turn.purpose == "goal_reasoning":
        return GOAL_REASONING_SYSTEM_PROMPT
    # 阶段一“想清楚”的战略判断回合。它的行为规则(先给判断再决定要不要问)在
    # `V1_STRATEGY_SYSTEM_PROMPT` 里;漏了这条会落到默认的 planning 提示词。
    if turn.purpose == "v1_strategy":
        return V1_STRATEGY_SYSTEM_PROMPT
    if turn.purpose == "v1_strategy_synthesis":
        return V1_STRATEGY_SYNTHESIS_SYSTEM_PROMPT
    if turn.purpose == "v1_timeline_repair":
        return V1_TIMELINE_REPAIR_SYSTEM_PROMPT
    if turn.purpose == "v1_timeline_alignment":
        return V1_TIMELINE_ALIGNMENT_SYSTEM_PROMPT
    if turn.purpose == "v1_weekly_plan":
        return V1_WEEKLY_PLAN_SYSTEM_PROMPT
    return SYSTEM_PROMPT


def _prompt_version(turn: TurnContext) -> str:
    if turn.purpose == "strategic_intake":
        return STRATEGIC_INTAKE_PROMPT_VERSION
    if turn.purpose == "goal_reasoning":
        return GOAL_REASONING_PROMPT_VERSION
    if turn.purpose == "v1_strategy":
        return V1_STRATEGY_PROMPT_VERSION
    if turn.purpose == "v1_strategy_synthesis":
        return V1_STRATEGY_SYNTHESIS_PROMPT_VERSION
    if turn.purpose == "v1_timeline_repair":
        return V1_TIMELINE_REPAIR_PROMPT_VERSION
    if turn.purpose == "v1_timeline_alignment":
        return V1_TIMELINE_ALIGNMENT_PROMPT_VERSION
    if turn.purpose == "v1_weekly_plan":
        return V1_WEEKLY_PLAN_PROMPT_VERSION
    return PROMPT_VERSION


def _payload_of(output: Any) -> Any:
    """把 `WorkflowOutput` 剥到模型那个对象上。

    实测的形状是 `WorkflowOutput(result={"output": {...}}, state=...)` —— 外面那层是
    `End` 组件包的(它把收到的输入裹进一个 `output` 键)。这里逐层剥,并且**每一层都
    容错**:SDK 的小版本换个包装方式,不该让整次调用变成"格式错误"。
    """
    result = getattr(output, "result", output)
    if not isinstance(result, dict):
        raise PayloadInvalid(f"工作流的输出不是一个对象: {type(result).__name__}")

    inner = result.get("output", result)
    if not isinstance(inner, dict):
        raise PayloadInvalid(f"工作流输出的 output 不是一个对象: {type(inner).__name__}")
    return inner


__all__ = [
    "OUTPUT_CONFIG",
    "WORKFLOW_ID",
    "OpenJiuwenReasoner",
    "available",
    "reset_availability_cache",
    "reset_runner_state",
    "warm_up",
]

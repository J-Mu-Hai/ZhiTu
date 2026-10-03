"""模型输出 → `ReasoningResult` 的**唯一**解析处。

## 为什么单独一个模块

有两条调用模型的路(`direct_llm` 直连 HTTP、`openjiuwen_runtime` 走 SDK 的 Workflow),
但"模型那句话里到底有什么"只能有一份规则。分两份的后果不是风格问题:`brief` 的清洗
规则里写着"标签只认 user_stated,其余一律按 model_assumed 归类"—— 那是**防止未确认的
值进确认字段**的东西。两份拷贝迟早会漂移,而漂移的那一份会把模型的一句猜测写进
`weekly_available_minutes`。

所以这里放三样东西:

1. **提示词渲染**(`render_turn`)—— 它是"模型看得到什么"这个问题的唯一答案,
   也是"模型看不到真实主键"那条红线的落点。
2. **输出解析**(`parse_claims` / `parse_actions` / `payload_to_result`)——
   逐项独立校验、独立丢弃,丢掉的东西**不会变成默认值**。
3. **清洗规则**(`clean_value`)—— 闭集字段 + 类型与范围判断。

## 这里只做形状判断,不做业务校验

`actions` 里出现 `n999`、出现环、出现别的空间的 id —— 这些在这里一律**放行**。
判断它们需要知道这个空间里有哪些节点、依赖成不成环,那是数据库里的状态,不是这段
代码能拿到的。放一个"只能看见模型输出、看不见真实数据"的半吊子校验器在这里,结果
是它放过去的东西还得在服务端再校验一遍,而两套规则的差异就是一个漏洞。
能不能落地由 `services/proposal_validation.py` 说了算。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from backend.agent.prompts.goal_reasoning import render_reasoning_turn
from backend.agent.prompts.planning import (
    TURN_TEMPLATE,
    render_brief_section,
    render_history_section,
    render_plan_section,
    render_relations_section,
    render_scope_section,
    render_time_section,
    render_tools_section,
)
from backend.agent.runtime.base import (
    AnalysisDraft,
    BriefClaim,
    QuestionDraft,
    QuestionOptionDraft,
    ReasoningMapDraft,
    ReasoningMapLinkDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
    ToolRequest,
    TurnContext,
)
from backend.db.models.enums import ModelSource

logger = logging.getLogger(__name__)

#: 一周的分钟数。超过它的"每周可投入"一定是模型算错了或单位搞混了(把小时当分钟)。
MINUTES_IN_WEEK = 7 * 24 * 60

#: 一次最多接受多少条变更。提示词里已经要求它只在条件齐全时产出计划,正常是十几条。
#: 设这个上限是为了给"模型开始复读、吐出两百条 create_node"留一道闸 ——
#: 那种输出会让校验与写入都变得很慢,而它没有任何价值。
MAX_ACTIONS = 80

#: 一轮最多问几个问题。产品规则:默认 1 个,仅两个高度相关时最多 2 个。
MAX_QUESTIONS = 2
#: 一个问题最多几个选项。阶段 10 收到 3 —— 选项是加速器,不是问卷。
MAX_QUESTION_OPTIONS = 3
#: 一句问题 / 一句"为什么现在问"最多多长。
MAX_QUESTION_CHARS = 500
MAX_WHY_NOW_CHARS = 500
#: 战略判断字段的长度上限。
MAX_ANALYSIS_CHARS = 800
MAX_RECOMMENDATION_CHARS = 500
MAX_DECISION_IMPACT_CHARS = 800
MAX_CONFIDENCE_NOTE_CHARS = 500
#: 选项 id 与标签的长度上限。
MAX_OPTION_ID_CHARS = 40
MAX_OPTION_LABEL_CHARS = 120

#: `response_mode` 的闭集。不在里面的整个问题丢掉 —— 与 brief 的闭集同一条纪律。
QUESTION_RESPONSE_MODES = frozenset({"single_select", "multi_select", "free_text", "mixed"})

#: 一次模型调用最多请求几个工具。服务端还会用全局预算卡总量。
MAX_TOOL_REQUESTS = 2
#: 工具名与 reason 的长度上限。
MAX_TOOL_NAME_CHARS = 64
MAX_TOOL_REASON_CHARS = 300

#: `stopReason` 的闭集。模型写别的 -> `None`(服务端自己判预算)。
STOP_REASONS = frozenset(
    {"ready_to_propose", "need_user_answer", "insufficient_evidence", "budget_exhausted", "failed"}
)

#: 一段文本类条件最多留多长。截断而不是丢弃:被模型写长了的目标仍然比没有目标有用。
TEXT_LIMITS = {
    "goal": 500,
    "current_level": 1000,
    "success_criteria": 1000,
}

#: brief 里允许出现的字段,**顺序有意义**:它会被渲染进给模型看的 JSON schema。
#: 闭集 —— 模型多想出来的键直接丢掉,免得某天它自创一个 "priority" 然后被一路带进数据库。
#:
#: 顺序写死而不是用集合,是因为"同一个 promptVersion 必须产出同一份输入"是排查模型
#: 行为的前提;集合的迭代顺序在不同进程之间不稳定,会让同一版本的提示词每次长得不一样。
BRIEF_FIELD_ORDER = (
    "goal",
    "deadline",
    "weekly_available_minutes",
    "current_level",
    "success_criteria",
    "constraints",
)

ALLOWED_BRIEF_FIELDS = frozenset(BRIEF_FIELD_ORDER)

#: 模型有时候会把 JSON 包在 ``` 代码块里,有时候前面还说一句话。
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class PayloadInvalid(Exception):
    """模型这次没按形状回答。**这是可预期的上游失败,不是崩溃。**"""


# ---------------------------------------------------------------------------------
# 提示词渲染
# ---------------------------------------------------------------------------------
def render_turn(turn: TurnContext) -> str:
    """把 TurnContext 渲染成**真正发给模型的那段文本**。

    ## 为什么是一个独立的模块级函数

    它是"模型到底看得到什么"这个问题的唯一答案。做成类的方法,要断言"模型看不到
    真实主键"就得先构造一个 reasoner、塞进一份配置、再想办法不真的发请求;做成
    独立函数,那条断言就是 `assert str(node.id) not in render_turn(turn)` ——
    测的是**实际会被发出去的那段字符串**,而不是它的一部分。

    `handle` 必须出现在这里,否则整套记号方案就断了:模型看不到记号,就只能看着
    一堆没有名字的节点,然后自己编一个引用 —— 而那个引用一定会被服务端按悬空引用
    拒绝。反过来,`node_handles` 里那份记号到真实 id 的映射**绝不能**出现在这里,
    见 `base.TurnContext` 的注释。
    """
    if turn.purpose == "goal_reasoning":
        # 目标推理回合走另一份模板:它关心的是决策维度与取舍,不是任务拆解。
        return render_reasoning_turn(turn)
    known = {
        "goal": turn.known.goal,
        "deadline": turn.known.deadline,
        "weekly_available_minutes": turn.known.weekly_available_minutes,
        "current_level": turn.known.current_level,
        "success_criteria": turn.known.success_criteria,
        "constraints": list(turn.known.constraints),
    }
    # 逐字段挑选,不是 `asdict(node)`。`asdict` 会把以后新加的每一个字段都带上,
    # 而"哪些东西可以进提示词"必须是一次一次明确的决定,不是默认全给。
    #
    # 正文(`description` / `acceptance_criteria`)在这里是**原样**的:截到多少字
    # 由渲染层决定,所以"模型实际看到多少正文"只有一处答案。
    nodes = [
        {
            "handle": n.handle,
            "title": n.title,
            "node_type": n.node_type,
            "purpose": n.purpose,
            "planning_level": n.planning_level,
            "status": n.status,
            "depth": n.depth,
            "deadline": n.deadline,
            "estimate_minutes": n.estimate_minutes,
            "parent_handle": n.parent_handle,
            "description": n.description,
            "acceptance_criteria": n.acceptance_criteria,
            "body_read": n.body_read,
            # 长笔记:三个事实 + (只对焦点节点)那份正文本身。正文同样是原样的,
            # 截到多少由渲染层决定 —— 与上面两行同一条纪律。
            "notes_present": n.notes_present,
            "notes_chars": n.notes_chars,
            "notes_read": n.notes_read,
            "note_body": n.note_body,
            "layer": n.layer,
            "read_only": n.read_only,
        }
        for n in turn.nodes
    ]
    edges = [
        {
            "source": edge.source,
            "target": edge.target,
            "kind": edge.kind,
            "relation_type": edge.relation_type,
            "note": edge.note,
        }
        for edge in turn.edges
    ]
    history = [{"role": r, "content": c} for r, c in turn.history]

    return TURN_TEMPLATE.format(
        current_date=turn.current_date,
        weekday=turn.weekday,
        timezone=turn.timezone,
        workspace_title=turn.workspace_title or "(未命名)",
        workspace_intent=turn.workspace_intent or "(用户没写)",
        scope_section=render_scope_section(
            scope_title=turn.scope_root_title,
            focus_handle=turn.focus_handle,
            focus_title=turn.context_node_title,
            writable=list(turn.writable_handles),
            nodes=nodes,
            live_node_count=turn.live_node_count,
            window_truncated=turn.window_truncated,
            current_view=turn.current_view,
        ),
        brief_section=render_brief_section(known),
        time_section=render_time_section(turn.time),
        plan_section=render_plan_section(nodes),
        relations_section=render_relations_section(edges, hidden=turn.edges_hidden),
        tools_section=render_tools_section(list(turn.tool_exchanges)),
        history_section=render_history_section(history),
        user_message=turn.user_message,
    )


# ---------------------------------------------------------------------------------
# 组装结果
# ---------------------------------------------------------------------------------
#: `payload_to_result` **真正会读**的键。它单独是个常量,为的是让"声明给 SDK 的键"
#: 与"解析器会读的键"能对得上,而且**不是靠测试里手抄一遍**。
#:
#: 这一条是真实模型验收抓出来的:C 批给提示词和解析器都加了 `analysis`,却漏了
#: `openjiuwen_runtime` 那份输出声明 —— 而那条路上模型给的对象是**照声明重建**的,
#: 不在声明里的键在到解析器之前就没了。于是七栏判断被安静地丢掉,一行记录都不落。
#: 原本那条"声明与解析必须一致"的测试没拦住,因为它自己也把键手抄了一遍 ——
#: 同一个遗漏写在了两个地方,看起来就是一致的。
#:
#: 所以键的来源收在这里一处:加一栏只改 `parse_analysis` 与这个常量,测试会替人
#: 盯住 `OUTPUT_CONFIG` 有没有跟上。
PARSED_PAYLOAD_FIELDS = frozenset(
    {
        "reply",
        "brief",
        "actions",
        "questions",
        "toolRequests",
        "stopReason",
        "analysis",
        "reasoningMap",
    }
)


def payload_to_result(
    payload: Any,
    *,
    source: ModelSource,
    request_id: str,
    prompt_version: str,
    model_name: str | None = None,
    latency_ms: int | None = None,
    usage: dict | None = None,
) -> ReasoningResult:
    """把模型给出的那个对象变成一次成功的调用结果。

    `reply` 缺失或不是一段非空文本时抛 `PayloadInvalid` —— 调用方把它翻译成
    `MODEL_OUTPUT_INVALID` 的降级结果,**不是**抛给上游。

    `brief` 与 `actions` 缺失是**正常情况**(模型这轮只是在回答问题,没有变更),
    不构成失败。这正是"部分可用比全部不可用好"的落点。

    这里读的键必须与 `PARSED_PAYLOAD_FIELDS` 一致,而后者必须与
    `openjiuwen_runtime.OUTPUT_CONFIG` 一致 —— 两条都有测试钉着。
    """
    if not isinstance(payload, dict):
        raise PayloadInvalid(f"模型给的不是一个对象: {type(payload).__name__}")

    reply = payload.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise PayloadInvalid("reply 不是一段非空文本")

    return ReasoningResult(
        reply=reply.strip(),
        source=source,
        brief_claims=tuple(parse_claims(payload.get("brief"))),
        actions=parse_actions(payload.get("actions")),
        questions=parse_questions(payload.get("questions")),
        tool_requests=parse_tool_requests(payload.get("toolRequests")),
        stop_reason=parse_stop_reason(payload.get("stopReason")),
        analysis=parse_analysis(payload.get("analysis")),
        reasoning_map=parse_reasoning_map(payload.get("reasoningMap")),
        request_id=request_id,
        prompt_version=prompt_version,
        model_name=model_name,
        latency_ms=latency_ms,
        usage=usage,
    )


def payload_from_chat_completion(raw: Any) -> tuple[dict, dict | None]:
    """从 chat/completions 的响应里取出模型那个 JSON 对象,附带 usage。

    这是**直连那条路专用的**拆包:openjiuwen 那条路拿到的已经是解析好的对象
    (它自己的 `OutputFormatter` 按 `output_config` 抽过一遍了)。
    """
    usage = raw.get("usage") if isinstance(raw, dict) and isinstance(raw.get("usage"), dict) else None
    content = raw["choices"][0]["message"]["content"]
    return parse_json_object(content), usage


def parse_json_object(content: str) -> dict:
    """从一段模型文本里抠出那个 JSON 对象。

    模型有时候把 JSON 包在 ``` 代码块里(哪怕提示词说了不要),有时候前面还写一句
    "好的,这是我的建议:"。这两种都是**常见回复**,不是异常 —— 所以这里先剥代码块,
    再用 `raw_decode` 从第一个花括号开始读,允许后面还有别的字。
    """
    text = content.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    decoder = json.JSONDecoder()
    try:
        parsed = decoder.raw_decode(text)[0]
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        parsed = decoder.raw_decode(text[start:])[0]
    if not isinstance(parsed, dict):
        raise PayloadInvalid(f"模型给的 JSON 不是一个对象: {type(parsed).__name__}")
    return parsed


# ---------------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------------
def parse_actions(raw_actions: Any) -> tuple[dict, ...]:
    """筛掉明显不是变更对象的条目,**其余原样交给服务端校验**。

    这里只做三件事:是 list 吗、每条是 dict 吗、有 `op` 字段吗。任何更进一步的判断
    (op 认不认识、引用解不解得开、依赖成不成环)都需要数据库状态,不属于这一层。
    """
    if not isinstance(raw_actions, list):
        if raw_actions not in (None, []):
            logger.info("actions 不是数组,已忽略: %r", type(raw_actions).__name__)
        return ()

    if len(raw_actions) > MAX_ACTIONS:
        logger.warning(
            "模型一次给了 %d 条变更,超过上限 %d,多余的已丢弃。", len(raw_actions), MAX_ACTIONS
        )

    kept: list[dict] = []
    for entry in raw_actions[:MAX_ACTIONS]:
        if not isinstance(entry, dict):
            continue
        # op 不是字符串的条目在这里就丢掉:它不是"一条校验失败的变更",
        # 而是一个连形状都不对的元素,留着只会让校验器多写一个特例分支。
        if not isinstance(entry.get("op"), str):
            continue
        kept.append(entry)
    return tuple(kept)


# ---------------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------------
def parse_questions(raw_questions: Any) -> tuple[QuestionDraft, ...]:
    """把模型给的 `questions` 变成**已经校验过**的问题草稿。

    与 `parse_actions` 的关键区别:actions 在这一层只做形状判断(能不能落地需要数据库),
    而问题在服务端能完成全部格式校验,所以这里就把它校验完 —— 选项数量、选项 id 唯一、
    `responseMode` 闭集、自由输入与选项的相容性。

    逐项独立校验、独立丢弃:第 2 个问题写坏了不该把第 1 个一起丢掉。
    超过 `MAX_QUESTIONS` 的**截断而不是报错** —— 模型偶尔会复读同一个问题。
    """
    if not isinstance(raw_questions, list):
        if raw_questions not in (None, []):
            logger.info("questions 不是数组,已忽略: %r", type(raw_questions).__name__)
        return ()

    if len(raw_questions) > MAX_QUESTIONS:
        logger.info(
            "模型一轮给了 %d 个问题,只留前 %d 个(上限见 MAX_QUESTIONS)",
            len(raw_questions),
            MAX_QUESTIONS,
        )

    kept: list[QuestionDraft] = []
    for entry in raw_questions[:MAX_QUESTIONS]:
        draft = _parse_question(entry)
        if draft is not None:
            kept.append(draft)
    return tuple(kept)


def _parse_question(entry: Any) -> QuestionDraft | None:
    """一个问题的严格形状。返回 None 表示这一条不可用。

    ## 为什么 `free_text` 与 `single_select` 在这里就把选项定死

    答案校验(见 `question_service.answer_question`)要判断"这个选项存不存在"。
    如果这里放行一个 `free_text` 却带着 3 个选项的问题,那条答案校验就无从判断
    用户点的那个 id 算不算数 —— 两处一定要有一处说了算,而这一层是最靠前的。
    """
    if not isinstance(entry, dict):
        return None

    question = entry.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    question = question.strip()[:MAX_QUESTION_CHARS]

    why_now = entry.get("whyNow", entry.get("why_now"))
    why_now = (
        why_now.strip()[:MAX_WHY_NOW_CHARS] if isinstance(why_now, str) else ""
    )

    # 阶段 10:提问前的战略判断。**先判断,后提问。**
    analysis_summary = entry.get("analysisSummary", entry.get("analysis_summary"))
    analysis_summary = (
        analysis_summary.strip()[:MAX_ANALYSIS_CHARS]
        if isinstance(analysis_summary, str)
        else ""
    )
    recommendation = entry.get("recommendation")
    recommendation = (
        recommendation.strip()[:MAX_RECOMMENDATION_CHARS]
        if isinstance(recommendation, str)
        else ""
    )
    decision_impact = entry.get("decisionImpact", entry.get("decision_impact"))
    decision_impact = (
        decision_impact.strip()[:MAX_DECISION_IMPACT_CHARS]
        if isinstance(decision_impact, str)
        else ""
    )
    confidence_note = entry.get("confidenceNote", entry.get("confidence_note"))
    confidence_note = (
        confidence_note.strip()[:MAX_CONFIDENCE_NOTE_CHARS]
        if isinstance(confidence_note, str) and confidence_note.strip()
        else None
    )

    mode = entry.get("responseMode", entry.get("response_mode"))
    if mode not in QUESTION_RESPONSE_MODES:
        return None

    options: list[QuestionOptionDraft] = []
    raw_options = entry.get("options")
    if isinstance(raw_options, list):
        seen: set[str] = set()
        for option in raw_options[:MAX_QUESTION_OPTIONS]:
            if not isinstance(option, dict):
                continue
            option_id = option.get("id")
            label = option.get("label")
            if not isinstance(option_id, str) or not option_id.strip():
                continue
            if not isinstance(label, str) or not label.strip():
                continue
            option_id = option_id.strip()[:MAX_OPTION_ID_CHARS]
            if option_id in seen:
                continue
            seen.add(option_id)
            recommended = option.get("recommended", option.get("isRecommended"))
            options.append(
                QuestionOptionDraft(
                    id=option_id,
                    label=label.strip()[:MAX_OPTION_LABEL_CHARS],
                    recommended=bool(recommended) if isinstance(recommended, bool) else False,
                )
            )

    allow_custom = entry.get("allowCustomInput", entry.get("allow_custom_input"))
    allow_custom = bool(allow_custom) if isinstance(allow_custom, bool) else False

    if mode == "free_text":
        # 自由输入题不该带选项 —— 带着也会被拒,不如在这里就归零。
        options = []
        allow_custom = True
    elif mode in {"single_select", "multi_select"}:
        if not options:
            # 选择题一个选项都没有 = 没问清楚。
            return None
    elif mode == "mixed":
        # 混合题:选项可选,自由输入一定允许。0 个选项时它等价于自由输入题。
        allow_custom = True

    return QuestionDraft(
        question=question,
        why_now=why_now,
        response_mode=mode,
        options=tuple(options),
        allow_custom_input=allow_custom,
        analysis_summary=analysis_summary,
        recommendation=recommendation,
        decision_impact=decision_impact,
        confidence_note=confidence_note,
    )


# ---------------------------------------------------------------------------------
# toolRequests / stopReason
# ---------------------------------------------------------------------------------
def parse_tool_requests(raw: Any) -> tuple[ToolRequest, ...]:
    """把模型给的 `toolRequests` 变成**形状合法**的工具请求。

    **这里只做形状判断,不判断工具是否存在、参数是否对、手柄是否可见** —— 那些需要
    注册表与本轮上下文,在 `agent_tools` 里做。放一个半吊子校验器在这里只会让两处
    规则漂移。
    """
    if not isinstance(raw, list):
        if raw not in (None, []):
            logger.info("toolRequests 不是数组,已忽略: %r", type(raw).__name__)
        return ()
    kept: list[ToolRequest] = []
    for entry in raw[:MAX_TOOL_REQUESTS]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments = entry.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        request_id = entry.get("id")
        reason = entry.get("reason")
        kept.append(
            ToolRequest(
                id=request_id.strip()[:MAX_TOOL_NAME_CHARS] if isinstance(request_id, str) and request_id.strip() else name.strip()[:MAX_TOOL_NAME_CHARS],
                name=name.strip()[:MAX_TOOL_NAME_CHARS],
                arguments=arguments,
                reason=reason.strip()[:MAX_TOOL_REASON_CHARS] if isinstance(reason, str) else "",
            )
        )
    return tuple(kept)


def parse_stop_reason(raw: Any) -> str | None:
    """`stopReason` 只能是闭集里的一个;写别的就返回 `None`(服务端自己判)。"""
    if isinstance(raw, str) and raw.strip() in STOP_REASONS:
        return raw.strip()
    return None


# ---------------------------------------------------------------------------------
# reasoningMap(目标推理地图)
# ---------------------------------------------------------------------------------
#: 闭集。与 `db/models/enums.py` 的取值逐字对应 —— 这里多一个,服务层就写不进库。
REASONING_NODE_TYPES = frozenset(
    {"dimension", "question", "risk", "resource", "route", "stage", "assumption"}
)
REASONING_NODE_STATUSES = frozenset(
    {"unexplored", "exploring", "resolved", "paused", "archived"}
)
REASONING_SOURCES = frozenset({"agent", "user", "research"})
REASONING_LINK_TYPES = frozenset({"depends_on", "influences"})
REASONING_PHASES = frozenset(
    {
        # 阶段 8(路线优先)的五个语义档。
        "orientation",
        "roadmap_draft",
        "roadmap_review",
        "strategy_confirmed",
        "execution_refinement",
        # 阶段 7 的旧值。存量会话仍在用,继续接受。
        "strategic_exploration",
        "strategic_convergence",
        "awaiting_strategy_confirmation",
        "execution_planning",
        "monitoring",
    }
)
REASONING_ACTIONS = frozenset(
    {"ask_user", "analyze", "expand", "confirm", "pause", "complete", "revisit"}
)

#: 一次最多接受多少个地图节点 / 连线(护栏,防模型复读)。服务层还会再夹总上限。
MAX_REASONING_NODES = 24
MAX_REASONING_LINKS = 40
#: 一个地图节点标题 / 摘要 / 理由的长度上限。
MAX_REASONING_TITLE_CHARS = 200
MAX_REASONING_TEXT_CHARS = 1000


def _clean_text_list(raw: Any, limit: int = 12) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for entry in raw[:limit]:
        if isinstance(entry, dict):
            entry = entry.get("text") or entry.get("value")
        if isinstance(entry, str) and entry.strip():
            out.append(entry.strip()[:MAX_REASONING_TEXT_CHARS])
    return tuple(out)


def _clean_reasoning_score(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(5, int(value)))


def _clean_reasoning_text(raw: Any) -> str | None:
    """可选的长文本字段(timeframe / deliverable / pass_criteria)。空串当没有。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text[:MAX_REASONING_TEXT_CHARS] if text else None


def _clean_iso_date(raw: Any) -> str | None:
    """阶段 11:接受 `YYYY-MM-DD` 字符串。**不合法就当没有**,不猜日期。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _clean_week(raw: Any) -> int | None:
    """相对周号(从 1 开始)。超出合理范围或非法就当没有。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    week = int(raw)
    return week if 1 <= week <= 520 else None


def _clean_timeframe_kind(entry: dict) -> str | None:
    """`dated` / `relative`。其它值当没有。"""
    raw = entry.get("timeframeKind")
    if raw is None:
        raw = entry.get("timeframe_kind")
    return raw if raw in ("dated", "relative") else None


def parse_reasoning_map(raw: Any) -> ReasoningMapDraft | None:
    """把模型的 `reasoningMap` 变成一份**形状合法**的草稿。

    **只做形状判断** —— 父子引用是否成环、handle 是否与已有节点冲突、战略阶段的
    问题是否合规,都需要数据库状态,在 `reasoning_service` 里做。这里丢掉的是
    "连形状都不对"的条目,留着会让服务层的校验器多出一堆特例分支。
    """
    if not isinstance(raw, dict):
        return None

    nodes: list[ReasoningMapNodeDraft] = []
    seen_handles: set[str] = set()
    for entry in (raw.get("nodes") or [])[:MAX_REASONING_NODES]:
        if not isinstance(entry, dict):
            continue
        handle = entry.get("handle")
        title = entry.get("title")
        if not isinstance(handle, str) or not handle.strip():
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        clean_handle = handle.strip()[:16]
        if clean_handle in seen_handles:
            # 同一份草稿里重复的 handle:丢掉后一个,而不是让唯一约束在写入时炸。
            logger.info("reasoningMap 出现重复 handle,已丢弃:%s", clean_handle)
            continue
        seen_handles.add(clean_handle)
        node_type = entry.get("nodeType")
        status = entry.get("status")
        source = entry.get("source")
        parent = entry.get("parent")
        summary = entry.get("summary")
        rationale = entry.get("rationale")
        nodes.append(
            ReasoningMapNodeDraft(
                handle=clean_handle,
                title=title.strip()[:MAX_REASONING_TITLE_CHARS],
                node_type=node_type if node_type in REASONING_NODE_TYPES else "dimension",
                parent_handle=parent.strip()[:16] if isinstance(parent, str) and parent.strip() else None,
                summary=summary.strip()[:MAX_REASONING_TEXT_CHARS] if isinstance(summary, str) and summary.strip() else None,
                status=status if status in REASONING_NODE_STATUSES else None,
                importance=_clean_reasoning_score(entry.get("importance")),
                uncertainty=_clean_reasoning_score(entry.get("uncertainty")),
                urgency=_clean_reasoning_score(entry.get("urgency")),
                impact=_clean_reasoning_score(entry.get("impact")),
                confidence=_clean_reasoning_score(entry.get("confidence")),
                rationale=rationale.strip()[:MAX_REASONING_TEXT_CHARS] if isinstance(rationale, str) and rationale.strip() else None,
                assumptions=_clean_text_list(entry.get("assumptions")),
                evidence=_clean_text_list(entry.get("evidence")),
                source=source if source in REASONING_SOURCES else "agent",
                timeframe=_clean_reasoning_text(entry.get("timeframe")),
                deliverable=_clean_reasoning_text(entry.get("deliverable")),
                pass_criteria=_clean_reasoning_text(
                    entry.get("passCriteria") if entry.get("passCriteria") is not None else entry.get("pass_criteria")
                ),
                timeframe_kind=_clean_timeframe_kind(entry),
                start_week=_clean_week(
                    entry.get("startWeek") if entry.get("startWeek") is not None else entry.get("start_week")
                ),
                end_week=_clean_week(
                    entry.get("endWeek") if entry.get("endWeek") is not None else entry.get("end_week")
                ),
                start_date=_clean_iso_date(
                    entry.get("startDate") if entry.get("startDate") is not None else entry.get("start_date")
                ),
                end_date=_clean_iso_date(
                    entry.get("endDate") if entry.get("endDate") is not None else entry.get("end_date")
                ),
            )
        )

    links: list[ReasoningMapLinkDraft] = []
    for entry in (raw.get("links") or [])[:MAX_REASONING_LINKS]:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        target = entry.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        source, target = source.strip()[:16], target.strip()[:16]
        if not source or not target or source == target:
            continue
        # 两端必须在本轮的 nodes 里或在已有节点里(服务层再校验已有节点)。
        if source not in seen_handles and target not in seen_handles:
            continue
        link_type = entry.get("type")
        note = entry.get("note")
        links.append(
            ReasoningMapLinkDraft(
                source_handle=source,
                target_handle=target,
                link_type=link_type if link_type in REASONING_LINK_TYPES else "influences",
                note=note.strip()[:MAX_REASONING_TEXT_CHARS] if isinstance(note, str) and note.strip() else None,
            )
        )

    if not nodes and not links:
        return None

    focus = raw.get("focus")
    focus_reason = raw.get("focusReason")
    phase = raw.get("phase")
    action = raw.get("turnAction")
    return ReasoningMapDraft(
        nodes=tuple(nodes),
        links=tuple(links),
        focus_handle=focus.strip()[:16] if isinstance(focus, str) and focus.strip() else None,
        focus_reason=focus_reason.strip()[:MAX_REASONING_TEXT_CHARS] if isinstance(focus_reason, str) and focus_reason.strip() else None,
        phase=phase if phase in REASONING_PHASES else None,
        turn_action=action if action in REASONING_ACTIONS else None,
    )


# ---------------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------------
def parse_claims(raw_brief: Any) -> list[BriefClaim]:
    """把模型给的 brief 变成若干条**已经校验过**的条件主张。

    每项独立校验、独立丢弃。丢掉的东西不会变成默认值。
    """
    if not isinstance(raw_brief, dict):
        return []

    claims: list[BriefClaim] = []
    for field, entry in raw_brief.items():
        if field not in ALLOWED_BRIEF_FIELDS:
            logger.info("模型给了一个不在闭集里的 brief 字段,已忽略: %s", field)
            continue
        if not isinstance(entry, dict):
            continue
        value = clean_value(field, entry.get("value"))
        if value is None:
            continue
        source = entry.get("source")
        # 标签只认这两个值。模型写别的(比如 "assumed"、"inferred")时**按更低信任的那边
        # 归类**,而不是猜它的意思 —— 猜错了就等于让一个未确认的值进了确认字段。
        normalized = "user_stated" if source == "user_stated" else "model_assumed"
        claims.append(BriefClaim(field=field, value=value, source=normalized))
    return claims


# ---------------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------------
#: `analysis` 里允许出现的栏,**顺序有意义**:它同时是渲染给用户看的顺序
#: —— 从"我读到了什么"到"我建议怎么走",读起来就是一条推理链。
#:
#: 闭集:模型多想出来的栏直接丢掉。理由与 `ALLOWED_BRIEF_FIELDS` 相同 ——
#: 不设闭集的话,某天它自创一个 `todo` 栏,里面的内容会被一路存进数据库,
#: 而没有任何一处代码知道该怎么显示它。
ANALYSIS_FIELD_ORDER = (
    "known",
    "unknowns",
    "evidence",
    "assumptions",
    "diagnosis",
    "strategy_options",
    "risks",
)

ALLOWED_ANALYSIS_FIELDS = frozenset(
    (*ANALYSIS_FIELD_ORDER, "confidence_note", "narrative")
)

#: 每栏最多留几条。**不是分页,是防复读**:模型偶尔会开始把同一句话说十遍,
#: 而一栏 500 条会把分析区变成一堵墙,用户一条都不会读。
MAX_ANALYSIS_ITEMS = 12

#: 每条最多多长。截断而不是丢弃 —— 写长了的判断仍然比没有判断有用。
MAX_ANALYSIS_ITEM_CHARS = 400

#: 可信度说明的长度上限。它被要求是"一句话",给到 400 是留足余地。
MAX_CONFIDENCE_CHARS = 400


def parse_analysis(raw_analysis: Any) -> AnalysisDraft | None:
    """把模型给的 `analysis` 变成一份**已经校验过**的判断。

    ## 三种"没有"都返回 `None`,但理由各不相同

    - 没给这个键(`None`)、或者给的不是对象 —— 这一轮它没打算判断什么,
      这是正常情况,不是失败。
    - 给了对象但七栏全空、也没有可信度说明与正文 —— 见 `is_empty`。
    - 给了内容但**某一栏**的形状不对 —— 只丢那一栏,其余留下。
      一条写坏的 `risks` 不该让整份判断消失。

    **三种都必须是 `None`,不能是"一个七栏全空的 `AnalysisDraft`"。** 调用方
    (`conversation_service.submit_turn` 与 `analysis_service.record`)判断的依据是
    `is not None`,一个空壳会被当成"模型给了一份判断"照收,于是**每一轮不涉及分析
    的对话都会在分析表里留下一行什么都没说的记录**。它会挤掉上一条真正有内容的
    分析 —— 而那正是这个模块开头说的要防的事。空壳只应该活在日志里。

    清洗规则与 `brief` 一致:逐项独立校验、独立丢弃,丢掉的东西不变成默认值。

    ## `narrative` 在这里**不截断**

    它是这次判断的正文(§2.2),而上限是 20,000 码点 —— 由
    `analysis_service.record` 在执行(**超限丢掉正文并记日志,不存一份截断的**,
    见那里)。这里只去首尾空白:解析器的职责是把模型的原话取出来,不是裁定它多长。
    在这一层截断会让"截了多少"变成一件没人知道的事。
    """
    if raw_analysis is None:
        return None
    if not isinstance(raw_analysis, dict):
        logger.info("analysis 不是对象,已忽略: %r", type(raw_analysis).__name__)
        return None

    sections: dict[str, tuple[str, ...]] = {}
    for field in ANALYSIS_FIELD_ORDER:
        sections[field] = _clean_items(raw_analysis.get(field), field)

    note = raw_analysis.get("confidence_note")
    confidence = None
    if isinstance(note, str) and note.strip():
        confidence = note.strip()[:MAX_CONFIDENCE_CHARS]

    body = raw_analysis.get("narrative")
    narrative = body.strip() if isinstance(body, str) and body.strip() else None

    draft = AnalysisDraft(confidence_note=confidence, narrative=narrative, **sections)
    return None if draft.is_empty() else draft


def _clean_items(raw: Any, field: str) -> tuple[str, ...]:
    """一栏里的若干条文本。字符串与单元素对象都收 —— 模型有时候会把一条写成
    `{"text": "..."}`,那仍然是它想说的话。

    ## 截断必须留下痕迹

    每栏 12 条、每条 400 字是**面向提示词的预算**(见 `MAX_ANALYSIS_ITEMS`),
    超出就切掉。但"切掉"和"模型只说了这么多"在库里长得一模一样:用户看到一句
    戛然而止的话,而没有任何地方说得出它是被切的。

    所以每一次**真的**切到时按 INFO 记一条:字段名 + 原始长度 + 上限。这不是给
    用户看的,是给"这一栏怎么总是半句话"这类提问留一条可查的线索。
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        logger.info("analysis.%s 不是数组,已忽略: %r", field, type(raw).__name__)
        return ()

    if len(raw) > MAX_ANALYSIS_ITEMS:
        logger.info(
            "analysis.%s 有 %d 条,只留前 %d 条(上限见 MAX_ANALYSIS_ITEMS)",
            field,
            len(raw),
            MAX_ANALYSIS_ITEMS,
        )

    items: list[str] = []
    for entry in raw[:MAX_ANALYSIS_ITEMS]:
        if isinstance(entry, dict):
            entry = entry.get("text") or entry.get("value") or entry.get("note")
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if text:
            if len(text) > MAX_ANALYSIS_ITEM_CHARS:
                logger.info(
                    "analysis.%s 的一条有 %d 字,截到 %d 字(上限见 MAX_ANALYSIS_ITEM_CHARS)",
                    field,
                    len(text),
                    MAX_ANALYSIS_ITEM_CHARS,
                )
            items.append(text[:MAX_ANALYSIS_ITEM_CHARS])
    return tuple(items)


def clean_value(field: str, value: Any) -> Any:
    """按字段类型清洗。返回 None 表示"这一项不可用"。"""
    if value is None:
        return None

    if field == "weekly_available_minutes":
        # bool 是 int 的子类,`isinstance(True, int)` 为真 —— 不排掉的话
        # 模型回一个 true 就会变成"每周 1 分钟"。
        if isinstance(value, bool) or not isinstance(value, int):
            logger.info("weekly_available_minutes 不是整数,已忽略: %r", value)
            return None
        if value <= 0 or value > MINUTES_IN_WEEK:
            logger.info("weekly_available_minutes 超出合理范围,已忽略: %r", value)
            return None
        return value

    if field == "deadline":
        if not isinstance(value, str):
            return None
        try:
            # fromisoformat 会拒绝 2026-13-45 和 "三个月后",这正是我们要的。
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            logger.info("deadline 不是合法 ISO 日期,已忽略: %r", value)
            return None

    if field == "constraints":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return None
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return cleaned or None

    limit = TEXT_LIMITS.get(field, 500)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


__all__ = [
    "ALLOWED_ANALYSIS_FIELDS",
    "ALLOWED_BRIEF_FIELDS",
    "ANALYSIS_FIELD_ORDER",
    "BRIEF_FIELD_ORDER",
    "MAX_ACTIONS",
    "MAX_QUESTIONS",
    "MAX_QUESTION_OPTIONS",
    "MAX_TOOL_REQUESTS",
    "MINUTES_IN_WEEK",
    "PARSED_PAYLOAD_FIELDS",
    "QUESTION_RESPONSE_MODES",
    "REASONING_ACTIONS",
    "REASONING_LINK_TYPES",
    "REASONING_NODE_STATUSES",
    "REASONING_NODE_TYPES",
    "REASONING_PHASES",
    "REASONING_SOURCES",
    "PayloadInvalid",
    "clean_value",
    "parse_actions",
    "parse_analysis",
    "parse_claims",
    "parse_questions",
    "parse_reasoning_map",
    "parse_stop_reason",
    "parse_tool_requests",
    "payload_from_chat_completion",
    "payload_to_result",
    "render_turn",
]

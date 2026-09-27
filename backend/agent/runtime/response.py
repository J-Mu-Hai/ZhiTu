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

from backend.agent.prompts.planning import (
    TURN_TEMPLATE,
    render_brief_section,
    render_history_section,
    render_plan_section,
    render_relations_section,
    render_scope_section,
    render_time_section,
)
from backend.agent.runtime.base import (
    AnalysisDraft,
    BriefClaim,
    ReasoningResult,
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
            "status": n.status,
            "depth": n.depth,
            "deadline": n.deadline,
            "estimate_minutes": n.estimate_minutes,
            "parent_handle": n.parent_handle,
            "description": n.description,
            "acceptance_criteria": n.acceptance_criteria,
            "body_read": n.body_read,
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
        history_section=render_history_section(history),
        user_message=turn.user_message,
    )


# ---------------------------------------------------------------------------------
# 组装结果
# ---------------------------------------------------------------------------------
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
        analysis=parse_analysis(payload.get("analysis")),
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

ALLOWED_ANALYSIS_FIELDS = frozenset((*ANALYSIS_FIELD_ORDER, "confidence_note"))

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
    - 给了对象但七栏全空、也没有可信度说明 —— 见 `is_empty`。
    - 给了内容但**某一栏**的形状不对 —— 只丢那一栏,其余留下。
      一条写坏的 `risks` 不该让整份判断消失。

    **三种都必须是 `None`,不能是"一个七栏全空的 `AnalysisDraft`"。** 调用方
    (`conversation_service.submit_turn` 与 `analysis_service.record`)判断的依据是
    `is not None`,一个空壳会被当成"模型给了一份判断"照收,于是**每一轮不涉及分析
    的对话都会在分析表里留下一行什么都没说的记录**。它会挤掉上一条真正有内容的
    分析 —— 而那正是这个模块开头说的要防的事。空壳只应该活在日志里。

    清洗规则与 `brief` 一致:逐项独立校验、独立丢弃,丢掉的东西不变成默认值。
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

    draft = AnalysisDraft(confidence_note=confidence, **sections)
    return None if draft.is_empty() else draft


def _clean_items(raw: Any, field: str) -> tuple[str, ...]:
    """一栏里的若干条文本。字符串与单元素对象都收 —— 模型有时候会把一条写成
    `{"text": "..."}`,那仍然是它想说的话。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        logger.info("analysis.%s 不是数组,已忽略: %r", field, type(raw).__name__)
        return ()

    items: list[str] = []
    for entry in raw[:MAX_ANALYSIS_ITEMS]:
        if isinstance(entry, dict):
            entry = entry.get("text") or entry.get("value") or entry.get("note")
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if text:
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
    "MINUTES_IN_WEEK",
    "PayloadInvalid",
    "clean_value",
    "parse_actions",
    "parse_analysis",
    "parse_claims",
    "payload_from_chat_completion",
    "payload_to_result",
    "render_turn",
]

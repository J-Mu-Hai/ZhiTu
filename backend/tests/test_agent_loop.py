"""有界工具循环、权限、provenance 与提案安全。

脚本化 reasoner 在这里返回**一串** `ReasoningResult`(而不是固定一份),用来验证:
中间轮的动作不生成提案、工具结果回填给下一次调用、预算耗尽被正确处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ReasoningResult, ToolRequest
from backend.db.models import (
    ReasoningState,
    ToolCallRecord,
)
from backend.db.models.enums import ModelSource, ReasoningStatus, ToolCallStatus
from backend.services import agent_loop_service
from backend.tests.conftest import snapshot


@dataclass
class SequenceReasoner:
    """按顺序返回一串预设结果,并记录每次收到的 TurnContext。"""

    results: tuple[ReasoningResult, ...]
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        if self._index < len(self.results):
            result = self.results[self._index]
        else:
            result = ReasoningResult(reply="没有更多轮次。", source=ModelSource.DIRECT_LLM)
        self._index += 1
        return result


def _result(
    *,
    reply: str = "好的。",
    actions: tuple[dict, ...] = (),
    tool_requests: tuple[ToolRequest, ...] = (),
    stop_reason: str | None = None,
    degraded: bool = False,
) -> ReasoningResult:
    return ReasoningResult(
        reply=reply,
        source=ModelSource.DIRECT_LLM,
        degraded=degraded,
        actions=actions,
        tool_requests=tool_requests,
        stop_reason=stop_reason,
    )


async def _send(client: httpx.AsyncClient, account, content: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, "clientMessageId": content},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------------
# 1. 工具结果回填 / 中间轮 actions 不生成提案
# ---------------------------------------------------------------------------------
async def test_tool_result_is_fed_back_and_only_final_actions_become_a_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            # 中间轮:请求工具,同时故意带一条 action —— 它**不能**变成提案。
            _result(
                reply="我先看看这个节点。",
                actions=(
                    {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "中间轮不该写"},
                ),
                tool_requests=(ToolRequest(id="t1", name="get_node", arguments={"handle": "n1"}, reason="看正文"),),
            ),
            # 终止轮:基于工具结果给最终动作。
            _result(
                reply="根据查到的事实,我提一个小变更。",
                actions=(
                    {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "最终轮才写"},
                ),
                stop_reason="ready_to_propose",
            ),
        )
    )
    use_reasoner(reasoner)

    body = await _send(app_client, account, "帮我看看根目标")
    assert body["proposalErrors"] == [], body["proposalErrors"]
    assert body["proposal"] is not None
    summaries = " ".join(item["summary"] for item in body["proposal"]["items"])
    assert "最终轮才写" in summaries
    assert "中间轮不该写" not in summaries

    # 第二次模型调用确实收到了工具结果。
    assert len(reasoner.calls) == 2
    exchanges = reasoner.calls[1].tool_exchanges
    assert exchanges and exchanges[0].tool_name == "get_node"
    assert exchanges[0].status == "ok"

    # state 与工具记录都落了库。
    state = await db.scalar(select(ReasoningState))
    assert state is not None
    assert state.status is ReasoningStatus.RESOLVED
    assert state.model_calls_used == 2
    assert state.tool_calls_used == 1
    assert any(item.get("source") == "tool" for item in state.facts_summary)
    records = list((await db.execute(select(ToolCallRecord))).scalars())
    assert len(records) == 1
    assert records[0].tool_name == "get_node"
    assert records[0].status is ToolCallStatus.OK


async def test_intermediate_actions_alone_do_not_create_a_proposal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(
        SequenceReasoner(
            results=(
                _result(
                    reply="先查一下。",
                    actions=({"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "不该出现"},),
                    tool_requests=(ToolRequest(id="t1", name="list_children", arguments={"handle": "n1"}),),
                ),
                _result(reply="查完了,这次不需要改计划。", stop_reason="insufficient_evidence"),
            )
        )
    )
    body = await _send(app_client, account, "有没有子节点")
    assert body["proposal"] is None
    assert body["proposalErrors"] == []
    plan = await _plan(app_client, account)
    assert all(node["title"] != "不该出现" for node in plan["nodes"])


# ---------------------------------------------------------------------------------
# 2. 预算耗尽:停、剥 actions、state blocked
# ---------------------------------------------------------------------------------
async def test_budget_exhaustion_stops_and_marks_state_blocked(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    forever = _result(
        reply="我再查一次。",
        actions=({"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "预算耗尽后不该写"},),
        tool_requests=(ToolRequest(id="t1", name="get_node", arguments={"handle": "n1"}),),
    )
    use_reasoner(SequenceReasoner(results=(forever, forever, forever)))

    body = await _send(app_client, account, "多查几轮")
    assert body["proposal"] is None, "预算耗尽时中间轮的 actions 不该生成提案"
    state = await db.scalar(select(ReasoningState))
    assert state is not None
    assert state.status is ReasoningStatus.BLOCKED
    assert state.model_calls_used == agent_loop_service.MAX_MODEL_CALLS
    assert state.tool_calls_used <= agent_loop_service.MAX_TOOL_CALLS
    plan = await _plan(app_client, account)
    assert all(node["title"] != "预算耗尽后不该写" for node in plan["nodes"])


# ---------------------------------------------------------------------------------
# 3. 权限 / 安全:未知工具、伪造句柄、非法参数
# ---------------------------------------------------------------------------------
async def test_unknown_tool_and_fake_handle_are_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            _result(
                reply="我试试。",
                tool_requests=(
                    ToolRequest(id="t1", name="drop_table", arguments={"table": "plan_nodes"}),
                    ToolRequest(id="t2", name="get_node", arguments={"handle": "n999"}),
                ),
            ),
            _result(reply="两个都没查到。", stop_reason="insufficient_evidence"),
        )
    )
    use_reasoner(reasoner)
    await _send(app_client, account, "查两个不存在的东西")

    exchanges = reasoner.calls[1].tool_exchanges
    assert {ex.status for ex in exchanges} == {"rejected"}
    # 错误摘要里不能带真实 UUID 或数据。
    assert all("plan_nodes" not in str(ex.summary) or "不认识" in str(ex.summary) for ex in exchanges)
    records = list((await db.execute(select(ToolCallRecord))).scalars())
    assert {r.status for r in records} == {ToolCallStatus.REJECTED}
    assert all(r.result_summary is None for r in records)


async def test_read_tools_do_not_touch_plan_data(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    # 先有一个节点,让 get_node 有正文可读。
    created = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": await _root_id(app_client, account), "title": "读我"},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text

    use_reasoner(
        SequenceReasoner(
            results=(
                _result(
                    reply="查一下。",
                    tool_requests=(
                        ToolRequest(id="t1", name="get_node", arguments={"handle": "n1"}),
                        ToolRequest(id="t2", name="list_children", arguments={"handle": "n1"}),
                    ),
                ),
                _result(reply="看完了。", stop_reason="ready_to_propose"),
            )
        )
    )
    plan_before = await _plan(app_client, account)
    await _send(app_client, account, "读一下根节点")

    await db.rollback()
    counts = await snapshot(db)
    assert counts["plan_nodes"] == len(plan_before["nodes"])
    assert counts["node_relations"] == 0
    assert counts["dependencies"] == 0
    assert counts["tool_call_records"] == 2
    assert counts["reasoning_states"] == 1


async def _root_id(client: httpx.AsyncClient, account) -> str:
    return next(
        node["id"] for node in (await _plan(client, account))["nodes"] if node["parentId"] is None
    )


# ---------------------------------------------------------------------------------
# 4. 模型失败:用户消息保留、state blocked、可恢复
# ---------------------------------------------------------------------------------
async def test_model_failure_keeps_the_user_message_and_blocks_state(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(SequenceReasoner(results=(_result(reply="", degraded=True),)))

    body = await _send(app_client, account, "这句话必须留下来")
    assert body["userMessage"]["content"] == "这句话必须留下来"
    assert body["degraded"] is True

    await db.rollback()
    state = await db.scalar(select(ReasoningState))
    assert state is not None
    assert state.status is ReasoningStatus.BLOCKED
    # 用户消息仍在库里(两次提交里的第一次)。
    from backend.db.models import Message

    stored = await db.scalar(
        select(Message).where(Message.content == "这句话必须留下来")
    )
    assert stored is not None


# ---------------------------------------------------------------------------------
# 5. 只读排期模拟:返回事实,写不了计划
# ---------------------------------------------------------------------------------
async def test_simulate_schedule_tool_returns_facts_without_writing(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            _result(
                reply="我先模拟一下。",
                tool_requests=(ToolRequest(id="t1", name="simulate_schedule", arguments={}),),
            ),
            _result(
                reply="模拟结果说有一份事实清单;我没有据此写日程。",
                stop_reason="ready_to_propose",
            ),
        )
    )
    use_reasoner(reasoner)
    await _send(app_client, account, "排得开吗")

    summary = reasoner.calls[1].tool_exchanges[0].summary
    assert "weeklyBudgetMinutes" in summary
    assert "gaps" in summary
    assert "recoveryOptionKinds" in summary
    # 没有写任何排期/计划数据。
    await db.rollback()
    counts = await snapshot(db)
    assert counts["scheduled_sessions"] == 0
    assert counts["schedule_applications"] == 0
    assert counts["plan_nodes"] == 1


# ---------------------------------------------------------------------------------
# 6. 提案安全:读工具之后仍只有待确认提案
# ---------------------------------------------------------------------------------
async def test_tools_never_bypass_the_proposal_confirmation(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(
        SequenceReasoner(
            results=(
                _result(
                    reply="先查容量。",
                    tool_requests=(ToolRequest(id="t1", name="get_time_capacity", arguments={}),),
                ),
                _result(
                    reply="基于事实我提一个节点。",
                    actions=(
                        {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "工具后小提案"},
                    ),
                    stop_reason="ready_to_propose",
                ),
            )
        )
    )
    body = await _send(app_client, account, "看容量再决定")
    assert body["proposal"] is not None
    # 确认之前计划不变。
    assert all(node["title"] != "工具后小提案" for node in (await _plan(app_client, account))["nodes"])

    confirmed = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/proposals/{body['proposal']['id']}/confirm",
        json={"idempotencyKey": "tool-loop-confirm"},
        headers=account.headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert any(node["title"] == "工具后小提案" for node in (await _plan(app_client, account))["nodes"])


# ---------------------------------------------------------------------------------
# 7. provenance:可区分来源,且没有原始 CoT
# ---------------------------------------------------------------------------------
async def test_reasoning_state_provenance_is_distinguishable(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    from backend.agent.runtime.base import AnalysisDraft, BriefClaim

    account = await make_account()
    result = ReasoningResult(
        reply="我读到了一件事,并做了一个假设。",
        source=ModelSource.DIRECT_LLM,
        brief_claims=(BriefClaim(field="current_level", value="大三", source="user_stated"),),
        tool_requests=(ToolRequest(id="t1", name="get_time_capacity", arguments={}),),
        analysis=AnalysisDraft(
            known=("根目标是空的",),
            assumptions=("他每周能投入 5 小时",),
            unknowns=("他什么时候要交",),
        ),
    )
    use_reasoner(
        SequenceReasoner(
            results=(
                result,
                _result(reply="综合一下。", stop_reason="insufficient_evidence"),
            )
        )
    )
    await _send(app_client, account, "这是我的情况")

    await db.rollback()
    state = await db.scalar(select(ReasoningState))
    assert state is not None
    sources = {item.get("source") for item in state.facts_summary}
    assert "tool" in sources
    assert "user" in sources
    assert "model_inference" in sources
    assert {item.get("source") for item in state.assumptions} == {"assumption"}
    # 没有原始 CoT / 隐藏提示词。
    assert "chain_of_thought" not in str(state.facts_summary)
    assert "system" not in str(state.facts_summary)

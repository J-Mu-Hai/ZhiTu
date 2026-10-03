"""阶段 11:Strategic Intake — 先问关键问题,再给时间架构。

这一组钉的是**首次节奏**:

1. 进入目标后先进入 intake,只在对话区逐步提关键问题,**不直接生路线图、不生成
   散乱画布讨论节点**;
2. 已知信息(例如每周可投入)跳过、不重复问;
3. 工具 / 教材 / 每天几点这类琐碎问题不得进入 intake;
4. intake 最多 5 个问题,到顶必须继续生成时间架构,不能无限追问;
5. 时间架构带**结构化时间范围**:有日期用年月日,没有用相对第 N–M 周并标“待校准”;
6. 确认之前不写任何 PlanNode / 任务 / 日程。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    QuestionDraft,
    QuestionOptionDraft,
    ReasoningMapDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
)
from backend.db.models import GoalReasoningSession, PlanNode
from backend.db.models.enums import ModelSource, ReasoningSessionPhase


def _roadmap_draft(*, kind: str = "relative", stages: int = 4) -> ReasoningMapDraft:
    nodes: list[ReasoningMapNodeDraft] = [
        ReasoningMapNodeDraft(
            handle="r1",
            title="推荐路线:约 10 周从基础到可展示项目",
            node_type="route",
            timeframe="约 10 周",
            timeframe_kind=kind,
            deliverable="一个可展示的数据分析项目",
            pass_criteria="能端到端做完并讲清结论",
        )
    ]
    for index in range(1, stages + 1):
        start_week = (index - 1) * 2 + 1
        end_week = index * 2
        nodes.append(
            ReasoningMapNodeDraft(
                handle=f"r{index + 1}",
                title=f"阶段 {index}",
                node_type="stage",
                parent_handle="r1",
                timeframe=f"第 {start_week}–{end_week} 周" if kind == "relative" else None,
                deliverable=f"成果 {index}",
                pass_criteria=f"通过标准 {index}",
                timeframe_kind=kind,
                start_week=start_week if kind == "relative" else None,
                end_week=end_week if kind == "relative" else None,
                start_date=f"2026-0{index}-01" if kind == "dated" else None,
                end_date=f"2026-0{index}-28" if kind == "dated" else None,
            )
        )
    return ReasoningMapDraft(
        nodes=tuple(nodes),
        focus_handle="r2",
        focus_reason="第一阶段最该先定下来",
        phase="roadmap_draft",
        turn_action="ask_user",
    )


def _intake_question(question: str = "你希望最终获得什么成果?") -> QuestionDraft:
    return QuestionDraft(
        question=question,
        why_now="它会改变总时长与阶段成果",
        response_mode="single_select",
        options=(
            QuestionOptionDraft(id="portfolio", label="一个能展示的作品", recommended=True),
            QuestionOptionDraft(id="automation", label="自动化脚本"),
        ),
        allow_custom_input=True,
        analysis_summary="你已给出目标方向;不同成果会改变阶段顺序。",
        recommendation="先明确要交出的东西,再定阶段。",
        decision_impact="不同成果会改变阶段 2 的素材与阶段 3 的形式。",
        confidence_note="“尽快见效”是推断,若不对请纠正。",
    )


@dataclass
class SeqReasoner:
    """按顺序返回 (draft, questions)。"""

    results: tuple[tuple[ReasoningMapDraft | None, tuple[QuestionDraft, ...]], ...]
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        draft, questions = self.results[min(self._index, len(self.results) - 1)]
        self._index += 1
        return ReasoningResult(
            reply="我先把最该确认的一点问清楚。",
            source=ModelSource.DIRECT_LLM,
            reasoning_map=draft,
            questions=questions,
        )


async def _enter(client: httpx.AsyncClient, account, key: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _questions(client: httpx.AsyncClient, account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/questions", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["questions"]


# ---------------------------------------------------------------------------------
# 1. 首轮只问一个关键问题,不直接生路线图
# ---------------------------------------------------------------------------------
async def test_first_turn_asks_intake_question_instead_of_roadmap(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(SeqReasoner(results=((None, (_intake_question(),)),)))

    body = await _enter(app_client, account, "intake-1")
    view = body["reasoning"]
    # 没有路线 / 阶段 —— 首轮不生成整张地图。
    assert view["nodes"] == []
    assert view["phase"] == ReasoningSessionPhase.INTAKE.value
    assert view["datesCalibrated"] is False
    assert body["question"] is not None

    questions = await _questions(app_client, account)
    assert len(questions) == 1
    # **只在对话区**:presentation 是 conversation_intake,不是画布节点。
    assert questions[0]["presentation"] == "conversation_intake"
    assert questions[0]["analysisSummary"]


# ---------------------------------------------------------------------------------
# 2. 已知信息跳过:intake 不再重复问每周投入
# ---------------------------------------------------------------------------------
async def test_intake_skips_known_weekly_time(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    # 空间意图里已经写了每周可投入。
    await app_client.patch(
        f"/api/workspaces/{account.workspace_id}",
        json={"intent": "我想学习 Python,每周 150 分钟。"},
        headers=account.headers,
    )
    duplicate = _intake_question("你每周能投入多少小时?")
    good = _intake_question("你希望最终获得什么成果?")
    use_reasoner(SeqReasoner(results=((None, (duplicate, good)),)))

    await _enter(app_client, account, "intake-known")
    questions = await _questions(app_client, account)
    assert len(questions) == 1
    assert "每周" not in questions[0]["question"]


# ---------------------------------------------------------------------------------
# 3. 琐碎问题不得进入 intake
# ---------------------------------------------------------------------------------
async def test_intake_blocks_trivial_questions(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    trivia = _intake_question("你想用哪个 IDE?")
    roadmap = _roadmap_draft()
    use_reasoner(SeqReasoner(results=((None, (trivia,)), (roadmap, ()))))

    body = await _enter(app_client, account, "intake-trivial")
    # 琐碎问题被拦掉,于是这一轮不再产生问题,继续要求架构 -> 第二轮给出架构。
    assert body["reasoning"]["phase"] != ReasoningSessionPhase.INTAKE.value or body["question"] is None
    if body["question"] is None:
        second = await _enter(app_client, account, "intake-trivial-2")
        assert second["reasoning"]["nodes"], "拦掉琐碎问题后必须继续生成架构"


# ---------------------------------------------------------------------------------
# 4. intake 有上限:到 5 个后必须生成架构
# ---------------------------------------------------------------------------------
async def test_intake_max_questions_then_architecture(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(
        SeqReasoner(
            results=(
                (None, (_intake_question("问题 1?"),)),
                (None, (_intake_question("问题 2?"),)),
                (_roadmap_draft(), ()),
            )
        )
    )
    await _enter(app_client, account, "intake-max-1")

    session = await db.scalar(
        select(GoalReasoningSession).where(
            GoalReasoningSession.workspace_id == uuid.UUID(account.workspace_id)
        )
    )
    assert session is not None
    # 人为把计数推到上限:下一次必须**不再问**,而是给出时间架构。
    session.intake_questions_asked = 5
    await db.commit()

    body = await _enter(app_client, account, "intake-max-2")
    assert body["reasoning"]["nodes"], "到顶必须生成时间架构,不能无限追问"


# ---------------------------------------------------------------------------------
# 5. 时间架构:相对周 + 日期待校准
# ---------------------------------------------------------------------------------
async def test_architecture_uses_relative_weeks_when_no_dates(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(SeqReasoner(results=((_roadmap_draft(kind="relative"), ()),)))

    body = await _enter(app_client, account, "arch-relative")
    view = body["reasoning"]
    assert view["phase"] == ReasoningSessionPhase.TEMPORAL_ARCHITECTURE_DRAFT.value
    assert view["datesCalibrated"] is False
    stages = [node for node in view["nodes"] if node["nodeType"] == "stage"]
    assert 3 <= len(stages) <= 5
    assert all(stage["timeframeKind"] == "relative" for stage in stages)
    assert all(stage["startWeek"] and stage["endWeek"] for stage in stages)
    # **不伪造日历日期。**
    assert all(stage["startDate"] is None and stage["endDate"] is None for stage in stages)


async def test_architecture_marks_dates_calibrated_when_given(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(SeqReasoner(results=((_roadmap_draft(kind="dated"), ()),)))

    body = await _enter(app_client, account, "arch-dated")
    view = body["reasoning"]
    assert view["datesCalibrated"] is True
    stages = [node for node in view["nodes"] if node["nodeType"] == "stage"]
    assert all(stage["timeframeKind"] == "dated" for stage in stages)
    assert all(stage["startDate"] and stage["endDate"] for stage in stages)


# ---------------------------------------------------------------------------------
# 6. 确认之前不写任何 PlanNode
# ---------------------------------------------------------------------------------
async def test_architecture_writes_no_plan_nodes_before_confirmation(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(SeqReasoner(results=((_roadmap_draft(kind="relative"), ()),)))
    await _enter(app_client, account, "arch-no-plan")

    await db.rollback()
    rows = list((await db.execute(select(PlanNode))).scalars())
    # 新空间只有根目标一行;架构本身不是 PlanNode。
    assert len(rows) == 1, "确认前不得把战略架构写成计划节点"

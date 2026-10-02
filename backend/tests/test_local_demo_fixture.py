"""本地完整体验的脚本回放 fixture。

它不是产品能力:它念的是写死的 JSON(界面来源徽标会写「脚本回放」)。这里钉的是
**它有没有覆盖本地体验要演示的那几条**,以及它能不能被 `ScriptedReasoner` 正常解析。
fixture 少一条,用户在本地就少看到一种功能,而那不会被别的测试发现。
"""

from __future__ import annotations

from pathlib import Path

from backend.agent.runtime.response import parse_questions, parse_tool_requests

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "scripts" / "dev" / "fixtures" / "local-demo-script.json"


def _turns() -> list[dict]:
    from backend.agent.runtime.scripted import parse_script

    assert FIXTURE.exists(), f"fixture 不在仓库里: {FIXTURE}"
    return list(parse_script(FIXTURE.read_text(encoding="utf-8")))


def test_fixture_is_valid_and_parseable() -> None:
    from backend.agent.runtime.scripted import ScriptedReasoner

    turns = _turns()
    assert len(turns) >= 5, "本地演示至少需要事实 / 问题 / 战略 / 工具 / 下一层这几种轮次"
    # 能被真正构造出来,才可能在本地跑起来。
    ScriptedReasoner(tuple(turns))


def test_fixture_covers_a_fact_without_deadline() -> None:
    actions = [action for turn in _turns() for action in turn.get("actions", [])]
    info = [
        action
        for action in actions
        if action.get("op") == "create_node"
        and action.get("purpose") == "information"
        and action.get("deadline") is None
    ]
    assert info, "fixture 没有「无截止日期的事实 -> 信息节点」这一条"


def test_fixture_covers_a_question_card() -> None:
    questions = [
        q
        for turn in _turns()
        for q in parse_questions(turn.get("questions"))
    ]
    assert questions, "fixture 没有回答问题卡这一条"
    assert all(q.options for q in questions if q.response_mode in {"single_select", "multi_select"})


def test_fixture_covers_a_strategy_that_is_confirmed_before_the_next_layer() -> None:
    actions = [action for turn in _turns() for action in turn.get("actions", [])]
    strategies = [
        action
        for action in actions
        if action.get("op") == "create_node" and action.get("planningLevel") == "strategy"
    ]
    finer = [
        action
        for action in actions
        if action.get("op") == "create_node"
        and action.get("planningLevel") in {"phase", "month", "week", "day"}
    ]
    assert strategies, "fixture 没有战略选择"
    assert finer, "fixture 没有战略之后的下一层"
    # 战略必须出现在下一层之前(确认之后才下钻)。
    strategy_index = next(
        i for i, turn in enumerate(_turns()) if any(a.get("planningLevel") == "strategy" for a in turn.get("actions", []))
    )
    finer_index = next(
        i
        for i, turn in enumerate(_turns())
        if any(
            a.get("planningLevel") in {"phase", "month", "week", "day"}
            for a in turn.get("actions", [])
        )
    )
    assert strategy_index < finer_index, "下一层必须排在战略之后"


def test_fixture_covers_a_relation_and_a_read_tool() -> None:
    actions = [action for turn in _turns() for action in turn.get("actions", [])]
    relations = [action for action in actions if action.get("op") == "create_relation"]
    assert {action.get("relationType") for action in relations} & {"related_to", "influences"}, (
        "fixture 没有 related_to / influences 关系建议"
    )
    tools = [tool for turn in _turns() for tool in parse_tool_requests(turn.get("toolRequests"))]
    assert tools, "fixture 没有只读工具请求(例如 simulate_schedule)"
    assert "simulate_schedule" in {tool.name for tool in tools}

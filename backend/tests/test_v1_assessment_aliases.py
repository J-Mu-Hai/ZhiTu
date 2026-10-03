"""V1 战略判断解析:线上模型字段漂移的**同义名容错**。

背景:一次真实 OpenJiuwen + DeepSeek 验收里,模型返回的 `candidateDirections` 用了
`id`/`label`/`note`,`responseMode` 用了自造的 `single_select`,`keyDimensions` 甚至是
字符串数组 —— 严格只认一种拼法的解析器会把候选方向/判断**静默丢掉**,闭环停在
`problem_structure`(见 `logs/accept_v1_active_loop.py`)。

这些用例是那次真实验收的**回归钉**:同义名要能归一,但闭集、数量、长度一律不放松。
"""

from __future__ import annotations

from backend.agent.runtime.response import parse_v1_assessment


def test_live_model_drift_is_normalized() -> None:
    """把实测到的漂移形状原样喂进去,候选方向/判断不得被丢。"""
    raw = {
        "globalAssessment": "条件已足够定战略。",
        "strategicThesis": "只学够用的最小语法,把时间压在真实数据上。",
        "keyDimensions": ["数据来源", "报告形态", "环境搭建"],  # 字符串数组:无节点键,忽略
        "nodeUpdates": [
            {
                "nodeKey": "goal_definition",
                "judgment": "30 天做出一个能展示的分析项目。",
                "knownFacts": ["用户说零基础、每天 1 小时"],
                "assumptions": ["假设能拿到一份真实数据"],
            }
        ],
        "responseMode": "single_select",
        "criticalQuestion": "那份真实数据从哪来?",
        "candidateDirections": [
            {"id": "course_data", "label": "课程/比赛给的数据", "note": "省去找数据的时间"},
            {"id": "self_data", "label": "自己想分析的东西", "note": "动力最强"},
        ],
        "focusKey": "data_source",
        "focusReason": "决定第一周先找数据还是先学工具。",
        "strategyTradeoff": "先定数据,进度更稳。",
        "strategyReady": True,
    }
    draft = parse_v1_assessment(raw)
    assert draft is not None
    assert draft.critical_question == "那份真实数据从哪来?"
    assert draft.response_mode == "offer_options", "single_select 应归一为 offer_options"
    assert [d.key for d in draft.candidate_directions] == ["course_data", "self_data"]
    assert draft.candidate_directions[0].title == "课程/比赛给的数据"
    assert draft.candidate_directions[0].reason == "省去找数据的时间"
    assert draft.node_updates and draft.node_updates[0].node_key == "goal_definition"
    assert draft.node_updates[0].known_facts == ("用户说零基础、每天 1 小时",)
    assert draft.focus_key == "data_source"
    assert draft.strategy_ready is True


def test_snake_case_and_generic_aliases() -> None:
    raw = {
        "thesis": "整体判断走 thesis 字段。",
        "node_updates": [
            {
                "key": "key_conflict",
                "analysis": "卡在目标太大。",
                "known_facts": ["用户说 30 天必须出成果"],
                "why": "它决定路线。",
            }
        ],
        "response_mode": "question",
        "critical_question": "你更在意哪种成果?",
        "candidate_directions": [{"value": "a", "name": "方向 A", "description": "理由 A"}],
        "focus": "true_intent",
        "focus_reason": "它最影响路线。",
        "tradeoff": "先要能展示的成果。",
        "ready": True,
    }
    draft = parse_v1_assessment(raw)
    assert draft is not None
    assert draft.strategic_thesis == "整体判断走 thesis 字段。"
    assert draft.response_mode == "ask"
    assert draft.critical_question == "你更在意哪种成果?"
    assert draft.node_updates[0].node_key == "key_conflict"
    assert draft.node_updates[0].judgment == "卡在目标太大。"
    assert draft.candidate_directions[0].key == "a"
    assert draft.candidate_directions[0].title == "方向 A"
    assert draft.candidate_directions[0].reason == "理由 A"
    assert draft.focus_key == "true_intent"
    assert draft.strategy_tradeoff == "先要能展示的成果。"
    assert draft.strategy_ready is True


def test_unknown_response_mode_falls_back_to_none() -> None:
    draft = parse_v1_assessment({"responseMode": "totally_made_up"})
    assert draft is not None
    assert draft.response_mode == "none"


def test_length_caps_still_apply() -> None:
    raw = {
        "candidateDirections": [
            {"id": f"k{i}", "label": f"方向 {i}"} for i in range(6)
        ],
        "nodeUpdates": [
            {"nodeKey": f"key_{i}", "judgment": "j"} for i in range(6)
        ],
    }
    draft = parse_v1_assessment(raw)
    assert draft is not None
    assert len(draft.candidate_directions) == 3, "同义名容错不放松数量上限"
    assert len(draft.node_updates) == 3

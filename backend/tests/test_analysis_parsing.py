"""模型给的那段 `analysis` —— 从一段不可信的 JSON 变成一份判断。

## 这个边界上最容易犯的错不是"漏了一栏",是**收了空壳**

`parse_analysis` 有三条"没有"的路:没给这个键、给了空的、给的东西形状不对。三条
都必须是 `None`。一旦其中一条返回"一个七栏全空的 `AnalysisDraft`",调用方
(`submit_turn` 里那句 `if result.analysis is not None`)就会把它当成"模型给了一份
判断"记下来 —— 于是**每一轮不涉及分析的普通对话都会在分析表里留下一条什么都没说
的记录**,而它会挤掉上一条真正有内容的分析。

这不是假设:第一版的 `None` 分支返回的正是空壳,而这段代码在集成测试里看不出来
——那里的假模型直接把 `AnalysisDraft` 交给 `ReasoningResult`,根本不走这个函数。
所以这里有一条**从 `payload_to_result` 出发**的断言,盯的就是那条只在真实模型路径
上才走到的分支。

## 清洗的取向:丢一条,不丢一栏

模型偶尔会把某一栏写坏(给个对象、给个数字、把一条写成 `{"text": ...}`)。一种做法
是整栏丢掉,另一种是逐条挑出能用的。这里是后者 —— 一份 `risks` 里有三条读得懂、
一条读不懂时,那三条仍然该被用户看到。**丢弃是逐条的,而且丢掉的不会变成默认值。**
"""

from __future__ import annotations

from backend.agent.runtime.base import AnalysisDraft
from backend.agent.runtime.response import (
    ANALYSIS_FIELD_ORDER,
    MAX_ANALYSIS_ITEM_CHARS,
    MAX_ANALYSIS_ITEMS,
    MAX_CONFIDENCE_CHARS,
    parse_analysis,
    payload_to_result,
)
from backend.db.models.enums import ModelSource


def _result(payload: dict):
    return payload_to_result(
        payload, source=ModelSource.DIRECT_LLM, request_id="probe", prompt_version="probe-v1"
    )


# ---------------------------------------------------------------------------------
# 三种"没有"都必须是 None
# ---------------------------------------------------------------------------------
def test_a_turn_without_an_analysis_key_is_not_an_analysis() -> None:
    """普通对话(模型的回答里压根没有 `analysis` 这个键)不产生分析记录。

    **这条走的是 `payload_to_result`,不是直接调 `parse_analysis`。** 差别很要紧:
    直接调只能证明这个函数的返回值,证明不了"调用方拿到的是不是 None"。中间那句
    `analysis=parse_analysis(...)` 才是真正决定"会不会多出一行"的地方。
    """
    assert _result({"reply": "好的,我记下了。"}).analysis is None


def test_an_empty_analysis_object_is_not_an_analysis() -> None:
    """模型给了 `analysis: {}` —— 提了这件事,但什么都没说。同样不收。"""
    assert _result({"reply": "好的。", "analysis": {}}).analysis is None


def test_a_seven_column_draft_with_nothing_in_it_is_not_an_analysis() -> None:
    """七栏都给了、但每一栏都是空数组 —— 仍然是"什么都没说"。

    这一条与上一条的区别只有形式:模型有时候会把七个键一个不落地写出来,内容却是
    空的。它比 `{}` 更容易骗过"看键在不在"的判断,所以单独钉一条。
    """
    raw = {field: [] for field in ANALYSIS_FIELD_ORDER}
    assert parse_analysis(raw) is None


def test_an_analysis_that_is_not_an_object_is_ignored() -> None:
    """模型把这一栏写成了一段字符串 —— 忽略,不是崩溃,也不是"收下再说"。"""
    assert parse_analysis("我觉得这个计划有点赶") is None


# ---------------------------------------------------------------------------------
# 有内容时:逐栏收,逐条挑
# ---------------------------------------------------------------------------------
def test_one_column_of_content_is_enough_to_keep_the_analysis() -> None:
    """只要有一栏说了话,这份判断就要留下。其余栏是空元组,不是"这一栏不存在"。"""
    draft = parse_analysis({"risks": ["按这个拆法,第三周会排不开"]})

    assert draft is not None
    assert draft.risks == ("按这个拆法,第三周会排不开",)
    assert draft.known == ()
    assert draft.confidence_note is None


def test_a_single_string_is_accepted_as_one_item() -> None:
    """`"risks": "会排不开"` —— 模型少写了一对中括号,但意思完全一样。"""
    draft = parse_analysis({"risks": "会排不开"})

    assert draft is not None
    assert draft.risks == ("会排不开",)


def test_an_item_wrapped_in_an_object_is_still_that_item() -> None:
    """`{"text": ...}` / `{"value": ...}` / `{"note": ...}` 三种包装都收。

    这是模型很常见的一种写法。丢掉它等于丢掉一条它明明说了的判断,而用户看到的
    只是"这条分析比上一轮少了点什么"。
    """
    draft = parse_analysis(
        {
            "known": [{"text": "用户已经会 Python"}],
            "unknowns": [{"value": "每周能投入多少时间"}],
            "evidence": [{"note": "用户说设备已经到手"}],
        }
    )

    assert draft is not None
    assert draft.known == ("用户已经会 Python",)
    assert draft.unknowns == ("每周能投入多少时间",)
    assert draft.evidence == ("用户说设备已经到手",)


def test_a_broken_column_does_not_take_the_rest_with_it() -> None:
    """一栏写坏了(不是数组),只丢那一栏 —— 其余四栏照收。

    整份丢掉是最省事的写法,也是最坏的一种:用户看到的是"AI 这轮没给分析",
    而它其实给了四条。
    """
    draft = parse_analysis(
        {
            "known": {"a": "不是数组"},
            "risks": ["会排不开"],
            "diagnosis": ["拆得比实际能投入的细"],
        }
    )

    assert draft is not None
    assert draft.known == ()
    assert draft.risks == ("会排不开",)
    assert draft.diagnosis == ("拆得比实际能投入的细",)


def test_a_junk_item_is_skipped_and_the_readable_ones_are_kept() -> None:
    """一栏里混了数字、空串、null —— 跳过那几条,剩下的留下,顺序不变。"""
    draft = parse_analysis({"risks": ["会排不开", 42, "  ", None, "场地还没定"]})

    assert draft is not None
    assert draft.risks == ("会排不开", "场地还没定")


def test_too_many_items_are_cut_not_kept_all() -> None:
    """一栏最多留 `MAX_ANALYSIS_ITEMS` 条。

    这是**防复读**,不是分页:模型有时候会开始把同一句话写十几遍,而一栏几十条会把
    分析区变成一堵墙 —— 用户一条都不会读。切掉的是尾巴,留下的是它最先说的那些。
    """
    draft = parse_analysis({"known": [f"第 {i} 条" for i in range(MAX_ANALYSIS_ITEMS + 8)]})

    assert draft is not None
    assert len(draft.known) == MAX_ANALYSIS_ITEMS
    assert draft.known[0] == "第 0 条"


def test_a_long_item_is_truncated_not_dropped() -> None:
    """一条写长了就截断。

    **截断而不是丢弃**,因为一条被截断的判断仍然是一条判断 —— 用户读到前半句
    也知道模型想说什么了;而丢掉的话,那一栏就变成了空的。
    """
    long_text = "会" * (MAX_ANALYSIS_ITEM_CHARS + 50)
    draft = parse_analysis({"risks": [long_text]})

    assert draft is not None
    assert len(draft.risks) == 1
    assert len(draft.risks[0]) == MAX_ANALYSIS_ITEM_CHARS


def test_the_confidence_note_is_trimmed_and_capped() -> None:
    """可信度是"一句话",前后空白去掉、超长截断。"""
    assert parse_analysis({"risks": ["x"], "confidence_note": "  中等  "}).confidence_note == "中等"

    long_note = "较" * (MAX_CONFIDENCE_CHARS + 20)
    draft = parse_analysis({"risks": ["x"], "confidence_note": long_note})
    assert draft is not None
    assert len(draft.confidence_note) == MAX_CONFIDENCE_CHARS


def test_a_blank_or_non_string_confidence_note_becomes_none() -> None:
    """空的、或者根本不是字符串的可信度说明 —— 当成"没说",不编一个默认值。"""
    for raw in ("", "   ", 0.8, ["中等"]):
        draft = parse_analysis({"risks": ["x"], "confidence_note": raw})
        assert draft is not None
        assert draft.confidence_note is None, raw


def test_confidence_alone_is_enough_to_keep_the_analysis() -> None:
    """七栏全空、只有一句可信度 —— 这仍然是一份判断,不能丢。"""
    draft = parse_analysis({"confidence_note": "这几条我把握不大"})

    assert draft is not None
    assert draft.confidence_note == "这几条我把握不大"


# ---------------------------------------------------------------------------------
# 结构上的两条护栏
# ---------------------------------------------------------------------------------
def test_unknown_keys_are_ignored_without_dropping_the_analysis() -> None:
    """模型多写了一个我们没定义的键 —— 忽略它,其余照收。

    收紧成 `extra="forbid"` 那样的报错会让一次写歪的键把整份分析带走,而它对用户
    的价值是零。
    """
    draft = parse_analysis({"risks": ["会排不开"], "mood": "担心", "confidence": 0.8})

    assert draft is not None
    assert draft.risks == ("会排不开",)


def test_the_field_order_covers_every_column_of_the_draft() -> None:
    """`ANALYSIS_FIELD_ORDER` 必须与 `AnalysisDraft` 的七栏**一模一样**。

    这个常量被三处共用(提示词的说明、这里的清洗、契约层的序列化)。将来加了第八栏
    而忘了改这里的话,那一栏就会被**静默丢掉** —— 模型说了、库里没有、界面上不显示,
    而且任何地方都不报错。所以这里用一个集合相等把它钉死。
    """
    declared = {
        name
        for name in AnalysisDraft.__dataclass_fields__
        if name != "confidence_note"  # 它不是"一栏",是一句话
    }

    assert set(ANALYSIS_FIELD_ORDER) == declared

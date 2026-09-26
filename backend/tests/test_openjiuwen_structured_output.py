"""用**真的 openJiuwen SDK** 跑一遍结构化输出,确认 `brief` 不会被它悄悄清空。

## 这里防的是一个"静默删数据"的真实回归

实测现象:模型原文里明明有完整的规划条件 ——

    "brief": {"goal": {"value": "三个月内完成一个能跑起来的 Python 小项目",
                       "source": "user_stated"}, "deadline": {...}, ...}

而落库前拿到的是 `{}`。于是 `planning_briefs` 一行都没有,scheduler 拿**默认容量
档案**排了 46 场,而不是用户亲口说的每周 6 小时 —— 那正好违反"不许替用户决定
每周能投入多少小时"。全程没有异常、没有日志。

根因在 `OutputFormatter._extract_configured_fields`:它会把任何 dict 型已声明字段里
**没列进 `properties`** 的键逐个 pop 掉(只 pop 一层)。`OUTPUT_CONFIG["brief"]` 当时
没声明 `properties`,于是模型写的六个键全被当成"多余字段"删了。`actions` 因为是个
列表(`isinstance(v, dict)` 为假)而毫发无伤 —— 这就是为什么只有 brief 出事。

## 为什么必须真的导入 SDK(而不是复刻那段裁剪逻辑)

复刻一遍那个循环再断言"我的复刻没删掉 brief"是没有意义的:它测的是**我对 SDK 的
理解**,而不是 SDK 的行为,而这次出错的恰恰是我的理解。只有真的跑一次
`OutputFormatter.format_response` 才算数。

## 为什么在**子进程**里跑

`test_openjiuwen_adapter.py` 里有一条断言:`"openjiuwen" not in sys.modules`
(适配器不许在 import 期拖起 SDK —— 导入它要几秒并连带 transformers / tiktoken)。
同一个进程里导入过 SDK 之后,那条断言就再也不成立了,而失效方式很隐蔽:
**取决于用例执行顺序**,单跑绿、全量红。

所以把 SDK 关进子进程:父进程的 `sys.modules` 始终干净,那条断言继续有效。
子进程报告 openjiuwen 不存在时按 skip 处理 —— 没有 key、没有 SDK 也要能跑完整套测试,
这是这个仓库的结构性要求。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agent.runtime.response import BRIEF_FIELD_ORDER

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 子进程退出码:openjiuwen 没装。用它把"跑不了"和"跑失败"区分开 ——
#: 混在一起会让"本机没装 SDK"看起来像"结构化输出坏了"。
_EXIT_NO_SDK = 3

#: 一份**真实的**模型输出:六个条件齐全,外加一个模型自创的字段。
#: 自创字段是用来确认 SDK 的清理**仍然生效**的 —— 修 brief 不能把"闭集"一起修没了。
_MODEL_OUTPUT: dict[str, Any] = {
    "reply": "收到,条件齐了。",
    "brief": {
        "goal": {"value": "三个月内完成一个能跑起来的 Python 小项目", "source": "user_stated"},
        "deadline": {"value": "2026-12-26", "source": "model_assumed"},
        "weekly_available_minutes": {"value": 360, "source": "user_stated"},
        "current_level": {"value": "会基础语法和一点函数", "source": "user_stated"},
        "success_criteria": {"value": "有一个能演示的项目", "source": "model_assumed"},
        "constraints": {"value": ["每周只有周末能写"], "source": "user_stated"},
        "priority": {"value": "模型自创的字段", "source": "model_assumed"},
    },
    "actions": [{"op": "create_node", "ref": "n1", "title": "打基础"}],
}

#: 一轮只问到 goal 的回复。条件是一轮轮问出来的,这是**最常见**的中间状态。
_PARTIAL_OUTPUT: dict[str, Any] = {
    "reply": "先问一句,你每周大概能投入多少时间?",
    "brief": {"goal": {"value": "做个项目", "source": "user_stated"}},
}

#: 一轮什么都没读出来的回复 —— 模型只是回答了用户的问题。
_ABSENT_OUTPUT: dict[str, Any] = {"reply": "先问一句,你每周大概能投入多少时间?"}

_PROBE = """
import json, sys
sys.path.insert(0, {repo_root!r})

try:
    from openjiuwen.core.workflow.components.llm.llm_comp import (
        OutputFormatter,
        SchemaGenerator,
    )
except ImportError:
    raise SystemExit({no_sdk})

from backend.agent.runtime.openjiuwen_runtime import OUTPUT_CONFIG

inputs = json.loads(sys.stdin.read())

def run(payload):
    raw = json.dumps(payload, ensure_ascii=False)
    return OutputFormatter.format_response(raw, {{"type": "json"}}, OUTPUT_CONFIG)

full = run(inputs["full"])
partial = run(inputs["partial"])
absent = run(inputs["absent"])

print(json.dumps({{
    "full_brief": full.get("brief"),
    "full_reply": full.get("reply"),
    "full_actions": full.get("actions"),
    "partial_brief": partial.get("brief"),
    "absent_brief": absent.get("brief"),
    "schema": SchemaGenerator.generate_json_schema(OUTPUT_CONFIG),
}}, ensure_ascii=False))
"""


@pytest.fixture(scope="module")
def sdk_roundtrip() -> dict[str, Any]:
    """在子进程里用真 SDK 跑一遍,把结果拿回来。

    模块级:子进程要几秒(导入 openjiuwen 的代价),而几条断言读的是同一份结果。
    """
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(repo_root=str(REPO_ROOT), no_sdk=_EXIT_NO_SDK)],
        input=json.dumps(
            {"full": _MODEL_OUTPUT, "partial": _PARTIAL_OUTPUT, "absent": _ABSENT_OUTPUT},
            ensure_ascii=False,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        check=False,
    )

    if completed.returncode == _EXIT_NO_SDK:
        pytest.skip("这台机器没装 openjiuwen —— 结构化输出的形状无法在此验证")

    assert completed.returncode == 0, (
        "在真 SDK 上跑结构化输出失败了。子进程 stderr:\n"
        f"{completed.stderr[-4000:]}"
    )

    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_every_brief_field_survives_the_sdk(sdk_roundtrip: dict[str, Any]) -> None:
    """六个条件必须**一个不少**地穿过 SDK。

    这条就是那个 bug 的回归:声明里漏了 `properties`,SDK 会把它们逐个 pop 掉,
    而返回值看起来完全正常(是个 `{}`,不是 `None`,也不抛异常)。
    """
    brief = sdk_roundtrip["full_brief"]
    assert brief, f"brief 又被清空了 —— 模型写的条件一个都没剩下: {brief!r}"
    assert set(brief) == set(BRIEF_FIELD_ORDER), f"brief 的键不对: {sorted(brief)}"

    # 逐字段比对**值本身**,不只比键:被"保留成 None"或"截断成空"同样是丢数据。
    for field in BRIEF_FIELD_ORDER:
        assert field in brief, f"brief.{field} 没穿过来"
        expected = _MODEL_OUTPUT["brief"][field]
        assert brief[field] == expected, f"brief.{field} 的内容变了: {brief[field]!r}"


def test_weekly_minutes_arrive_as_the_user_stated_number(sdk_roundtrip: dict[str, Any]) -> None:
    """单独钉住那个字段,因为它是**唯一会被 scheduler 当容量用**的一个。

    其余五个字段读错了顶多是描述不准,这个读错了会直接改变每周排多少场
    (实测:缺了它 scheduler 用默认档案排出 46 场,而用户说的是每周 6 小时)。
    """
    claim = sdk_roundtrip["full_brief"]["weekly_available_minutes"]
    assert claim["value"] == 360, f"每周可投入被改了: {claim!r}"
    assert claim["source"] == "user_stated", "用户亲口说的被标成了模型假设"


def test_an_invented_field_is_still_dropped(sdk_roundtrip: dict[str, Any]) -> None:
    """闭集仍然有效:模型自创的 `priority` 不该进来。

    修 brief 的顺手写法是"把 `properties` 整个去掉"或"声明成什么都能装",
    那会把这道闸一起拆掉。所以这里正面确认它还在。
    """
    assert "priority" not in sdk_roundtrip["full_brief"], (
        "模型自创的字段混进来了 —— brief 的闭集闸门失效了"
    )


def test_a_partial_brief_is_not_a_failure(sdk_roundtrip: dict[str, Any]) -> None:
    """条件是一轮轮问出来的,只问到 goal 时**必须能存下来**。

    六个字段如果被声明成 `required`,模型早问一句就会让整轮回复陪葬 ——
    用户看到的是"模型输出格式不对",而模型其实干得好好的。
    """
    assert sdk_roundtrip["partial_brief"] == _PARTIAL_OUTPUT["brief"]


def test_a_turn_without_a_brief_is_not_a_failure(sdk_roundtrip: dict[str, Any]) -> None:
    """模型只是回答一句话、这轮没读出任何条件,是**正常**的一轮。

    没声明 `required: False` 的话,SDK 按 `field_config.get("required", True)` 处理,
    每次闲聊之后都会变成一次降级。
    """
    assert not sdk_roundtrip["absent_brief"], "这轮没有 brief,不该凭空造一个出来"
    assert sdk_roundtrip["full_reply"], "reply 必须还在"
    assert sdk_roundtrip["full_actions"], "actions 必须还在"


def test_the_generated_schema_declares_the_same_fields(sdk_roundtrip: dict[str, Any]) -> None:
    """给模型看的那份 schema 与 `BRIEF_FIELD_ORDER` 是同一组。

    这两个地方一旦漂移,症状是"模型忽然不写某个条件了":schema 里没有它,
    模型就得不到提示,而 SDK 那边又会因为它不在 `properties` 里把它删掉。
    """
    schema = sdk_roundtrip["schema"]
    declared = schema["properties"]["brief"]["properties"]
    assert set(declared) == set(BRIEF_FIELD_ORDER), (
        f"schema 里的 brief 字段与 BRIEF_FIELD_ORDER 对不上: {sorted(declared)}"
    )

    # 六个字段都不是必填 —— 否则"只问到 goal"的那一轮会被 SDK 判成失败。
    assert "brief" not in schema["required"], "brief 整个字段不该是必填"
    assert "required" not in declared["goal"], "单个条件被写成必填了"

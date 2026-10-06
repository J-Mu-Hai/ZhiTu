"""openJiuwen 适配器的**离线**测试。

## 这个文件在防什么

适配器是整个后端唯一一处直接对着第三方 SDK 的早期 API 写死签名的地方
(`LLMCompConfig(...)` 的字段名、`outputs_schema` 的 `${...}` 引用语法、`End` 节点的
`inputs_schema`)。这类代码的失败方式很难看:**它不报错,它安静地返回 None**。

两条已经踩过的坑就是证据,所以这里各钉一条:

1. `output_config` 里不写 `required`,SDK 的 `_extract_configured_fields` 就按必填处理
   (`field_config.get("required", True)`)—— 于是"模型这轮没提变更"这种完全正常的
   回复会变成一次降级。见 `test_optional_fields_are_declared_optional`。
2. `End` 节点少了 `inputs_schema`,`WorkflowOutput.result` 会是 `None`,而 `state`
   照样是 `COMPLETED` —— 一次成功的模型调用凭空消失,日志里什么异常都没有。
   见 `test_end_node_receives_the_component_output`。

## 为什么全是离线的

本文件**不导入 openjiuwen**,也**不发任何请求**:

- 导入它要几秒,还会注册一堆连接器与文档解析器 —— 让单元测试为此变慢没有道理;
- conftest 里有 autouse 的 `_block_real_network`,真出网会直接失败;
- 而没有 key 也能跑完整套测试,是这个仓库的结构性要求,不是巧合。

`_build_flow` 因此**不被调用**(它里面才有 `import`)。这里测的是它**依赖的那些决定**
—— 配置常量、拆包规则、降级映射 —— 加上一个不碰 SDK 的装配判断。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from backend.agent.runtime import build_reasoner
from backend.agent.runtime.base import KnownConditions, Reasoner, TurnContext
from backend.agent.runtime.direct_llm import DirectLLMReasoner
from backend.agent.runtime.openjiuwen_runtime import (
    OUTPUT_CONFIG,
    OpenJiuwenReasoner,
    available,
    reset_availability_cache,
    reset_runner_state,
)
from backend.agent.runtime.response import PayloadInvalid
from backend.agent.runtime.rule_fallback import RuleFallbackReasoner
from backend.db.models.enums import DegradedReason, ModelSource

# --------------------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_module_state() -> Iterator[None]:
    """`available()` 与 Runner 启动标记都是**进程级**缓存,逐个用例清干净。

    不清的话,用例之间会通过模块全局互相影响,而那种失败看起来像"这条测试本身
    时好时坏"。
    """
    reset_availability_cache()
    reset_runner_state()
    yield
    reset_availability_cache()
    reset_runner_state()


# `settings_factory` 现在住在 `conftest.py` —— 装配 reasoner 的用例不止这一个模块
# (`test_scripted_reasoner.py` 也要),而两份定义迟早会分叉。它仍然是同一个 fixture:
# 造一份不读 `.env`、不受进程环境影响的配置。


def test_available_only_asks_whether_the_package_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`available()` 查 spec,而且**只查一次**。

    它是每次装配 reasoner 都会走的判断,决定界面上那个徽标写什么。如果它顺手把包
    导进来(几秒 + 一堆连接器注册),那么每个读到 `/plan` 的请求都要付这个代价 ——
    哪怕这个请求根本不碰模型。
    """
    calls: list[str] = []
    real_find_spec = importlib.util.find_spec

    def counting_find_spec(name: str, *args: Any, **kwargs: Any):
        calls.append(name)
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", counting_find_spec)

    first = available()
    second = available()
    third = available()

    assert first is second is third, "缓存没生效,每次都重查了"
    assert calls == ["openjiuwen"], f"查了不止一次: {calls}"


def test_available_treats_a_broken_parent_package_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`find_spec` 抛异常 = 用不了,不是"程序错了"。

    它在父包损坏时抛 `ImportError`、在被当作命名空间包的一部分时抛 `ValueError`。
    这两种情况下正确的结果都是"这条路走不通,换直连",而不是让整个请求 500。
    """

    def broken(name: str, *args: Any, **kwargs: Any):
        raise ImportError("父包坏了")

    monkeypatch.setattr(importlib.util, "find_spec", broken)
    assert available() is False


def test_build_reasoner_prefers_openjiuwen_when_it_is_there(
    monkeypatch: pytest.MonkeyPatch, settings_factory
) -> None:
    """装了就用它 —— 它是这个产品的既定技术路线,直连只是退路。"""
    monkeypatch.setattr("backend.agent.runtime.available", lambda: True)

    reasoner = build_reasoner(settings_factory(agent_reasoner="auto", llm_api_key="k"))

    assert isinstance(reasoner, OpenJiuwenReasoner)


def test_build_reasoner_falls_back_to_the_direct_path(
    monkeypatch: pytest.MonkeyPatch, settings_factory
) -> None:
    """没装就走直连。**徽标因此会写"直连模型"** —— 不把直连说成 openJiuwen。"""
    monkeypatch.setattr("backend.agent.runtime.available", lambda: False)

    reasoner = build_reasoner(settings_factory(agent_reasoner="auto", llm_api_key="k"))

    assert isinstance(reasoner, DirectLLMReasoner)
    assert not isinstance(reasoner, OpenJiuwenReasoner)


def test_no_key_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, settings_factory
) -> None:
    """没有 key 时**不管配了什么**都退回规则兜底。

    这条优先于 `AGENT_REASONER`:指定了 `openjiuwen` 但没有 key,给一个每次都
    "密钥无效"的实现,用户得到的是一个死胡同;规则兜底至少能把缺的条件问出来。
    """
    monkeypatch.setattr("backend.agent.runtime.available", lambda: True)

    for choice in ("auto", "openjiuwen", "direct"):
        reasoner = build_reasoner(settings_factory(agent_reasoner=choice, llm_api_key=""))
        assert isinstance(reasoner, RuleFallbackReasoner), f"{choice} 没有退回规则兜底"


def test_rule_choice_ignores_everything_else(settings_factory) -> None:
    """显式要规则兜底时,连查都不查 openJiuwen。"""
    reasoner = build_reasoner(settings_factory(agent_reasoner="rule", llm_api_key="k"))
    assert isinstance(reasoner, RuleFallbackReasoner)


def test_the_adapter_satisfies_the_reasoner_seam(settings_factory) -> None:
    """接缝是结构性的:`reason` 必须存在且是协程。

    测试替换整个实现靠的是 `app.dependency_overrides`,那是鸭子类型 —— 一个名字
    拼错的方法要等到第一次真请求才炸。
    """
    import inspect

    reasoner = OpenJiuwenReasoner(settings_factory(llm_api_key="k"))
    assert isinstance(reasoner, Reasoner)
    assert inspect.iscoroutinefunction(reasoner.reason)


# --------------------------------------------------------------------------------------
# 输出形状声明 —— 两条实测踩过的坑
# --------------------------------------------------------------------------------------


def test_optional_fields_are_declared_optional() -> None:
    """`brief` / `actions` 必须**显式**写 `required: False`。

    这是实测踩出来的,不是审美。openJiuwen 的 `_extract_configured_fields` 读的是
    `field_config.get("required", True)` —— **不写就是必填**。而"模型这轮只是回答了
    一句话,没有提任何变更"是完全正常的一种回复:把它当失败,用户会在每次闲聊之后
    看到一次"模型输出格式不对"。

    所以这里断言的恰恰是"那个键存在",而不是"它是 False" —— 只有前者能拦住
    "有人把它删了以为默认就是可选"。
    """
    for field in ("brief", "actions"):
        assert field in OUTPUT_CONFIG, f"{field} 不在输出声明里了"
        assert "required" in OUTPUT_CONFIG[field], (
            f"{field} 没写 required —— SDK 会按**必填**处理,而它其实是可选的"
        )
        assert OUTPUT_CONFIG[field]["required"] is False

    assert OUTPUT_CONFIG["reply"].get("required") is True, "reply 是必填的"


def test_the_output_declaration_matches_what_the_parser_reads() -> None:
    """声明给 SDK 的键,与解析器真正会读的键是同一组。

    SDK 按 `output_config` 裁剪模型输出,解析器再从裁剪完的对象里读。两处各写一份
    就会漂移,而漂移的方向很难发现:被裁掉的键在解析器那边看起来就是"模型没给",
    于是安静地少了一块 —— 没有异常,没有日志。

    **对照的那一半取自 `PARSED_PAYLOAD_FIELDS`,不是这个文件里手写的载荷。**
    这条测试原来的写法是自己列三个键、再断言它等于 `OUTPUT_CONFIG` —— 于是
    "解析器会读 `analysis`、但声明里没有"这件事它一个字都说不出来:同一个遗漏
    写在了两处,看起来就是一致的。这正是 C 批漏掉 `analysis` 却全绿的原因。
    """
    from backend.agent.prompts.planning import PROMPT_VERSION
    from backend.agent.runtime.response import PARSED_PAYLOAD_FIELDS, payload_to_result

    assert set(OUTPUT_CONFIG) == set(PARSED_PAYLOAD_FIELDS), (
        "给 SDK 的输出声明与解析器会读的键对不上了 —— 少写的那一边会让模型给的"
        "内容在到解析器之前就被裁掉,而且不报错"
    )

    payload = {
        "reply": "好。",
        # brief 的每一项都是 `{value, source}` —— 不是裸值。标签那一半是**防未确认
        # 的值进确认字段**的东西,所以这里照真实形状写。
        "brief": {"weekly_available_minutes": {"value": 360, "source": "user_stated"}},
        "actions": [{"op": "create_node", "ref": "n1", "title": "打基础"}],
        "questions": [
            {
                "question": "重心放哪边?",
                "whyNow": "它决定先拆哪边",
                "responseMode": "free_text",
                "options": [],
                "allowCustomInput": True,
            }
        ],
        "toolRequests": [
            {"id": "t1", "name": "get_node", "arguments": {"handle": "n1"}, "reason": "看看正文"}
        ],
        "stopReason": "ready_to_propose",
        "analysis": {"known": ["n1 的正文里写着只能周末做"], "diagnosis": ["缺一个时长"]},
        "reasoningMap": {
            "phase": "strategic_exploration",
            "focus": "r1",
            "nodes": [{"handle": "r1", "title": "目标用途", "nodeType": "dimension"}],
            "links": [],
        },
        "intakeDecision": {
            "action": "ask",
            "question": "你最希望先得到什么可验证成果?",
            "decisionScope": "deliverable",
            "whyThisMatters": "它决定阶段 1 交什么",
            "quickReplies": ["一个能展示的作品"],
        },
        "v1Assessment": {
            "globalAssessment": "Python 是手段而不是成果。",
            "nodeUpdates": [
                {
                    "nodeKey": "true_intent",
                    "judgment": "真实诉求还不明确。",
                    "knownFacts": [],
                    "assumptions": ["（AI 假设）可能为了求职"],
                }
            ],
            "focusKey": "true_intent",
            "focusReason": "它最影响路线。",
            "question": "你想拿出什么具体成果?",
            "strategyTradeoff": "",
            "strategyReady": False,
        },
        "v1Timeline": {
            "summary": "基础 → 项目 → 展示",
            "phases": [{"title": "基础", "startWeek": 1, "endWeek": 2}],
        },
        "v1Strategy": {
            "mainLine": "先做最小闭环",
            "parallelLine": "并行看一点统计",
            "deferOrAvoid": "暂不系统学算法",
            "riskControl": "每两周复盘",
            "tradeoff": "先要能展示的成果",
        },
        "v1TimelineAlignment": {
            "summary": "按每周一个闭环推进",
            "totalSpan": "约 4 周",
            "cadence": "每周一个可验收小闭环",
            "phaseCount": 3,
            "biggestRisk": "投入不稳定",
            "assumptions": [{"text": "用户想尽快出成果", "source": "user_fact"}],
            "question": "",
            "options": [],
        },
    }
    assert set(payload) == set(OUTPUT_CONFIG), (
        "载荷的键与给 SDK 的输出声明对不上了 —— 有一边多写或少写了"
    )

    result = payload_to_result(
        payload, source=ModelSource.OPENJIUWEN, request_id="r", prompt_version=PROMPT_VERSION
    )

    assert result.degraded is False, "键齐全的载荷必须是成功的"
    assert [c.field for c in result.brief_claims] == ["weekly_available_minutes"]
    assert len(result.actions) == 1
    assert len(result.questions) == 1, "解析器读了 questions,结果里就必须有它"
    assert result.questions[0].response_mode == "free_text"
    assert len(result.tool_requests) == 1, "解析器读了 toolRequests,结果里就必须有它"
    assert result.tool_requests[0].name == "get_node"
    assert result.stop_reason == "ready_to_propose"
    assert result.analysis is not None, "解析器读了 analysis,结果里就必须有它"
    assert result.analysis.known == ("n1 的正文里写着只能周末做",)
    assert result.reasoning_map is not None, "解析器读了 reasoningMap,结果里就必须有它"
    assert result.reasoning_map.focus_handle == "r1"
    assert result.v1_strategy is not None, "解析器读了 v1Strategy,结果里就必须有它"
    assert result.v1_strategy.main_line == "先做最小闭环"


def test_the_forwarding_schemas_carry_every_declared_key() -> None:
    """两张转发表必须带上**声明过的每一个**键,一个不少。

    这一条钉的是真实模型验收抓出来的那个缺陷:`OUTPUT_CONFIG`、组件输出、`End` 输入
    是三处独立写下的键名,而 openJiuwen 那条路上的载荷是**照这三处重建**的。C 批给
    提示词和解析器都加了 `analysis`,唯独没加这三处里的后两处 —— 模型每轮都认真
    给出七栏判断,`node_analyses` 却一行都没有,全程没有一个异常或一条日志。

    断言的是"集合相等"而不是"包含":多出来一个键同样有问题(`End` 会收到一个
    组件根本没产出的引用,`WorkflowOutput.result` 变成 None,一次成功的调用凭空消失)。
    """
    from backend.agent.runtime.openjiuwen_runtime import (
        component_outputs_schema,
        end_inputs_schema,
    )

    outputs = component_outputs_schema()
    inputs = end_inputs_schema()

    assert set(outputs) == set(OUTPUT_CONFIG), (
        "组件输出表与输出声明对不上 —— 少掉的那个键在模型那边给了也没用"
    )
    assert set(inputs) == set(OUTPUT_CONFIG), "End 输入表与输出声明对不上"

    # 逐条比引用串的写法:两处的 `${}` 语法不一样(`${x}` 对 `${planning.x}`),
    # 写反了不会报错,只会让那个键取到 None。
    for field in OUTPUT_CONFIG:
        assert outputs[field] == f"${{{field}}}"
        assert inputs[field] == f"${{planning.{field}}}"

    assert "analysis" in outputs, (
        "analysis 不在组件输出表里 —— 这正是让整层分析记录一行都不落的那个遗漏"
    )


def test_the_workflow_identity_is_stable() -> None:
    """工作流的 (id, version) 是 openJiuwen 的注册键,**不能是随机值**。

    随机 id 会让每次调用都注册一张新图;而版本号是 SDK 侧做缓存与追踪的依据。
    """
    from backend.agent.runtime.openjiuwen_runtime import WORKFLOW_ID, WORKFLOW_VERSION

    assert WORKFLOW_ID == "zhitu_planning"
    assert WORKFLOW_VERSION and WORKFLOW_VERSION[0].isdigit()


# --------------------------------------------------------------------------------------
# 拆包
# --------------------------------------------------------------------------------------


class _FakeWorkflowOutput:
    """复刻实测到的形状:`WorkflowOutput(result={"output": {...}}, state=...)`。

    外面那层 `output` 是 `End` 组件包的 —— 它把自己收到的输入裹了进去。
    """

    def __init__(self, result: Any) -> None:
        self.result = result
        self.state = "COMPLETED"


def test_payload_of_peels_the_end_node_wrapper() -> None:
    """逐层剥到模型那个对象上。"""
    from backend.agent.runtime.openjiuwen_runtime import _payload_of

    inner = {"reply": "好的。", "brief": None, "actions": []}
    assert _payload_of(_FakeWorkflowOutput({"output": inner})) == inner
    # 没有那层包裹时也要能剥(小版本换个包装方式不该让整次调用变成"格式错误")
    assert _payload_of(_FakeWorkflowOutput(inner)) == inner
    # 裸 dict 同样接受
    assert _payload_of(inner) == inner


def test_payload_of_refuses_shapes_it_cannot_read() -> None:
    """读不出来要抛 `PayloadInvalid`,而不是返回 None 让调用方继续。

    返回 None 的话,失败会在更远的地方以"reply 缺失"的样子出现,而那一层已经
    不知道真正的原因了。
    """
    from backend.agent.runtime.openjiuwen_runtime import _payload_of

    with pytest.raises(PayloadInvalid):
        _payload_of(_FakeWorkflowOutput(None))
    with pytest.raises(PayloadInvalid):
        _payload_of(_FakeWorkflowOutput({"output": 42}))
    with pytest.raises(PayloadInvalid):
        _payload_of(_FakeWorkflowOutput(["不是对象"]))
    with pytest.raises(PayloadInvalid):
        _payload_of(None)


def test_a_completed_workflow_with_no_result_is_not_a_success(
    settings_factory,
) -> None:
    """`state=COMPLETED` 但结果是 None,必须是**失败**,不能当成"模型没提变更"。

    这正是 `End` 节点少写 `inputs_schema` 时的现象:一次成功的模型调用凭空消失,
    而 `state` 一切正常。如果这里被当成成功,用户会看到一句空回复。
    """
    reasoner = OpenJiuwenReasoner(settings_factory(llm_api_key="k"))

    result = reasoner._parse(
        _FakeWorkflowOutput(None), request_id="req-1", latency_ms=5
    )

    assert result.degraded is True
    assert result.degraded_reason is DegradedReason.MODEL_OUTPUT_INVALID
    assert result.retryable is True, "这是可重试的,不是'模型不行'"
    assert result.source is ModelSource.UNAVAILABLE


def test_an_empty_reply_is_not_a_success(settings_factory) -> None:
    """模型给出空回复也算没解析出来 —— 空字符串对用户等于没有回复。"""
    reasoner = OpenJiuwenReasoner(settings_factory(llm_api_key="k"))

    result = reasoner._parse(
        _FakeWorkflowOutput({"output": {"reply": "   ", "actions": []}}),
        request_id="req-1",
        latency_ms=5,
    )

    assert result.degraded is True
    assert result.degraded_reason is DegradedReason.MODEL_OUTPUT_INVALID


def test_a_good_payload_becomes_a_successful_result(settings_factory) -> None:
    """形状对了就是成功,而且 `source` 必须写成 openjiuwen。"""
    reasoner = OpenJiuwenReasoner(settings_factory(llm_api_key="k"))

    result = reasoner._parse(
        _FakeWorkflowOutput(
            {
                "output": {
                    "reply": "先按每周 6 小时排三周。",
                    "brief": {"weekly_available_minutes": {"value": 360, "source": "user_stated"}},
                    "actions": [{"op": "create_node", "ref": "n1", "title": "打基础"}],
                }
            }
        ),
        request_id="req-1",
        latency_ms=12,
    )

    assert result.degraded is False
    assert result.source is ModelSource.OPENJIUWEN
    assert result.reply == "先按每周 6 小时排三周。"
    assert result.latency_ms == 12
    assert len(result.actions) == 1


# --------------------------------------------------------------------------------------
# 降级映射
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason", "retryable"),
    [
        ("HTTPError: 401 Unauthorized", DegradedReason.MODEL_AUTH_FAILED, False),
        ("AuthenticationError: invalid api key", DegradedReason.MODEL_AUTH_FAILED, False),
        ("RateLimitError: 429 Too Many Requests", DegradedReason.MODEL_RATE_LIMITED, True),
        ("openjiuwen BaseError: rate limit exceeded", DegradedReason.MODEL_RATE_LIMITED, True),
        ("ReadTimeout: request timed out", DegradedReason.MODEL_TIMEOUT, True),
        ("ConnectionError: 连接被重置", DegradedReason.MODEL_UNAVAILABLE, True),
    ],
)
def test_sdk_errors_map_to_what_the_user_should_see(
    settings_factory, text: str, reason: DegradedReason, retryable: bool
) -> None:
    """SDK 把上游的 401/429/超时都包在它自己的异常里,类型分不出来,只能按文本认。

    **认不出来时按"暂时不可用 + 可重试"归类** —— 宁可让用户点一次注定失败的重试,
    也不要把一次超时说成"密钥错了"(那会让他去找管理员,而什么也没坏)。
    """
    reasoner = OpenJiuwenReasoner(settings_factory(llm_api_key="k"))

    result = reasoner._failed(RuntimeError(text), request_id="req-1", started=None)

    assert result.degraded is True
    assert result.degraded_reason is reason
    assert result.retryable is retryable
    assert result.reply.strip(), "降级了也得给用户一句话,不能是空字符串"


#: 一次成功的规划会说的话。降级回复里出现任何一个,这条降级就白标注了 ——
#: 用户看到的是回复本身,不是响应里的 `degraded` 字段。
_PLAN_CLAIM_WORDS = ("已为你", "已经排好", "已排好", "计划如下", "为你生成")


def test_the_degraded_reply_never_pretends_to_have_planned(settings_factory) -> None:
    """降级时给用户的那句话,不能说成"排好了"。"""
    reasoner = OpenJiuwenReasoner(settings_factory(llm_api_key="k"))

    cases = [
        RuntimeError("502 Bad Gateway"),
        TimeoutError("timeout"),
        RuntimeError("401 Unauthorized"),
        RuntimeError("429 rate limit"),
    ]
    for exc in cases:
        result = reasoner._failed(exc, request_id="req-1", started=None)
        assert result.reply.strip(), "降级了也得给用户一句话,不能是空字符串"
        for word in _PLAN_CLAIM_WORDS:
            assert word not in result.reply, f"{exc!r} 的回复听起来像成功了: {result.reply}"


@pytest.mark.asyncio
async def test_no_key_degrades_without_a_network_call(settings_factory) -> None:
    """没 key 时 `reason()` 直接降级,**一个字节都不发**。

    留着这条是因为"没 key 不该出网"很容易被一次重构破坏(比如把 key 检查挪到
    `_build_flow` 之后),而破坏之后在开发机上完全看不出来 —— 你的 `.env` 里有 key。
    conftest 的 `_block_real_network` 是这里真正的执行者。
    """
    reasoner = OpenJiuwenReasoner(settings_factory(llm_api_key=""))
    turn = TurnContext(
        current_date="2026-09-26",
        weekday="星期六",
        timezone="Asia/Shanghai",
        workspace_title="Python 学习",
        workspace_intent="三个月内完成一个项目",
        known=KnownConditions(weekly_available_minutes=360),
    )

    result = await reasoner.reason(turn)

    assert result.degraded is True
    assert result.degraded_reason is DegradedReason.NO_API_KEY
    assert result.retryable is False, "没 key 是配置问题,重试没有意义"
    assert result.source is ModelSource.UNAVAILABLE


def test_the_adapter_never_imports_openjiuwen_at_module_load() -> None:
    """**模块导入本身绝不碰 openjiuwen。**

    这条测试在整套用例跑完之后跑(或跑之前),两种顺序都会红:只要这个模块在
    import 时执行了 `import openjiuwen`,它就会出现在 `sys.modules` 里。

    这不是洁癖:导入 openjiuwen 实测要几秒并连带拖起 transformers / tiktoken 等
    一堆重量级依赖,而**每一次冷启动**都付这个代价,哪怕这次请求根本不走模型。
    """
    assert "openjiuwen" not in sys.modules, (
        "openjiuwen 被导入了 —— 适配器必须用 find_spec 判断、在 reason() 里才真导入"
    )


def test_v1_strategy_turn_uses_the_v1_strategy_prompt() -> None:
    """阶段一“想清楚”的战略判断回合必须用 v1_strategy 提示词,不是 planning 默认。

    这条钉的是一个很难发现的漏分支:direct_llm 有 `v1_strategy` 分支,但
    openjiuwen 的 `_system_prompt` 曾经没有 —— 于是真实模式(默认路径)落到
    planning 提示词,“先给判断再决定要不要问”的行为规则静默失效。
    """
    from backend.agent.prompts.v1_strategy import (
        V1_STRATEGY_PROMPT_VERSION,
        V1_STRATEGY_SYSTEM_PROMPT,
    )
    from backend.agent.runtime.openjiuwen_runtime import _prompt_version, _system_prompt

    turn = TurnContext(
        current_date="2026-10-06",
        weekday="星期二",
        timezone="Asia/Shanghai",
        workspace_title="Python 学习",
        workspace_intent="",
        known=KnownConditions(),
        purpose="v1_strategy",
    )
    assert _system_prompt(turn) == V1_STRATEGY_SYSTEM_PROMPT
    assert _prompt_version(turn) == V1_STRATEGY_PROMPT_VERSION


def test_v1_prompt_and_sdk_instruction_forbid_json_null() -> None:
    """回归：`criticalQuestion: null` 会在 SDK 的 string 校验中中断整轮。"""
    from backend.agent.prompts.v1_strategy import V1_STRATEGY_SYSTEM_PROMPT
    from backend.agent.runtime.openjiuwen_runtime import _STRICT_JSON_INSTRUCTION

    assert "留空或 null" not in V1_STRATEGY_SYSTEM_PROMPT
    assert "严格禁止输出 JSON 的 `null`" in V1_STRATEGY_SYSTEM_PROMPT
    assert "Never emit JSON null" in _STRICT_JSON_INSTRUCTION
    assert 'empty string ""' in _STRICT_JSON_INSTRUCTION

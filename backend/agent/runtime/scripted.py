"""**测试脚手架**:按一份写好的脚本回应,不连任何模型。

## 它解决的是什么问题

访谈共建那条路(§2.5:用户答了"我排名 38",模型把这件事记成一个信息主题)在隔离栈里
**走不通**:`RuleFallbackReasoner` 永远返回 `actions=()`(那是它的全部意义 —— 不编造
计划内容),而 `conftest.FakeReasoner` 住在测试进程里,跑起来的前端 app import 不到。
于是"确认 → 画布上看见整理结果"这件事只有真实模型那一层能验,而那一层要密钥、
要出网、还会因为模型这次的心情而飘。

这个模块补的就是那一格:让一份**事先写好**的动作序列从 HTTP 接口走一遍。

## 三道防止它被误认成产品能力的闸

1. **名字。** `AGENT_REASONER=script`,枚举值是 `ModelSource.SCRIPTED`,落库、进响应、
   进对话徽标 —— 界面上写着"脚本",不会有人把它读成"AI 规划"。
2. **必须显式配两个环境变量。** 光设 `AGENT_REASONER=script` 而没设
   `ZHITU_SCRIPTED_ACTIONS` 会**在启动时直接抛错**,不是安静地退回规则兜底:
   退回兜底的话,一个"忘了配脚本"的验收会以"模型什么都没提"的方式通过,而那是
   最贵的一种假绿。`auto` / `direct` / `openjiuwen` / `rule` 的语义一个都没变。

   "启动时"这三个字由 `api/main.py::lifespan` 兑现:`get_reasoner` 是**懒构造**的
   (第一次需要推理的请求才建它),所以只把校验放在 `build_reasoner` 里的话,一个
   配坏的脚本会在**第一条消息**上炸成 500。而跨域的浏览器把"500 且响应里没有 CORS
   头"显示成「连不上后端服务,请确认后端已经启动」—— 验收的人于是去查进程,而进程
   活得好好的。所以配置错必须在进程起来的那一刻就说出来。
3. **它不假装降级。** `degraded=False` —— 它不是"模型不可用",它就是按配置在干活。
   把 `degraded` 置真的后果不只是徽标多一句话:`contracts/conversation.py` 约定
   "降级时不会有提案",而这条路要的恰恰是提案。

## 脚本的形状

`ZHITU_SCRIPTED_ACTIONS` 是**一段 JSON**,或者**一份写着这段 JSON 的文件的路径**
(`accept-e2e.mjs --script=<file>` 走的是后一种:让命令行工具不必把整段 JSON 塞进
环境变量)。判据是**先当 JSON 解**:解得开就当 JSON,解不开再看它是不是一个存在的
文件。顺序不能反 —— 反过来的话,一段恰好叫 `123` 的合法 JSON 会被当成文件名,而
那是这一层唯一可能读错的东西。

两种写法都收:

```jsonc
// 每一轮都回同一份(和 conftest.FakeReasoner 一个形状,适合"只验一件事")
{"reply": "我给你排了一版。", "actions": [{"op": "create_node", ...}]}

// 按轮次依次取用,用完之后是**空的一轮**(不提案、一句话都不编)
{"turns": [
  {"reply": "先问一句:你排名多少?",
   "claims": {"current_level": {"value": "大三在读", "source": "user_stated"}}},
  {"reply": "记下来了。", "actions": [{"op": "create_node", ...}]}
]}
```

"用完之后是空的"是**故意**的:重复最后那一轮会让一段多余的对话(比如重试)再提出
同一份动作,而它会被去重合并成第二次更新 —— 于是验收看到的是一个"不知道为什么多出来
的更新",而真正的原因在脚本之外。

### 游标按**空间**分,不按进程分

`get_reasoner` 是 `lru_cache(maxsize=1)`,一个进程里只有一个 reasoner,所以"用到第几轮"
一开始写成了一个进程级计数器。那是错的:一份脚本描述的是**一个空间里**的一段访谈
(`apps/web/tests/fixtures/interview-script.json` 就写死了前两轮不提案、第三轮才把
用户说的事实记下来),而隔离栈里所有 spec 共用同一个后端进程。按进程计数的话,先开口
的那个 spec 会把脚本吃掉,访谈那个 spec 拿到的是"脚本已经跑完了"—— 它的 `.proposal`
于是永远不出现,而那条失败信息看起来像产品坏了(实测见
`artifacts/runs/20260928-001323-8ccfce7`:那个 spec 自己建的空间干净,`proposals` 表却是空的)。

所以游标按**空间**分,钥匙是这一轮所在空间里 `depth == 0` 的那个节点 —— 根目标,
一个空间只有一个(`workspace_service.create_workspace` 建它时写死 `depth=0`)。
同一个空间的第 1、2、3 条消息各取一轮脚本,换个空间从头开始。

## 它走的是真实模型那条解析链

`claims` 与 `analysis` 写的是**模型输出的那个形状**(而不是构造好的 `BriefClaim` /
`AnalysisDraft`),并且交给 `response.parse_claims` / `response.parse_analysis` 清洗 ——
和 `direct_llm` 拿到 JSON 之后走的是同一对函数。这样脚本化的一条断言在"模型给了个
形状不对的 analysis"这件事上表达的意思和真模型那一层是一样的,而不是绕开清洗直接
塞一个内部对象进去。`actions` 同理:逐条原样交给 `proposal_service.build_from_actions`。

脚本绕开的**只有**"谁来想出这些动作"这一步;校验、权限、确认一步都不少。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from backend.agent.runtime.base import ReasoningResult, TurnContext
from backend.agent.runtime.response import (
    parse_analysis,
    parse_claims,
    parse_questions,
    parse_stop_reason,
    parse_tool_requests,
)
from backend.db.models.enums import ModelSource

#: 脚本所在的环境变量:一段 JSON,或一份写着它的文件的路径。名字带 `ZHITU_` 前缀,
#: 和别的开发期开关一致。
SCRIPT_ENV = "ZHITU_SCRIPTED_ACTIONS"

#: 提示词版本。落库的 `prompt_version` 会写它 —— 一条 `scripted-v1` 的消息在库里
#: 一眼就能和真的提示词版本(`planning-v11`)区分开。
PROMPT_VERSION = "scripted-v1"

#: 脚本用完之后那一轮的回复。**不含任何计划内容**,与 `rule_fallback` 同一条纪律:
#: 宁可什么都不说,也不用一句"好的,我来帮你规划"冒充。
_EXHAUSTED_REPLY = "脚本已经跑完了(这是测试脚手架,不会再有新的动作)。"


class ScriptedConfigError(RuntimeError):
    """脚本没配好。**启动时就抛** —— 见模块 docstring 的第 2 条。"""


def parse_script(raw: str) -> tuple[dict, ...]:
    """把 `ZHITU_SCRIPTED_ACTIONS` 解析成"每一轮一个 dict"的序列。空串等于没配。

    值有两种写法:**JSON 本身**,或者**指向一份 JSON 文件的路径**。先当 JSON 解,
    解不开再看它是不是一个存在的文件 —— 顺序见模块 docstring 里那段理由。
    """
    text = (raw or "").strip()
    if not text:
        raise ScriptedConfigError(
            f"AGENT_REASONER=script 需要配套的 {SCRIPT_ENV}(一段 JSON,或一份 JSON 文件的路径)。"
            "缺了它就会静默地什么都不提,而那会让一次验收以'模型什么都没生成'的方式通过。"
        )
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        source = Path(text)
        if source.is_file():
            return parse_script(source.read_text(encoding="utf-8"))
        # 两条路都走不通时才报错,而且**两条都说出来** —— 只说"不是合法的 JSON"的话,
        # 一个把路径写错的人会去改 JSON,而问题在那个路径上。
        raise ScriptedConfigError(
            f"{SCRIPT_ENV} 既不是合法的 JSON({exc}),也不是一个存在的文件:"
            f"{source}。写 JSON,或者写一份写着 JSON 的文件的路径。"
        ) from exc

    if isinstance(loaded, dict) and "turns" in loaded:
        turns = loaded["turns"]
        if not isinstance(turns, list) or not turns:
            raise ScriptedConfigError(f"{SCRIPT_ENV} 的 turns 必须是一个非空数组。")
    elif isinstance(loaded, dict):
        turns = [loaded]
    elif isinstance(loaded, list):
        # 只写动作列表也是一个自然的写法,补一个空的 reply。
        turns = [{"actions": loaded}]
    else:
        raise ScriptedConfigError(
            f"{SCRIPT_ENV} 要么是 {{\"reply\", \"actions\"}},要么是 {{\"turns\": [...]}}。"
        )

    for index, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict):
            raise ScriptedConfigError(f"{SCRIPT_ENV} 第 {index} 轮不是对象。")
        unknown = set(turn) - {
            "reply",
            "claims",
            "actions",
            "analysis",
            "questions",
            "toolRequests",
            "stopReason",
        }
        if unknown:
            # 静默忽略一个拼错的键(比如 `action`)会让整段脚本变成"什么都不提"。
            raise ScriptedConfigError(
                f"{SCRIPT_ENV} 第 {index} 轮里有认不出的键:{'、'.join(sorted(unknown))}。"
                "能写的是 reply / claims / actions / questions / toolRequests / stopReason / analysis。"
            )
    return tuple(turns)


def space_key(turn: TurnContext) -> str:
    """这一轮属于哪个空间 —— 拿根目标的真实 id 当钥匙。

    `TurnContext` 里没有空间 id:那个类**刻意**不给模型任何真实主键(见它的 docstring
    「这里没有 UUID,是刻意的」)。但根目标一定在 `node_handles` 里,而一个空间只有一个
    根 —— 它的 `depth` 是 0(`workspace_service.create_workspace` 写死的),
    `load_nodes` 又按 `depth asc` 排,所以它总是 `nodes` 的第一个。真取不到就返回空串,
    让那些没有节点的调用方共用一格,而不是把整个 reasoner 搞崩。
    """
    ids = dict(turn.node_handles)
    for view in turn.nodes:
        if view.depth == 0:
            return ids.get(view.handle, "")
    return ""


class ScriptedReasoner:
    """按脚本回应。**只由 `build_reasoner` 在 `AGENT_REASONER=script` 时构造。**"""

    def __init__(self, turns: tuple[dict, ...]) -> None:
        self._turns = turns
        #: 每个**空间**各用各的游标。理由见模块 docstring 的「游标按空间分,不按进程分」。
        self._used: dict[str, int] = {}
        #: 收到过的每一轮上下文。与 `conftest.FakeReasoner.calls` 同一个用途:
        #: "这一轮它到底看见了什么"是能查的,而不是靠猜。
        self.calls: list[TurnContext] = []

    @property
    def turns_total(self) -> int:
        return len(self._turns)

    @property
    def turns_used(self) -> int:
        """所有空间加起来用掉的轮数。**只是给断言看的**,推进游标的是 `_used` 里那一格。"""
        return sum(self._used.values())

    @classmethod
    def from_env(cls, raw: str | None = None) -> ScriptedReasoner:
        return cls(parse_script(os.environ.get(SCRIPT_ENV, "") if raw is None else raw))

    async def reason(self, turn: TurnContext) -> ReasoningResult:
        started = time.monotonic()
        self.calls.append(turn)

        key = space_key(turn)
        used = self._used.get(key, 0)
        scripted = self._turns[used] if used < len(self._turns) else {}
        self._used[key] = used + 1

        return ReasoningResult(
            reply=str(scripted.get("reply") or _EXHAUSTED_REPLY),
            source=ModelSource.SCRIPTED,
            # 它不是"模型不可用":它就是按配置在干活。见模块 docstring 第 3 条。
            degraded=False,
            degraded_reason=None,
            retryable=False,
            brief_claims=tuple(parse_claims(scripted.get("claims"))),
            # 逐条原样交给校验链 —— 这一行**不做任何加工**,包括不去猜哪些动作是合法的。
            actions=tuple(scripted.get("actions") or ()),
            questions=tuple(parse_questions(scripted.get("questions"))),
            tool_requests=tuple(parse_tool_requests(scripted.get("toolRequests"))),
            stop_reason=parse_stop_reason(scripted.get("stopReason")),
            analysis=parse_analysis(scripted.get("analysis")),
            request_id=uuid.uuid4().hex,
            prompt_version=PROMPT_VERSION,
            model_name=None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


__all__ = ["PROMPT_VERSION", "SCRIPT_ENV", "ScriptedConfigError", "ScriptedReasoner", "parse_script"]

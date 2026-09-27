"""`AGENT_REASONER=script`:隔离栈里那个**测试脚手架**的边界。

## 这个文件在防什么

它防的不是"脚本能不能念对",而是**它会不会被误当成产品能力**。一个只在测试里出现
的假模型,最坏的失败方式是它悄悄变成了线上的一条真实路径 —— 或者反过来,它悄悄失效
而验收照样通过。第 1 节管前者,第 2 节管后者。

三件具体的事:

1. **配置不全要炸,不能退。** 只设了 `AGENT_REASONER=script` 而没设
   `ZHITU_SCRIPTED_ACTIONS` 时,如果安静地退回规则兜底,一次验收就会以"模型什么
   都没提"的形状通过 —— 而它比"脚本念错了一句"难查得多,因为没有任何东西报错。
   而且要在**启动时**炸:reasoner 是懒构造的,留到第一条消息就成了一条 500,而
   浏览器把它显示成"连不上后端服务"(第 1 节的最后一条走真的 lifespan 验它)。
   脚本本身有两种写法(JSON、或一份 JSON 文件的路径),这一节也一并钉住。
2. **来源必须如实。** `source` 是 `scripted`,而且这个值**不在**产品那四个里。
   脚本借用 `direct_llm` 的话,隔离栈里的一次演示会在库里留下"这一轮是直连模型
   生成的",而它是脚本 —— 那正是 `ModelSource` 这个枚举存在的理由。
3. **脚本绕不开校验。** 第 3 节把脚本接到真的 `/messages` 上:越界的动作照样逐条
   落进 `proposalErrors`。脚本替掉的只有"谁来想出这些动作"这一步。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.agent.runtime import ScriptedReasoner, build_reasoner
from backend.agent.runtime.scripted import SCRIPT_ENV, ScriptedConfigError, parse_script
from backend.db.models.enums import ModelSource

#: 一份最小的、合法的一轮脚本。
ONE_TURN = {"reply": "我给你排一版。", "actions": [{"op": "create_node", "localId": "n2",
                                                   "parentRef": "n1", "title": "读官方教程"}]}


# ---------------------------------------------------------------------------------
# 1. 它只在显式配置下存在
# ---------------------------------------------------------------------------------
def test_the_script_mode_needs_its_script(monkeypatch: pytest.MonkeyPatch, settings_factory) -> None:
    """**配了模式没配脚本 = 启动即失败。**

    这是这个文件里最重要的一条。安静退回规则兜底的代价是:验收里那句"确认之后画布上
    长出了节点"会失败在断言上,而人会先怀疑模型、再怀疑去重、最后才想到"脚本压根
    没配" —— 如果它没在启动时就炸的话。
    """
    monkeypatch.delenv(SCRIPT_ENV, raising=False)

    with pytest.raises(ScriptedConfigError, match=SCRIPT_ENV):
        build_reasoner(settings_factory(agent_reasoner="script", llm_api_key=""))


def test_the_script_mode_needs_no_api_key(monkeypatch: pytest.MonkeyPatch, settings_factory) -> None:
    """脚本不连模型,所以**不受"没有 key 就退回规则兜底"那条规则约束**。

    它必须排在 `llm_api_key` 那道判断之前 —— 隔离栈里本来就没有密钥。
    """
    monkeypatch.setenv(SCRIPT_ENV, json.dumps(ONE_TURN))

    reasoner = build_reasoner(settings_factory(agent_reasoner="script", llm_api_key=""))
    assert isinstance(reasoner, ScriptedReasoner)


def test_the_other_modes_are_untouched(monkeypatch: pytest.MonkeyPatch, settings_factory) -> None:
    """`rule` / 没 key 的 `auto` 照旧 —— 新增一个分支最怕的是顺手改到了别的分支。"""
    from backend.agent.runtime import RuleFallbackReasoner

    monkeypatch.setenv(SCRIPT_ENV, json.dumps(ONE_TURN))
    assert isinstance(
        build_reasoner(settings_factory(agent_reasoner="rule", llm_api_key="k")),
        RuleFallbackReasoner,
    )
    assert isinstance(
        build_reasoner(settings_factory(agent_reasoner="auto", llm_api_key="")),
        RuleFallbackReasoner,
    )


def test_the_script_can_be_a_path_to_a_file(tmp_path: Path) -> None:
    """`ZHITU_SCRIPTED_ACTIONS` 也可以写成**一份文件的路径**。

    这条是 `accept-e2e.mjs --script=<file>` 那条路的地基:`--script` 收的是一个文件,
    而 runner 把它原样放进了环境变量。少了这条,整条路走不通 —— 而它在 HTTP 上表现
    出来的样子**不是**"脚本没配",是一条 500,浏览器把它显示成「连不上后端服务,
    请确认后端已经启动」,于是人跑去查一个活得好好的进程(2026-09-27 实际发生)。
    """
    source = tmp_path / "interview.json"
    source.write_text(json.dumps(ONE_TURN), encoding="utf-8")

    turns = parse_script(str(source))
    assert turns[0]["reply"] == ONE_TURN["reply"]


def test_a_path_that_is_not_there_says_both_things(tmp_path: Path) -> None:
    """路径写错时,消息里要**同时**有"不是 JSON"和那个路径。

    只说前一半的话,一个把路径打错的人会去改 JSON —— 而 JSON 好好的。
    """
    missing = tmp_path / "nope.json"

    with pytest.raises(ScriptedConfigError) as caught:
        parse_script(str(missing))

    assert str(missing) in str(caught.value)


async def test_a_misconfigured_script_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch, settings_factory
) -> None:
    """**启动期**就把它拦下来,不是留到第一条消息。

    上面那条 `test_the_script_mode_needs_its_script` 验的是"构造时会抛",而构造是
    **懒**的:`get_reasoner` 要到第一条需要推理的请求才建它。于是配置错的形状是
    "进程起来了、探针也绿了,第一条消息 500" —— 而那正是最容易被读反的一种:
    跨域浏览器把"500 且响应里没有 CORS 头"显示成「连不上后端服务,请确认后端已经
    启动」,验收的人去查进程,而进程活得好好的。

    所以这里**走真的 lifespan**,验的是"进程根本起不来"。
    """
    from backend.api.dependencies.agent import reset_reasoner_cache
    from backend.api.main import app, lifespan

    monkeypatch.delenv(SCRIPT_ENV, raising=False)
    scripted = settings_factory(agent_reasoner="script", llm_api_key="")
    # 两份绑定,同一个对象:lifespan 读 `main.settings`,而 `get_reasoner` 读的是
    # `dependencies.agent.settings`(`from ... import settings` 复制的是引用)。
    # 只改一处的话,这条测试量到的是"两个模块看到了不同的配置" —— 它当然不会抛。
    monkeypatch.setattr("backend.api.main.settings", scripted)
    monkeypatch.setattr("backend.api.dependencies.agent.settings", scripted)
    reset_reasoner_cache()
    try:
        with pytest.raises(ScriptedConfigError, match=SCRIPT_ENV):
            async with lifespan(app):
                pytest.fail("配置不全的脚本模式把进程放起来了")
    finally:
        # `get_reasoner` 是进程级 `lru_cache` —— 不清的话,后面每一个用例拿到的都是
        # 这一轮建出来的东西。
        reset_reasoner_cache()


def test_scripted_is_not_one_of_the_product_sources() -> None:
    """`scripted` 是一个**独立的**来源,不是四个产品来源里的任何一个的马甲。

    这条断言看起来只是比字符串,但它钉的是那个决定:脚本化的一轮在库里、在接口里、
    在界面上都必须一眼可辨。
    """
    assert ModelSource.SCRIPTED.value == "scripted"
    product = {
        ModelSource.OPENJIUWEN,
        ModelSource.DIRECT_LLM,
        ModelSource.RULE_FALLBACK,
        ModelSource.UNAVAILABLE,
    }
    assert ModelSource.SCRIPTED not in product
    assert {source.value for source in product} == {
        "openjiuwen",
        "direct_llm",
        "rule_fallback",
        "unavailable",
    }


# ---------------------------------------------------------------------------------
# 2. 脚本的形状:按轮取用、用完为止、写错就报
# ---------------------------------------------------------------------------------
def test_turns_are_used_in_order_and_then_the_script_runs_out() -> None:
    """用完**就是空的**,不重复最后一轮。

    重复会让一段多余的对话(比如界面重试)再提一遍同一份动作 —— 它会被去重合并成
    第二次更新,于是验收看到的是一个"不知道为什么多出来的更新",而原因在脚本之外。
    """
    turns = parse_script(json.dumps({"turns": [{"reply": "第一轮"}, {"reply": "第二轮"}]}))
    assert [turn["reply"] for turn in turns] == ["第一轮", "第二轮"]

    reasoner = ScriptedReasoner(turns)
    assert reasoner.turns_total == 2
    assert reasoner.turns_used == 0


def test_a_bare_action_list_is_accepted() -> None:
    """只写一个动作数组也是一种自然的写法 —— 补一句空的回复,而不是报错。"""
    turns = parse_script(json.dumps([{"op": "create_node", "localId": "n2",
                                     "parentRef": "n1", "title": "x"}]))
    assert turns[0]["actions"][0]["title"] == "x"
    assert turns[0].get("reply") is None


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("", "空串"),
        ("not json at all", "不是 JSON"),
        ('{"turns": []}', "turns 是空的"),
        ('{"turns": "nope"}', "turns 不是数组"),
        ('{"reply": "x", "action": []}', "把 actions 拼错了"),
        ('{"turns": ["nope"]}', "某一轮不是对象"),
        ("123", "JSON 合法但形状不对"),
    ],
)
def test_a_malformed_script_fails_loudly(raw: str, why: str) -> None:
    """每一种写法错误都要**当场报**,而不是变成一轮空动作。

    尤其是 `action` 那个拼错的键:静默忽略它的后果是整段脚本"什么都不提",
    而脚本文件本身看起来完全正常。
    """
    with pytest.raises(ScriptedConfigError):
        parse_script(raw)


# ---------------------------------------------------------------------------------
# 3. 接上真的 `/messages`:脚本替掉的只有"谁来想出这些动作"
# ---------------------------------------------------------------------------------
async def test_a_scripted_turn_goes_through_the_real_validation_chain(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """脚本提的动作经过**同一条**校验链:合法的那一条变成提案,越界的那一条被拒。

    这是"脚本是个脚手架,不是一个后门"的可证伪问法。注意它是**全有或全无**:
    `validate_actions` 只要有一条错就返回 `plan=None`(见 `proposal_service`),
    所以这两件事只能分成两次请求验 —— 一次全合法,一次夹一条越界的。
    """
    account = await make_account()
    script = [
        {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "写文献综述"},
        {"op": "create_node", "localId": "n3", "parentRef": "n2", "title": "我排名 38",
         "nodeType": "capability", "purpose": "information"},
    ]
    use_reasoner(ScriptedReasoner.from_env(json.dumps({"reply": "记下来了。", "actions": script})))

    response = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "我排名 38,每周能拿出 10 小时。", "clientMessageId": "t1"},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # 来源如实:界面上那个徽标会写"测试脚手架 · 脚本回放",而不是"AI 规划"。
    assert body["source"] == "scripted"
    assert body["assistantMessage"]["modelSource"] == "scripted"
    # 它不是降级:降级意味着"这次没能给出结果",而这条路是**按配置在干活**。
    assert body["degraded"] is False

    assert body["proposalErrors"] == []
    assert body["proposal"] is not None, "脚本提的动作没有变成提案"
    # 信息主题在提案预览里说得出用途 —— 那正是访谈共建要看的那一句。
    summaries = " / ".join(item["summary"] for item in body["proposal"]["items"])
    assert "我排名 38" in summaries, summaries

    # 提案**还没有生效**:画布上只有根节点。
    plan = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert len(plan.json()["nodes"]) == 1


async def test_an_out_of_scope_action_in_the_script_is_still_refused(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """脚本里夹一条**越界**的动作 —— 它造不出提案,连合法的那一条都不许落库。

    脚本替掉的只有"谁来想出这些动作"这一步。少了这条,一个绕过校验的脚本实现会
    悄悄生效,而它绕过的正是范围(用户在某一层子空间里,范围外的东西只读)。
    """
    account = await make_account()

    async def _create(title: str) -> dict:
        response = await app_client.post(
            f"/api/workspaces/{account.workspace_id}/nodes",
            json={
                "parentId": next(
                    node["id"]
                    for node in (
                        await app_client.get(
                            f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
                        )
                    ).json()["nodes"]
                    if node["parentId"] is None
                ),
                "title": title,
            },
            headers=account.headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["node"]

    inside = await _create("在范围内")
    await _create("在范围外")

    use_reasoner(
        ScriptedReasoner.from_env(
            json.dumps(
                {
                    "reply": "记下来了。",
                    "actions": [
                        {"op": "update_node", "targetRef": "n2", "description": "范围内:改这句"},
                        # `n3` 是那个范围外的节点 —— 记号认得出它,但它不可写。
                        {"op": "update_node", "targetRef": "n3", "description": "范围外:也想改"},
                    ],
                }
            )
        )
    )

    response = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={
            "content": "改一下这两条",
            "clientMessageId": "t2",
            # 进到「在范围内」这一层:范围因此只剩它自己。
            "contextNodeId": inside["id"],
            "scopeRootId": inside["id"],
        },
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["proposal"] is None, "有一条不合法的动作,整份提案都不该存在"
    assert [error["code"] for error in body["proposalErrors"]] == ["OUT_OF_SCOPE"], body[
        "proposalErrors"
    ]

    plan = (
        await app_client.get(
            f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
        )
    ).json()
    by_title = {node["title"]: node for node in plan["nodes"]}
    # 全有或全无:合法的那条也一个字都没写。
    assert by_title["在范围内"]["description"] is None
    assert by_title["在范围外"]["description"] is None


async def test_a_script_that_runs_out_quietly_stops_proposing(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    """脚本用完之后是**空的一轮**:不提案、不编一句话,而且不报错。

    这条与上面那条是同一个东西的两半 —— 只验"第一轮能提"的话,一个"每一轮都重复
    最后一轮"的实现照样全绿,而它会在这条的第二轮里露出来。
    """
    account = await make_account()
    use_reasoner(
        ScriptedReasoner.from_env(json.dumps({"turns": [{"reply": "第一轮提一个", "actions": [
            {"op": "create_node", "localId": "n2", "parentRef": "n1", "title": "写文献综述"}
        ]}]}))
    )

    def _send(content: str) -> dict:
        return {"content": content, "clientMessageId": content}

    first = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json=_send("第一句"),
        headers=account.headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["proposal"] is not None

    second = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json=_send("第二句"),
        headers=account.headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["proposal"] is None
    assert second.json()["proposalErrors"] == []
    # 回复不是上一轮的重复:脚本没话说了就说没话说了。
    assert "脚本已经跑完" in second.json()["reply"]

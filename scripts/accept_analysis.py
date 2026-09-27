#!/usr/bin/env python
"""AI 分析层验收 —— 用**真实模型**把「内容变了 → 这份判断过期 → 重新分析」走一遍。

## 这一层为什么必须单独跑一次

前两层(规则/契约、隔离栈端到端)验的是**没有模型时也成立**的那些保证。而分析这一层
最要紧的那件事恰好只有真模型才能验:**它读到的是不是当时的内容,以及内容变了之后
那份判断会不会自己承认过期。**

在隔离栈里这件事验不了,而且原因是**对的**:没有 key 时走规则兜底,规则兜底**不产生
分析记录**("一段不是模型给的判断不能冒充判断"),所以那一栈里根本没有一条真记录可以过期。
于是"过期"这条链路的浏览器端只能验前端那一半,剩下的一半在这里。

## 它验的八件事

1. 模型真的会给一份结构化的判断(七栏),不是一段没法挑错的散文。
2. 那条记录**挂在被讨论的节点上**(`focusNodeId`),而且是 `fresh` 的。
3. **改正文之后同一条记录变成 `stale`,并且说得清是哪个节点的正文变了。**
   —— 这是这一批的核心能力,前两层只能验它的契约,验不了它的实际行为。
4. 点「重新分析」之后:对话里真的多了两条消息、旧记录**还在**(append-only)、
   最新一条回到 `fresh`。
5. 全程没有多出节点、没有多出工时(正文保存只动正文)。
6. 时间边界:这一批的回复里**不许出现**"已经排好日程"这类声称——模型没有排期写入能力。
7. **访谈共建**(第二批,v1.2 §2.5):换一个**干净空间**,用真实模型走三轮问答,断言
   「用户说过的那条事实」最终落在**某个节点**上(正文或长笔记),而不是消失在一句
   「记下了」里;而且它落在的是**信息主题**上 —— 那正是第二批新长出来的那条路。
8. 同一段访谈里的两条机械边界:没有把「38/40」这样的比值当节点标题、信息主题没有
   带工时或截止(§4.1)、也没有一条提议撞上机械守卫。

**这一层刻意不比措辞。** 模型把那个节点叫「学业情况」还是「申请背景」都行,用户要的是
"我说过的那个数字被记下来了";所以第 7 条的断言是结构性的:含不含那个数字、落在哪一种
用途的节点上。

第 7 条不通过时,报告要能回答**是哪一半坏了**,于是第 5 步顺手量了一个对照项:同样这个
进程、这份提示词、这个模型,在**显式的**「重新分析」那一轮提出了多少条动作。两个数摆
在一起才分得清「动作通道不通」和「普通对话轮里模型不提动作」—— 这两种的修法完全不同。

## 它不碰什么

- **不碰 `data/zhitu_dev.db`。** 全程在临时目录里建库,验收结束删掉(`--keep` 可留)。
- **不输出密钥。** 脚本要求 `.env` 里有非空的 `LLM_API_KEY`,但从不打印它;报告里只写
  "有/没有",以及每一轮实际用到的 `model_source`。
- **不碰你正在跑的开发后端。** 它自己起一个 uvicorn 子进程,用自己的端口与数据库。

## 用法

    /c/Users/j/miniconda3/envs/zhitu/python.exe scripts/accept_analysis.py

可选:

    --turns 5     补充规划条件最多几轮(默认 4)
    --port 8141   验收用的端口
    --keep        跑完保留临时库与后端日志,方便自己翻
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]

# 控制台代码页是 936(GBK),中文会变成乱码。先固定成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'通过' if ok else '不通过'}] {name}" + (f" —— {detail}" if detail else ""))


def note(text: str) -> None:
    print(f"        {text}")


def short(value: object, limit: int = 110) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------------
# 起停一个真的后端进程
# ---------------------------------------------------------------------------------


def free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])


class Server:
    """一个 uvicorn 子进程。日志写文件而不是走管道 —— 管道写满后子进程会阻塞在写那
    一行上,表现成"后端 60 秒没就绪",而真正的原因不在后端。写法与
    `scripts/accept_stage8.py` 同一套。"""

    def __init__(self, port: int, database_url: str, log_path: Path) -> None:
        self.port = port
        self.database_url = database_url
        self.log_path = log_path
        self.base = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.port = free_port(self.port)
        self.base = f"http://127.0.0.1:{self.port}"
        env = self.environment()
        handle = self.log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", "backend.api.main:app",
                    "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "info",
                ],
                cwd=REPO_ROOT, env=env,
                stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
        finally:
            handle.close()

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process = None

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": self.database_url,
                "APP_ENV": "development",
                "DB_ECHO": "0",
                "PYTHONUTF8": "1",
            }
        )
        return env

    def migrate(self) -> None:
        """把临时库升到 head。

        **不能跳过这一步。** 一个从没跑过迁移的库会让应用直接拒绝启动
        (`db/health.py::assert_schema_current` —— 它宁可起不来,也不肯对着一份
        结构不明的库说"就绪")。所以"自起一个后端"这件事里,迁移是它的一部分。
        """
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"],
            cwd=REPO_ROOT, env=self.environment(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n=== alembic upgrade head ===\n")
            handle.write(completed.stdout or "")
        if completed.returncode != 0:
            raise RuntimeError(
                "迁移失败,后端不会起来 —— 见 backend.log 的 `alembic upgrade head` 那一段。"
            )


# ---------------------------------------------------------------------------------
# 验收主体
# ---------------------------------------------------------------------------------

#: 这一批明确**不许说**的话。模型没有排期写入能力,说了就是编的。
FORBIDDEN_CLAIMS = ("已经调整了日程", "已经排好了", "已排进日程", "已经帮你排", "已经安排好")

#: 「38/40」这样的**比值**不是一件事,不该变成节点标题。访谈里用户报排名,这是最容易
#: 被模型原样抄成标题的一种输入。**不比措辞**:只要求标题里没有这种形状。
BARE_RATIO = re.compile(r"^\D*\d+\s*/\s*\d+$")

#: 服务端那几道机械守卫的错误码。撞上它们不是产品缺陷(拒绝是对的),但它说明**模型的
#: 判断**与 §4.1 说的不一致,所以单独记一条,让读报告的人看得见是哪一半出了问题。
GUARD_CODES = {"INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE", "DUPLICATE_NODE_TITLE"}


class Acceptance:
    def __init__(self, server: Server, turns: int) -> None:
        self.server = server
        self.turns = turns
        self.token = ""
        self.workspace = ""
        self.root = ""
        self.task = ""
        self.client: httpx.AsyncClient | None = None
        self.latest: dict = {}
        self.first_id: str = ""
        self.transcript: list[dict] = []
        #: 访谈那一段里所有被服务端拒过的动作码 —— 全有或全无,所以它们同时解释了
        #: "为什么那一轮没有落库"。
        self.interview_errors: list[str] = []
        #: **诊断用的两个数**:对话轮里提过几轮提案、以及显式触发的那一轮提了多少条。
        #: 它们摆在一起才回答得了"模型不提动作"是哪一半的问题(见 `interview` 末尾)。
        self.chat_turns = 0
        self.chat_proposals = 0
        self.refresh_actions = 0

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def call(self, method: str, path: str, **kwargs) -> tuple[int, dict]:
        response = await getattr(self.client, method)(f"{self.server.base}{path}", **kwargs)
        try:
            body = response.json()
        except Exception:
            body = {"_raw": short(response.text, 400)}
        return response.status_code, body

    async def wait_ready(self, limit: float = 60.0) -> None:
        deadline = time.monotonic() + limit
        last = ""
        while time.monotonic() < deadline:
            try:
                response = await self.client.get(f"{self.server.base}/ready", timeout=3.0)
                if response.status_code == 200:
                    return
                last = f"HTTP {response.status_code}"
            except Exception as cause:
                last = type(cause).__name__
            await asyncio.sleep(0.4)
        raise RuntimeError(f"后端在 {limit:.0f} 秒内没有就绪(最后:{last})")

    # -- 场景搭建 ------------------------------------------------------------------

    async def register_and_create(self) -> None:
        print("1. 独立账号 + 一个空空间(目标是模糊的)")
        status, body = await self.call(
            "post", "/api/auth/register",
            json={
                "email": f"accept-analysis-{uuid.uuid4().hex[:10]}@zhitu.test",
                "password": "accept-analysis",
                "displayName": "分析层验收",
                "timezone": "Asia/Shanghai",
            },
        )
        record("注册返回 201 并拿到令牌", status == 201, f"HTTP {status}")
        if status != 201:
            raise RuntimeError(f"注册失败:{short(body, 300)}")
        self.token = body["token"]

        status, body = await self.call(
            "post", "/api/workspaces",
            json={"title": "分析层验收空间", "intent": "想做出一个能跑起来的东西", "goal": "三个月内做出一个能跑起来的东西"},
            headers=self.auth(),
        )
        record("建空间返回 201", status == 201, f"HTTP {status}")
        if status != 201:
            raise RuntimeError(f"建空间失败:{short(body, 300)}")
        self.workspace = body["workspace"]["id"]
        # 根目标取自**创建响应**而不是 `/plan` 的第一条 —— 后者要看服务端的排序,
        # 而这个响应本来就说了"新空间里只有一个根目标,别无他物"。
        #
        # 键名是 `rootNode`:契约层统一把线格式定成 camelCase(`contracts/common.py`),
        # 所以脚本读的一律是 JSON 里那个名字,不是 Python 侧的字段名。
        self.root = body["rootNode"]["id"]

        # 一个**带正文**的任务。正文里写一条真实的约束 —— 后面就是改它来触发过期。
        status, body = await self.call(
            "post", f"/api/workspaces/{self.workspace}/nodes",
            headers=self.auth(),
            json={
                "parentId": self.root,
                "title": "跑一组对照实验",
                "nodeType": "task",
                "description": "先跑一组对照实验,对照组用旧流程。",
                "estimateMinutes": 120,
            },
        )
        record("建任务返回 201", status == 201, f"HTTP {status}")
        if status != 201:
            raise RuntimeError(f"建节点失败:{short(body, 300)}")
        self.task = body["node"]["id"]

    # -- 对话 ----------------------------------------------------------------------

    async def turn(
        self, content: str, node_id: str | None = None, workspace: str | None = None
    ) -> dict:
        """发一条消息,把回复原文记进 `transcript`(报告要用)。

        `workspace` 缺省是分析那一段的空间;访谈那一段有它自己的干净空间(见
        `interview`),而 `transcript` 是共用的 —— 最后那条措辞检查因此覆盖全部轮次。
        """
        payload: dict = {"content": content}
        if node_id:
            payload["contextNodeId"] = node_id
        status, body = await self.call(
            "post", f"/api/workspaces/{workspace or self.workspace}/messages",
            headers=self.auth(), json=payload,
        )
        if status != 200:
            raise RuntimeError(f"发消息失败:HTTP {status} {short(body, 300)}")
        self.chat_turns += 1
        if body.get("proposal"):
            self.chat_proposals += 1
        self.transcript.append(
            {
                "asked": content,
                "reply": body.get("reply", ""),
                "source": body.get("source"),
                "degraded": body.get("degraded"),
                "degradedReason": body.get("degradedReason"),
                "inputChanged": body.get("inputChanged"),
                "proposal": (body.get("proposal") or {}).get("itemCount"),
            }
        )
        note(f"{body.get('source')} · {short(body.get('reply', ''))}")
        return body

    async def gather_conditions(self) -> None:
        """把"还缺哪些规划条件"补上。

        **脚本不规定 AI 该问什么**,只把服务端算出来的缺口补掉 —— 断言落在结果上
        (`brief.missing` 变空),不落在措辞上。
        """
        print("\n2. 补齐规划条件")
        answers = {
            "deadline": "三个月内,大概 12 月下旬。",
            "weekly_available_minutes": "每周能投入 6 小时,主要是周末。",
            "current_level": "会一点 Python,没做过完整的项目。",
            "success_criteria": "能跑起来、我能给别人演示一遍就行。",
            "constraints": "周中实验室排不开,只能周末做。",
        }
        for _ in range(self.turns):
            _, brief = await self.call(
                "get", f"/api/workspaces/{self.workspace}/messages", headers=self.auth()
            )
            missing = brief.get("brief", {}).get("missing", [])
            if not missing:
                break
            answer = answers.get(missing[0], f"关于 {missing[0]},按你觉得合理的来。")
            await self.turn(answer)
        _, brief = await self.call(
            "get", f"/api/workspaces/{self.workspace}/messages", headers=self.auth()
        )
        remaining = brief.get("brief", {}).get("missing", [])
        record(
            "规划条件补齐(服务端算的 missing 为空)",
            not remaining,
            "还缺:" + "、".join(remaining) if remaining else "每周 6 小时 / 12 月下旬 / 只能周末",
        )

    # -- 分析这一层 ----------------------------------------------------------------

    async def analyses(self, limit: int = 5) -> list[dict]:
        status, body = await self.call(
            "get",
            f"/api/workspaces/{self.workspace}/analyses?focusNodeId={self.task}&limit={limit}",
            headers=self.auth(),
        )
        if status != 200:
            raise RuntimeError(f"读分析失败:HTTP {status} {short(body, 300)}")
        return body["analyses"]

    async def first_analysis(self) -> None:
        print("\n3. 让模型分析这个节点")
        await self.turn("这个任务我打算先跑一组对照实验,你看这么安排行不行?", node_id=self.task)

        found = await self.analyses()
        record("模型给出了一份分析记录", bool(found), f"{len(found)} 条")
        if not found:
            raise RuntimeError(
                "这一轮没有留下分析记录 —— 后面几条都无从谈起。"
                "先看上面的回复:模型可能没给 analysis 那一段。"
            )
        self.latest = found[0]
        self.first_id = self.latest["id"]

        record(
            "这条记录挂在这个节点上",
            self.latest.get("focusNodeId") == self.task,
            f"focusNodeId={self.latest.get('focusNodeId')} 期望={self.task}",
        )
        record(
            "来源是模型,不是规则兜底",
            self.latest.get("modelSource") in {"openjiuwen", "direct_llm"},
            str(self.latest.get("modelSource")),
        )
        filled = {
            field: len(self.latest.get(field) or [])
            for field in ("known", "unknowns", "evidence", "assumptions", "diagnosis", "strategyOptions", "risks")
        }
        record(
            "七栏里至少有三栏有内容(不是一段没法挑错的散文)",
            sum(1 for count in filled.values() if count) >= 3,
            json.dumps(filled, ensure_ascii=False),
        )
        record("刚做出来的判断是「基于当前内容」", self.latest.get("freshness") == "fresh", str(self.latest.get("freshness")))
        record(
            "它如实说了这份判断的覆盖范围",
            "coverageNote" in self.latest,
            short(self.latest.get("coverageNote") or "(没有截断,不必说明)"),
        )

    async def edit_body_then_expect_stale(self) -> None:
        print("\n4. 改正文 —— 那份判断必须自己承认过期")
        _, plan = await self.call("get", f"/api/workspaces/{self.workspace}/plan", headers=self.auth())
        node = next(item for item in plan["nodes"] if item["id"] == self.task)
        before_version = node["contentVersion"]

        status, body = await self.call(
            "patch", f"/api/workspaces/{self.workspace}/nodes/{self.task}",
            headers=self.auth(),
            json={
                "description": "先跑一组对照实验,对照组用旧流程。**但只能周末做,周中实验室排不开。**",
                "contentVersion": before_version,
            },
        )
        record("正文写入成功(带版本号)", status == 200, f"HTTP {status} {short(body, 200)}")
        if status != 200:
            raise RuntimeError("正文没写进去,过期那一条验不了。")

        again = await self.analyses()
        latest = again[0]
        record(
            "同一条记录现在报「过期」",
            latest.get("freshness") == "stale",
            f"freshness={latest.get('freshness')}",
        )
        reasons = latest.get("staleReasons") or []
        record(
            "而且说得清是哪里变过(不是一句空泛的'已过期')",
            bool(reasons),
            " / ".join(reasons) or "(理由为空)",
        )
        # **改正文只让那一条过期,不产生第二条。** 新的记录要等重新分析才会有 ——
        # 所以这里断言的是"还是那一条、而且它的七栏内容没被动过",不是"条数变多了"。
        record(
            "过期只标不改:还是那一条,内容一字未动",
            latest.get("id") == self.first_id and bool(latest.get("known")),
            f"{len(again)} 条,id {'没变' if latest.get('id') == self.first_id else '变了'}",
        )
        self.latest = latest

    async def refresh(self) -> None:
        print("\n5. 「根据最新内容重新分析」——走的是对话,不是捷径")
        before = await self.analyses()
        status, body = await self.call(
            "post", f"/api/workspaces/{self.workspace}/nodes/{self.task}/analysis/refresh",
            headers=self.auth(),
        )
        record("重新分析返回 200", status == 200, f"HTTP {status}")
        if status != 200:
            raise RuntimeError(f"重新分析失败:{short(body, 300)}")
        # 这一轮的**动作条数**是第 8 步那条诊断的对照项:`manual_replan` 是显式触发,
        # 而普通对话轮是模型自己判断该不该提 —— 两者一比,才知道"模型不提动作"是哪一半。
        proposal = body.get("proposal") or {}
        self.refresh_actions = proposal.get("itemCount", 0)
        if self.refresh_actions:
            note(
                "这一轮明确提出 "
                + str(self.refresh_actions)
                + " 条动作:"
                + short(" / ".join(item["summary"] for item in proposal.get("items", [])), 160)
            )

        asked = (body.get("userMessage") or {}).get("content", "")
        record(
            "它替用户说了一句人话,而且就是那一句",
            asked == "根据最新内容重新分析一下这个节点。",
            short(asked),
        )
        record(
            "那一句挂在这个节点上(不是整个空间)",
            (body.get("userMessage") or {}).get("contextNodeId") == self.task,
            str((body.get("userMessage") or {}).get("contextNodeId")),
        )
        record(
            "这一轮在对话里留下了两条消息",
            (body.get("assistantMessage") or {}).get("content", "") not in ("", None),
            f"回复来源 {body.get('source')}",
        )

        after = await self.analyses()
        record(
            "分析记录是只增的:原来那些还在",
            len(after) >= len(before) + 1,
            f"{len(before)} → {len(after)} 条",
        )
        record(
            "最新一条回到了「基于当前内容」",
            after[0].get("freshness") == "fresh",
            f"freshness={after[0].get('freshness')}",
        )
        record(
            "而且它是**一条新记录**,不是把旧的改掉了",
            after[0].get("id") != before[0].get("id"),
            f"新 {after[0].get('id')} / 旧 {before[0].get('id')}",
        )
        self.transcript.append(
            {"asked": asked, "reply": body.get("reply", ""), "source": body.get("source"), "degraded": body.get("degraded")}
        )

    async def nothing_was_duplicated(self) -> None:
        print("\n6. 顺带钉住的两条:节点没多、工时没变")
        _, plan = await self.call("get", f"/api/workspaces/{self.workspace}/plan", headers=self.auth())
        tasks = [item for item in plan["nodes"] if item["nodeType"] == "task"]
        record("没有多出节点", len(plan["nodes"]) == 2, f"{len(plan['nodes'])} 个:根 + 任务")
        record("任务还是那一个,工时没被抹掉", len(tasks) == 1 and tasks[0]["estimateMinutes"] == 120,
               f"{len(tasks)} 个任务,预计 {tasks[0]['estimateMinutes'] if tasks else '?'} 分钟")
        record(
            "正文改的是我们写进去的那一版",
            "只能周末做" in (tasks[0]["description"] or ""),
            short(tasks[0]["description"] or ""),
        )

    def no_forbidden_claims(self) -> None:
        print("\n9. 时间边界与措辞:不许声称已经排好了日程")
        offenders = [
            short(item["reply"], 80)
            for item in self.transcript
            if any(claim in item["reply"] for claim in FORBIDDEN_CLAIMS)
        ]
        record(
            "所有回复里都没有「已经排好/已经调整日程」这类声称",
            not offenders,
            " / ".join(offenders) if offenders else f"检查了 {len(self.transcript)} 条回复",
        )

    # -- 访谈共建(第二批) ----------------------------------------------------------

    async def interview(self) -> None:
        """三轮访谈:用户说过的那条事实有没有落点。

        ## 为什么要另起一个干净空间

        上面那个空间里已经有根 + 一个任务,而这个断言要问的是「从零开始,模型会不会自己
        长出东西」——里混着那两个节点的话,每条断言都得先扣掉它们,而扣法本身就成了
        一个可能出错的地方。干净空间里只需要回答一个问题:用户说了排名 38,它去哪了。

        ## 断言哪里、不断言哪里

        不断言措辞,也不规定落在哪个节点上(节点叫什么都行)。断言的是**结构**:那个数字
        出现在某个节点的正文或长笔记里,而且承载它的节点是 `information` 用途 ——
        落在一个 planning 任务上意味着模型把它当成了一件要做的事,那正是 §2.5 要挡的。
        """
        print("\n8. 访谈共建:一句一句说出来的事实有没有落点")
        status, body = await self.call(
            "post", "/api/workspaces",
            json={
                "title": "访谈共建验收空间",
                "intent": "想申请出国读研,但不知道从哪开始",
                "goal": "三个月内把出国申请准备好",
            },
            headers=self.auth(),
        )
        record("访谈用的干净空间建起来了", status == 201, f"HTTP {status}")
        if status != 201:
            raise RuntimeError(f"建访谈空间失败:{short(body, 300)}")
        space = body["workspace"]["id"]

        said = [
            "我想申请出国读研,但不知道从哪开始。",
            "我排名 38,每周能投入 10 小时。",
            "把刚才那些细节记下来。",
        ]
        proposed = 0
        for index, line in enumerate(said, start=1):
            answer = await self.turn(line, workspace=space)
            codes = [error.get("code") for error in answer.get("proposalErrors") or []]
            self.interview_errors += [code for code in codes if code]
            if codes:
                note(f"第 {index} 轮有动作被服务端拒了:{codes}")
            proposal = answer.get("proposal")
            if not proposal:
                note(f"第 {index} 轮没有提案")
                continue
            proposed += 1
            note(
                f"第 {index} 轮提案 {proposal['itemCount']} 条:"
                + short(" / ".join(item["summary"] for item in proposal["items"]), 160)
            )
            if codes:
                # 全有或全无:只要有一条被拒,整份提案都不生效,确认它只会拿到错误码。
                continue
            status, confirmed = await self.call(
                "post", f"/api/workspaces/{space}/proposals/{proposal['id']}/confirm",
                headers=self.auth(), json={"idempotencyKey": f"interview-{index}"},
            )
            detail = f"HTTP {status}" + (f" —— {short(confirmed, 200)}" if status != 200 else "")
            record(f"第 {index} 轮提的东西确认之后真的落了库", status == 200, detail)

        record(
            "三轮里模型确实提出了要写进计划的东西",
            proposed > 0,
            f"{proposed}/{len(said)} 轮有提案",
        )
        guards = [code for code in self.interview_errors if code in GUARD_CODES]
        record(
            "没有一条提议撞上机械守卫(信息主题带工时 / 同父同用途重复)",
            not guards,
            "撞上的是:" + "、".join(guards) if guards else "三轮零拒绝",
        )

        _, plan = await self.call("get", f"/api/workspaces/{space}/plan", headers=self.auth())
        nodes = plan["nodes"]
        information = [node for node in nodes if node.get("purpose") == "information"]

        offenders = [node["title"] for node in nodes if BARE_RATIO.match(node["title"] or "")]
        record(
            "没有把「38/40」这样的比值当成节点标题",
            not offenders,
            " / ".join(offenders) if offenders else f"{len(nodes)} 个节点里没有这种形状的标题",
        )

        landed: list[tuple[dict, str]] = []
        for node in nodes:
            if "38" in (node.get("description") or ""):
                landed.append((node, "正文"))
        for node in information:
            status, stored = await self.call(
                "get", f"/api/workspaces/{space}/nodes/{node['id']}/notes", headers=self.auth()
            )
            if status == 200 and "38" in (stored.get("body") or ""):
                landed.append((node, "长笔记"))
        record(
            "「排名 38」被记在了某处(节点正文或长笔记),不是只剩一句「记下了」",
            bool(landed),
            " / ".join(f"「{node['title']}」的{where}" for node, where in landed) or "哪里都没有",
        )
        record(
            "承载它的是一条**信息主题**,不是一件要做的事",
            any(node.get("purpose") == "information" for node, _ in landed),
            "落在:" + " / ".join(f"「{node['title']}」({node.get('purpose')}·{where})" for node, where in landed)
            if landed else "没有落点",
        )

        planning_nodes = [node for node in nodes if node.get("purpose") != "information"]
        record(
            "「计划里有多少个节点」只数要排期的那些",
            plan["totalNodes"] == len(planning_nodes),
            f"totalNodes={plan['totalNodes']},非信息用途 {len(planning_nodes)} 个 / 全部 {len(nodes)} 个",
        )
        scheduling_bits = [
            node["title"] for node in information if node.get("estimateMinutes") or node.get("deadline")
        ]
        record(
            "信息主题没有带工时或截止(§4.1:它不需要这两个)",
            not scheduling_bits,
            " / ".join(scheduling_bits) if scheduling_bits else f"{len(information)} 个信息主题都没带",
        )

        # -------------------------------------------------------------- 诊断
        #
        # **这一条不是为了通过,是为了让上面那个 0 能读。** 三轮一个动作都没提时,
        # "0/3" 这个数字本身解释不了任何事:它既可能是"模型压根不给 actions"(通道或
        # 输出格式的问题),也可能是"模型会给 actions,只是没人告诉它用户报的情况可以建成
        # 信息主题、可以写成长笔记"(提示词的问题)。这两种的修法完全不同。
        #
        # 判据是**整个进程里有没有提过动作**,而不是"哪一轮提的":第一次写成"显式的
        # 『重新分析』那一轮必须提"(想拿它当对照),结果 2026-09-28 那次运行里,重新分析
        # 提了 0 条、而访谈第三轮提了 1 条 —— 那个判据本身在报一个假的不通过。哪一个
        # 触发会提是**模型的判断**,不是机制的不变量;机制的不变量只有"提了就要能落库"。
        record(
            "诊断:这一轮里模型确实提过动作(动作通道是通的)",
            (self.chat_proposals + self.refresh_actions) > 0,
            f"对话轮 {self.chat_proposals}/{self.chat_turns} 轮有提案,"
            f"显式「重新分析」{self.refresh_actions} 条",
        )
        if not self.chat_proposals:
            note(
                "普通对话轮里模型一条动作都没提过 —— 这时先看上面的提示词:它有没有告诉"
                "模型「用户报的情况可以建成信息主题、一大段可以写成长笔记」。机制全都在也"
                "不等于模型知道那条路存在。"
            )

    # -- 跑完 ----------------------------------------------------------------------

    async def run(self) -> None:
        # **`trust_env=False` 不是可选的。** httpx 默认会读环境与**注册表**里的代理
        # 设置(Windows 上 `getproxies()` 走 `Internet Settings`),而系统代理一旦打开,
        # 发往 `127.0.0.1` 的请求也会被交给代理 —— 表现成"后端明明起来了,`/ready`
        # 却一直 ReadTimeout"。这个脚本只跟自己起的那个进程说话,不需要任何代理。
        async with httpx.AsyncClient(timeout=180.0, trust_env=False) as client:
            self.client = client
            await self.wait_ready()
            await self.register_and_create()
            await self.gather_conditions()
            await self.first_analysis()
            await self.edit_body_then_expect_stale()
            await self.refresh()
            await self.nothing_was_duplicated()
            await self.interview()
            self.no_forbidden_claims()


# ---------------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------------


def environment_key_present() -> bool:
    """`.env` 里有没有非空的 `LLM_API_KEY`。**只回答有/没有,不读出来、不打印。**"""
    path = REPO_ROOT / ".env"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("LLM_API_KEY="):
            return bool(line.split("=", 1)[1].strip())
    return False


async def main_async(server: Server, turns: int) -> None:
    await Acceptance(server, turns).run()


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 分析层的真实模型验收")
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--port", type=int, default=8141)
    parser.add_argument("--keep", action="store_true")
    options = parser.parse_args()

    if not environment_key_present():
        # **拒绝运行,而不是降级。** 降级跑出来的那份结果会被读成"真实模型也这样",
        # 而它恰恰是把这一层最容易骗人的地方验没了。
        print("`.env` 里没有非空的 LLM_API_KEY —— 这一层验的就是真模型,没有 key 就没有可验的东西。")
        print("不降级运行:一份规则兜底的报告会被读成'真实模型也这样'。")
        return 2

    # 带时区,再转成本地时间只为了目录名好认 —— 裸 `datetime.now()` 在别处已经
    # 造成过"哪一天由时间戳推出来"的错误,这里不给它机会。
    stamp = datetime.now(tz=UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    run_dir = REPO_ROOT / "artifacts" / "acceptance" / f"{stamp}-realmodel"
    run_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="zhitu-analysis-"))
    db_path = work / "zhitu_analysis.db"

    print("=" * 72)
    print("知途 · AI 分析与访谈共建验收(真实模型)")
    print(f"运行目录  {run_dir}")
    print(f"临时库    {db_path}   (不碰 data/zhitu_dev.db)")
    print("密钥      有(.env 里的 LLM_API_KEY,全程不打印)")
    print("=" * 72)

    server = Server(options.port, f"sqlite+aiosqlite:///{db_path.as_posix()}", run_dir / "backend.log")
    server.migrate()
    server.start()
    exit_code = 1
    try:
        asyncio.run(main_async(server, options.turns))
        failed = [name for name, ok, _ in results if not ok]
        exit_code = 1 if failed else 0
    except Exception as cause:
        record("验收过程本身没有中断", False, f"{type(cause).__name__}: {short(cause, 300)}")
        exit_code = 1
    finally:
        server.stop()
        lines = [
            "知途 · AI 分析与访谈共建验收(真实模型)",
            "",
            f"运行目录    {run_dir}",
            f"时间        {stamp}",
            "密钥        有(未打印)",
            f"后端端口    {server.base}",
            f"临时库      {db_path}",
            "",
            f"结果        {sum(1 for _, ok, _ in results if ok)} 通过 / "
            f"{sum(1 for _, ok, _ in results if not ok)} 不通过,共 {len(results)} 条",
            "",
        ]
        lines += [f"[{'通过' if ok else '不通过'}] {name}" + (f" —— {detail}" if detail else "") for name, ok, detail in results]
        (run_dir / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if options.keep:
            shutil.copy2(db_path, run_dir / "zhitu_analysis.db")
            print(f"\n临时目录保留:{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
        print(f"\n报告:{run_dir / 'report.txt'}")
        failed = sum(1 for _, ok, _ in results if not ok)
        print(f"结果:{len(results) - failed} 通过 / {failed} 不通过")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

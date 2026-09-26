#!/usr/bin/env python
"""阶段 8 验收脚本 —— 把整条闭环在一个**真实进程**上跑一遍。

## 与 accept_stage1/2 的关键区别:这个脚本会真的调用模型

阶段 1、2 的验收跑在进程内的 ASGI transport 上,并且把 `LLM_API_KEY` 清空 —— 它们
验的是"没有模型时也成立"的那些保证。阶段 8 要验的恰好相反:模型真的被调用、
openJiuwen 真的在跑、模型给的阶段真的落进了计划。所以:

- 它**要求** `.env` 里有非空的 `LLM_API_KEY`。没有就拒绝运行,而不是降级成一个
  "本地规则也算过"的假验收。密钥本身全程不打印。
- 它起一个**真的 uvicorn 子进程**,用 HTTP 打真的接口。进程内 transport 验不了
  "重启之后还在" —— 那正是这一阶段最要命的一条。
- 它**不碰 `data/zhitu_dev.db`**。全程在临时目录里建库,验收结束就删。
  (这一点与阶段 2 一致:那个文件里装着你的真实计划。)

## 验收场景就是产品说明书里那一条

    新用户建「Python 学习」空空间 -> "三个月内完成一个项目"
    -> AI 主动了解基础与每周时间预算 -> 用户答每周 6 小时
    -> AI 生成分阶段计划 -> 用户确认
    -> 路径 / 时间线 / 任务 / 周计划 / 日计划同步出现
    -> **重启后端进程** -> 还在
    -> 用户反馈本周只剩 2 小时 -> AI 提出可执行调整 -> 确认后同步更新

路径、时间线、任务三个视图读的是同一个 `GET /plan`,所以"同步出现"这件事在这里
等价于"`/plan` 里有它们";周计划与日计划的落点是 `sessions`,所以脚本会真的排一次期。

## 用法

    /c/Users/j/miniconda3/envs/zhitu/python.exe scripts/accept_stage8.py

PowerShell:

    C:\\Users\\j\\miniconda3\\envs\\zhitu\\python.exe scripts\\accept_stage8.py

可选:

    --turns 6     多轮对话最多几轮(默认 5)
    --port 8137   验收用的端口
    --keep        跑完保留临时库,方便你自己翻
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

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


def short(value: object, limit: int = 90) -> str:
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
    """一个 uvicorn 子进程。`stop()` 之后可以再 `start()` —— 重启验证用它。

    ## 输出为什么写文件,而不是 `stdout=PIPE`

    这是踩过的第二个坑,而且症状完全指向错误的方向:子进程的日志走管道,
    **管道写满(Windows 上约 64KB)之后子进程会阻塞在写这一行上** —— 它还没走到
    `uvicorn.run()` 那一句去监听端口。于是表现是"后端 60 秒没就绪",看起来像
    启动慢或者端口冲突,而真正的原因是父进程没在读那根管子。写文件就没有这个死锁。

    顺带:`--log-level info` 是为了让"Uvicorn running on ..."这句留在文件里 ——
    起不来的时候,那段日志是唯一能说明白发生了什么的东西。
    """

    def __init__(self, port: int, database_url: str, log_path: Path) -> None:
        self.port = port
        self.database_url = database_url
        self.log_path = log_path
        self.base = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        # 重启时上一个进程可能还没把端口放开(Windows 上尤其明显)。占不到就换一个 ——
        # 换端口不影响这次验收要验的东西,而"起不来"会让持久化那一条给出假结论。
        self.port = free_port(self.port)
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": self.database_url,
                "APP_ENV": "development",
                "DB_ECHO": "0",
                "PYTHONUTF8": "1",
            }
        )
        # `--reload` 故意不加:它会另起一个 reloader 父进程,`terminate()` 只杀得掉
        # 父进程,子进程继续占着端口 —— 于是"重启"其实什么都没重启,而脚本会
        # 得到一份看似通过的持久化结论。
        handle = self.log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "backend.api.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--log-level",
                    "info",
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        finally:
            handle.close()

    def stop(self) -> str:
        if self.process is None:
            return ""
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process = None
        return self.log_path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------------
# 验收主体
# ---------------------------------------------------------------------------------


class Acceptance:
    def __init__(self, server: Server, turns: int) -> None:
        self.server = server
        self.turns = turns
        self.token = ""
        self.workspace = ""
        self.client = None  # 延迟到能 import httpx 之后再建

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def wait_ready(self, limit: float = 60.0) -> dict:
        deadline = time.monotonic() + limit
        last = ""
        while time.monotonic() < deadline:
            try:
                response = await self.client.get(f"{self.server.base}/ready", timeout=3.0)
                if response.status_code == 200:
                    return response.json()
                last = f"HTTP {response.status_code}"
            except Exception as cause:
                last = type(cause).__name__
            await asyncio.sleep(0.4)
        raise RuntimeError(f"后端在 {limit:.0f} 秒内没有就绪(最后:{last})")

    # -- 每一步 ------------------------------------------------------------------

    async def register_and_create(self) -> None:
        print("1. 注册账号,建一个空空间")
        registered = await self.client.post(
            "/api/auth/register",
            json={
                "email": f"accept8-{uuid.uuid4().hex[:10]}@zhitu.test",
                "password": "accept-stage-8",
                "displayName": "阶段八验收",
                "timezone": "Asia/Shanghai",
            },
        )
        record("注册返回 201 并拿到令牌", registered.status_code == 201, f"HTTP {registered.status_code}")
        if registered.status_code != 201:
            raise RuntimeError(f"注册失败,后面无法继续:{short(registered.text, 300)}")
        self.token = registered.json()["token"]

        created = await self.client.post(
            "/api/workspaces",
            json={"title": "Python 学习", "intent": "三个月内完成一个项目", "goal": "三个月内完成一个能跑起来的小项目"},
            headers=self.auth(),
        )
        body = created.json() if created.status_code == 201 else {}
        counts = body.get("workspace", {}).get("counts", {})
        record(
            "新空间 = 1 个根目标 + 0 会话 + 0 提案 + 0 排期",
            created.status_code == 201
            and counts == {"nodes": 1, "conversations": 0, "proposals": 0, "scheduledSessions": 0},
            str(counts),
        )
        if created.status_code != 201:
            raise RuntimeError(f"建空间失败:{short(created.text, 300)}")
        self.workspace = body["workspace"]["id"]

    async def converse(self) -> dict:
        """多轮对话,直到模型提出一份待确认的变更。

        **脚本不规定 AI 该问什么。** 产品的要求是"AI 主动了解基础与每周时间预算",
        而它问法由模型自己决定 —— 所以这里做的是:每轮结束看一眼 `brief` 缺什么,
        缺就补上,补齐了还没提案就明确要一份。断言落在**结果**上(brief 被填上了、
        提案出来了),不落在措辞上。

        每轮之间打印模型回复的片段。这不是调试输出,是验收证据的一部分:
        "AI 真的问了 / 真的答了"这件事只有回复原文能证明。
        """
        print("\n2. 多轮对话")
        turns = [
            "我想在三个月内完成一个能跑起来的 Python 小项目。",
            "我会基础语法和一点函数,没独立做过完整项目。每周大概能投入 6 小时。",
            "请根据这些条件,给我一份分阶段的计划。",
            "就按这个来,给我一份可以确认的变更。",
            "把阶段拆得更具体一点,然后给我一份可以确认的变更。",
            "请给我一份可以确认的变更。",
        ]
        seen_sources: list[str] = []
        seen_degraded: list[bool] = []
        last: dict = {}
        for index in range(min(self.turns, len(turns))):
            payload: dict = {"content": turns[index], "clientMessageId": str(uuid.uuid4())}
            if index == 0:
                payload["currentView"] = "path"
            sent = await self.client.post(
                f"/api/workspaces/{self.workspace}/messages",
                json=payload,
                headers=self.auth(),
                timeout=180.0,
            )
            if sent.status_code != 200:
                record(f"第 {index + 1} 轮发送", False, f"HTTP {sent.status_code} {short(sent.text, 200)}")
                raise RuntimeError("对话中断")
            last = sent.json()
            seen_sources.append(last["source"])
            seen_degraded.append(bool(last["degraded"]))
            brief = last["brief"]
            print(f"    第 {index + 1} 轮 · 模型({last['source']} {last.get('modelName') or '-'}"
                  f" {last.get('latencyMs')}ms): {short(last['reply'], 150)}")
            note(
                f"brief: 每周 {brief['weeklyAvailableMinutes']} 分钟 · 还缺 {brief['missing'] or '无'}"
                f" · 本轮提案 {'有' if last['proposal'] else '无'}"
                + (f" · 被挡下 {[e['code'] for e in last['proposalErrors']]}" if last["proposalErrors"] else "")
            )
            if last["proposal"]:
                break

        record(
            "这一轮确实由模型生成(不是本地规则兜底)",
            all(source != "rule_fallback" for source in seen_sources),
            f"来源:{seen_sources}",
        )
        record(
            "整段对话都没有降级",
            not any(seen_degraded),
            f"每一轮的 degraded:{seen_degraded}",
        )
        # 「用了 OpenJiuwen」这句话必须由**实际走过的路**支撑。direct_llm 也是真模型,
        # 但它不是 OpenJiuwen —— 那时候这一条会红,而不是被含糊过去。
        record(
            "走的是 openJiuwen 而不是直连 LLM",
            last["source"] == "openjiuwen",
            f"source={last['source']}",
        )
        record(
            "AI 拿到了每周时间预算(6 小时 = 360 分钟)",
            last["brief"]["weeklyAvailableMinutes"] == 360,
            f"weeklyAvailableMinutes={last['brief']['weeklyAvailableMinutes']}",
        )
        return last

    async def confirm(self, proposal_id: str, label: str, key: str | None = None) -> dict:
        sent = await self.client.post(
            f"/api/workspaces/{self.workspace}/proposals/{proposal_id}/confirm",
            json={"idempotencyKey": key or str(uuid.uuid4())},
            headers=self.auth(),
            timeout=120.0,
        )
        if sent.status_code != 200:
            record(label, False, f"HTTP {sent.status_code} {short(sent.text, 250)}")
            raise RuntimeError("确认失败")
        return sent.json()

    async def plan(self) -> dict:
        got = await self.client.get(f"/api/workspaces/{self.workspace}/plan", headers=self.auth())
        if got.status_code != 200:
            raise RuntimeError(f"读计划失败:HTTP {got.status_code}")
        return got.json()

    async def confirm_first_plan(self, last: dict) -> dict:
        print("\n3. 确认模型给的这份计划")
        proposal = last["proposal"]
        note(f"提案 {proposal['id']} · {proposal['itemCount']} 项 · 触发={proposal['triggerType']}")
        for item in proposal["items"][:8]:
            note(f"  {item['ordinal']}. [{item['op']}] {short(item['summary'], 80)}")

        key = str(uuid.uuid4())
        applied = await self.confirm(proposal["id"], "用户点击确认后写入成功", key)
        record(
            "确认写入成功,版本号前进",
            applied["applied"]["revisionVersion"] >= 1,
            f"新建 {applied['applied']['nodesCreated']} 节点 / 更新 {applied['applied']['nodesUpdated']}"
            f" / 版本 {applied['applied']['revisionVersion']}",
        )

        # 幂等:同一个键再点一次。用户双击"确认"必须是**同一件事**,不能建两遍。
        #
        # 判据是**库里的计划没变**,不是"返回的 nodesCreated 是 0"。
        # 重放返回的是**当初那次的结果**(12 个节点),它本来就该是 12 —— 幂等的语义是
        # "同一次操作的同一份回执",不是"这次什么也没干"。第一次跑的时候我按 0 断言,
        # 那是我的假设错了,不是实现错了。真正要防的是"双击建了两遍计划",
        # 而那个只能看库里到底有多少个节点。
        before_replay = await self.plan()
        again = await self.client.post(
            f"/api/workspaces/{self.workspace}/proposals/{proposal['id']}/confirm",
            json={"idempotencyKey": key},
            headers=self.auth(),
        )
        replayed = again.json() if again.status_code == 200 else {}
        after_replay = await self.plan()
        record(
            "重复点击:200 + replayed=true,且计划没有被写第二遍",
            again.status_code == 200
            and replayed.get("replayed") is True
            and len(after_replay["nodes"]) == len(before_replay["nodes"])
            and after_replay["revisionVersion"] == before_replay["revisionVersion"],
            f"HTTP {again.status_code} replayed={replayed.get('replayed')} "
            f"节点 {len(before_replay['nodes'])}→{len(after_replay['nodes'])} "
            f"版本 {before_replay['revisionVersion']}→{after_replay['revisionVersion']}",
        )
        return applied

    async def views_share_one_state(self, before: dict) -> dict:
        print("\n4. 路径 / 时间线 / 任务 / 周计划 / 日计划读的是同一份状态")
        after = await self.plan()
        nodes = after["nodes"]
        by_type: dict[str, int] = {}
        for node in nodes:
            by_type[node["nodeType"]] = by_type.get(node["nodeType"], 0) + 1
        record(
            "确认之后计划里真的长出了结构(不只是根节点)",
            len(nodes) > 1,
            f"{len(nodes)} 个节点 · {by_type}",
        )
        record(
            "根目标以外的节点都是 AI 提的,来源可查",
            all(node["origin"] == "ai" for node in nodes if node["parentId"]),
            f"user 来源 {sum(1 for n in nodes if n['origin'] == 'user')} 个",
        )
        # 路径图、时间线、任务列表读的都是这一份 `nodes`;周计划与日计划读 `sessions`。
        # 所以"同步出现"在这一层是结构性的 —— 它由**只有一个来源**保证,不靠界面自觉。
        note("三个视图(路径/时间线/任务)读的是同一个 GET /plan,不存在第二份投影")
        record("排期之前 sessions 为空(空的含义是'还没排过',不是'没有安排')", after["sessions"] == [], f"{len(after['sessions'])} 场")
        return after

    async def schedule(self) -> dict:
        print("\n5. 排出周计划与日计划")
        previewed = await self.client.post(
            f"/api/workspaces/{self.workspace}/schedule/preview", headers=self.auth(), timeout=120.0
        )
        if previewed.status_code != 200:
            record("预览排期", False, f"HTTP {previewed.status_code} {short(previewed.text, 250)}")
            raise RuntimeError("排期预览失败")
        preview = previewed.json()
        record(
            "预览是只读的:它给出场次,但计划里还没有任何安排",
            len(preview["sessions"]) > 0 and (await self.plan())["sessions"] == [],
            f"{len(preview['sessions'])} 场待排 · 版本 {preview['scheduleVersion'][:12]}…",
        )
        if preview["gaps"]:
            note(f"排不进去的有 {len(preview['gaps'])} 项,卡在:{[g['bindingConstraint'] for g in preview['gaps']]}")

        applied = await self.client.post(
            f"/api/workspaces/{self.workspace}/schedule/apply",
            json={"scheduleVersion": preview["scheduleVersion"], "idempotencyKey": str(uuid.uuid4())},
            headers=self.auth(),
            timeout=120.0,
        )
        if applied.status_code != 200:
            record("应用排期", False, f"HTTP {applied.status_code} {short(applied.text, 250)}")
            raise RuntimeError("应用排期失败")
        result = applied.json()
        record(
            "应用之后计划里真的有安排了",
            result["applied"]["created"] > 0 and len((await self.plan())["sessions"]) > 0,
            f"新增 {result['applied']['created']} 场 / 更新 {result['applied']['updated']}"
            f" / 取消 {result['applied']['canceled']}",
        )
        return result

    async def survives_restart(self, before: dict) -> dict:
        print("\n6. 重启后端进程,计划还在不在")
        workspace = self.workspace
        self.server.stop()

        note("进程已停。同一份数据库文件上重新起一个 —— 内存里的东西这时候一定没了。")
        self.server.start()
        self.client.base_url = self.server.base  # 新进程可能落在另一个端口上
        ready = await self.wait_ready()
        note(f"/ready -> {ready}")

        # 令牌是存在库里的(`auth_sessions`),所以重启之后必须仍然认得出这个账户。
        # 这一条一开始写成了硬编码的 `True` —— 那不是断言,是注释。
        probe = await self.client.get("/api/workspaces", headers=self.auth())
        record(
            "重启后旧令牌仍然有效(会话也在库里)",
            probe.status_code == 200 and any(item["id"] == workspace for item in probe.json()),
            f"HTTP {probe.status_code} · 空间 {workspace[:8]}… 在列表里",
        )

        after = await self.plan()
        before_nodes = {node["id"]: node["title"] for node in before["nodes"]}
        after_nodes = {node["id"]: node["title"] for node in after["nodes"]}
        record(
            "重启后节点逐个还在,标题一致",
            before_nodes == after_nodes and len(after_nodes) > 1,
            f"{len(after_nodes)} 个节点,revisionVersion {before['revisionVersion']} -> {after['revisionVersion']}",
        )
        record(
            "重启后排期还在",
            len(after["sessions"]) == len(before["sessions"]) > 0,
            f"{len(after['sessions'])} 场",
        )
        record(
            "重启后 weekly brief 还在(每周 360 分钟)",
            after["brief"]["weeklyAvailableMinutes"] == 360,
            str(after["brief"]["weeklyAvailableMinutes"]),
        )
        return after

    async def execution_feedback(self, plan: dict) -> None:
        print("\n7. 记录一场的执行结果")
        session = plan["sessions"][0]
        key = str(uuid.uuid4())
        body = {
            "result": "partial",
            "idempotencyKey": key,
            "actualMinutes": max(1, session["plannedMinutes"] // 2),
            "completionRatio": 0.5,
            "delayReason": "这周临时有别的安排",
            "userFeedback": "做了一半,比想象中慢。",
        }
        recorded = await self.client.post(
            f"/api/sessions/{session['id']}/executions", json=body, headers=self.auth()
        )
        if recorded.status_code not in (200, 201):
            record("记录执行结果", False, f"HTTP {recorded.status_code} {short(recorded.text, 250)}")
            return
        payload = recorded.json()
        record(
            "写入成功且**显式**说明已落库(不是静默切内存)",
            payload.get("saved") is True,
            f"saved={payload.get('saved')} 场次状态={payload.get('session', {}).get('status')}",
        )

        # 同样的键再发一次:不该产生第二条记录。
        await self.client.post(f"/api/sessions/{session['id']}/executions", json=body, headers=self.auth())
        history = await self.client.get(f"/api/sessions/{session['id']}/executions", headers=self.auth())
        records = history.json()
        items = records["records"] if isinstance(records, dict) else records
        record(
            "同一个幂等键重发不产生第二条记录",
            len(items) == 1,
            f"{len(items)} 条记录",
        )

    async def adjust_from_conversation(self, before: dict) -> None:
        """场景里的最后一步:用户说"本周只剩 2 小时",AI 提出调整,确认后同步更新。"""
        print("\n8. 用户反馈「这周只剩 2 小时」,让 AI 调整")
        sent = await self.client.post(
            f"/api/workspaces/{self.workspace}/messages",
            json={
                "content": "这周临时有事,只剩 2 小时了,帮我调整一下计划。",
                "clientMessageId": str(uuid.uuid4()),
                "currentView": "schedule",
            },
            headers=self.auth(),
            timeout=180.0,
        )
        if sent.status_code != 200:
            record("发送调整请求", False, f"HTTP {sent.status_code} {short(sent.text, 250)}")
            return
        last = sent.json()
        print(f"    模型({last['source']}): {short(last['reply'], 180)}")
        if not last["proposal"]:
            # 如实记录:模型这一轮没给变更。**不伪造一份调整方案**。
            record(
                "模型给出了可执行的调整方案",
                False,
                f"这一轮没有提案。降级={last['degraded']}({last['degradedReason']}),"
                f"校验错误={[e['code'] for e in last['proposalErrors']]}",
            )
            return
        for item in last["proposal"]["items"][:6]:
            note(f"  {item['ordinal']}. [{item['op']}] {short(item['summary'], 80)}")

        applied = await self.confirm(last["proposal"]["id"], "确认调整方案")
        after = await self.plan()

        # 判据是**计划载荷自己的版本号前进了,而且内容真的变了**。
        #
        # 不能用 `applied.revisionVersion` 比大小:它记的是"这次写入**产生**了哪一版"
        # (`plan_revisions` 那一行),而 `/plan` 里的 `revisionVersion` 是空间当前的
        # 版本计数 —— 前者恒等于后者减一,是两套编号,不是同一件事。
        # 详见 `backend/contracts/proposal.py` 的 `AppliedChangeView`。
        #
        # 也不能断言"节点数变多":只挪截止日期的调整完全合法,节点数一个都不变
        # (第 9 步的 /replan 就是这种)。所以比的是节点内容的指纹。
        def fingerprint(plan: dict) -> dict:
            return {
                node["id"]: (node["title"], node["deadline"], node["estimateMinutes"])
                for node in plan["nodes"]
            }

        # 场次**不在这里断言**:确认只改计划结构,重新排期是用户下一次在「排期」里
        # 预览并应用时的事(第 5 步已经验过那条路)。这里只如实报出数字。
        record(
            "确认之后计划版本前进、内容真的变了",
            after["revisionVersion"] > before["revisionVersion"]
            and fingerprint(after) != fingerprint(before),
            f"版本 {before['revisionVersion']} -> {after['revisionVersion']}"
            f" · 写入产生的是第 {applied['applied']['revisionVersion']} 版"
            f" · 节点 {len(before['nodes'])} -> {len(after['nodes'])}"
            f" · 场次 {len(before['sessions'])} -> {len(after['sessions'])}(等用户下次预览再重排)",
        )

    async def replan_endpoint(self) -> None:
        """阶段 7 的 `/replan` —— 与对话那条路共用同一套校验与确认,不另开写路径。"""
        print("\n9. 按执行情况重规划(POST /replan)")
        sent = await self.client.post(
            f"/api/workspaces/{self.workspace}/replan", headers=self.auth(), timeout=180.0
        )
        if sent.status_code != 200:
            record("/replan 可用", False, f"HTTP {sent.status_code} {short(sent.text, 250)}")
            return
        payload = sent.json()
        # 线上格式是 camelCase(契约基类 `ApiModel` 用 `to_camel` 生成别名),
        # 所以这里读 `consultedModel` —— 写成 snake_case 会直接 KeyError。
        note(f"偏差 {len(payload['deviations'])} 条 · 请过模型={payload['consultedModel']}"
             f" · 降级={payload['degraded']}({payload['degradedReason']})")
        note(f"服务端的话:{short(payload.get('message'), 160)}")
        if payload["proposal"]:
            applied = await self.confirm(payload["proposal"]["id"], "/replan 的提案可确认")
            record(
                "/replan 提的变更走的是同一个确认事务",
                "revisionVersion" in applied["applied"],
                f"版本 -> {applied['applied']['revisionVersion']}",
            )
        else:
            # 没有偏差、或者模型这次没给出方案 —— 两种都是**如实**的结果。
            record(
                "/replan 在没有可调整之处时如实说明,不编一份方案",
                payload["proposal"] is None and bool(payload.get("message")),
                f"proposal=None,message={'有' if payload.get('message') else '无'}",
            )


async def main_async(server: Server, turns: int) -> None:
    import httpx

    run = Acceptance(server, turns)
    async with httpx.AsyncClient(base_url=server.base, timeout=60.0) as client:
        run.client = client
        ready = await run.wait_ready()
        print(f"0. 后端就绪 {ready}")
        record("数据库是 SQLite 临时库(没有碰 data/zhitu_dev.db)", "accept8" in ready.get("database", ""), short(ready.get("database"), 70))
        record("模型 key 已配置(密钥本身不打印)", True, f"reasoner={ready.get('reasoner')}")

        await run.register_and_create()
        last = await run.converse()
        if not last.get("proposal"):
            record(
                "模型提了一份待确认的变更",
                False,
                f"{turns} 轮之内没拿到提案。最后一轮:降级={last['degraded']}"
                f"({last['degradedReason']}) 校验错误={[e['code'] for e in last['proposalErrors']]}",
            )
            print("\n没有提案就无法继续后面的步骤(确认 / 排期 / 重规划都挂在它上面)。")
            return
        record("模型提了一份待确认的变更", True, f"{last['proposal']['itemCount']} 项")

        await run.confirm_first_plan(last)
        plan = await run.views_share_one_state(last)
        await run.schedule()
        plan = await run.plan()
        plan = await run.survives_restart(plan)
        await run.execution_feedback(plan)
        await run.adjust_from_conversation(plan)
        await run.replan_endpoint()


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 8 验收:整条闭环在真实进程 + 真实模型上跑一遍")
    parser.add_argument("--turns", type=int, default=5, help="多轮对话最多几轮")
    parser.add_argument("--port", type=int, default=8137, help="验收用的端口")
    parser.add_argument("--keep", action="store_true", help="保留临时库")
    options = parser.parse_args()

    print()
    print("阶段 8 验收:对话 -> 计划 -> 确认 -> 排期 -> 重启 -> 反馈 -> 调整")
    print()

    # ---- 临时库。**必须在 `import backend` 之前**把环境指过去 ----
    #
    # 这条次序踩过一次,而且踩得很隐蔽:`settings` 是模块级单例,`alembic/env.py`
    # 会 import 它。先 import 了 backend 再改 `DATABASE_URL`,settings 已经拿着
    # `data/zhitu_dev.db` 了,于是**迁移跑在了开发库上**,临时库里一张表都没有,
    # 后端起不来 —— 而脚本会把它报成"迁移失败",看起来像迁移的问题。
    tmp = Path(tempfile.mkdtemp(prefix="zhitu-accept8-"))
    database_url = f"sqlite+aiosqlite:///{(tmp / 'accept8.db').as_posix()}"
    os.environ["DATABASE_URL"] = database_url
    os.environ["APP_ENV"] = "development"
    os.environ["DB_ECHO"] = "0"

    # ---- 再确认真的有模型 key。没有就拒绝,而不是降级成一个假验收 ----
    sys.path.insert(0, str(REPO_ROOT))
    from backend.core.config import settings

    if not settings.llm_api_key:
        shutil.rmtree(tmp, ignore_errors=True)
        print("  [不通过] .env 里没有 LLM_API_KEY,这个验收无法进行。")
        print()
        print("  阶段 8 验的就是「模型真的被调用」这件事。没有 key 时跑出来的")
        print("  「通过」会是本地规则兜底的结果,那正是这个脚本要防的假验收。")
        print("  把 key 填进 .env 再跑一次。")
        return 1
    print(f"  模型:{settings.llm_model} @ {settings.llm_base_url} · reasoner={settings.agent_reasoner}")
    print("  (key 已读取,**不会**打印,也不会进模型上下文)")
    # 这一行是给"本次验收有没有动到开发库"这个问题一个可核对的答案:
    # 打印的是**实际生效**的那个 `DATABASE_URL`,不是我以为的那个。
    print(f"  库(实际生效):{settings.database_url}")
    if "accept8" not in settings.database_url:
        print()
        print("  [不通过] 生效的库不是临时库 —— 停下来,别拿你的真实计划做验收。")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1
    print()

    from alembic import command
    from alembic.config import Config

    print("0. 在临时库上跑 Alembic 迁移")
    command.upgrade(Config(str(REPO_ROOT / "backend" / "alembic.ini")), "head")
    con = sqlite3.connect(tmp / "accept8.db")
    tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    con.close()
    record("迁移建库成功", tables > 0, f"{tables} 张表 · {database_url}")
    print()

    server = Server(free_port(options.port), database_url, tmp / "backend.log")
    server.start()
    try:
        asyncio.run(main_async(server, options.turns))
    except Exception as cause:
        record("验收过程未中断", False, f"{type(cause).__name__}: {short(cause, 300)}")
    finally:
        log = server.stop()
        if log.strip():
            print("\n--- 后端进程最后输出 ---")
            print("\n".join(log.strip().splitlines()[-12:]))
        if options.keep:
            print(f"\n临时库保留在:{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print()
    print("=" * 68)
    if failed:
        print(f"结果:有 {len(failed)} 项没通过")
        for name in failed:
            print(f"  - {name}")
        print("=" * 68)
        print()
        print("把上面整段输出贴给我即可。")
        return 1
    print(f"结果:全部 {len(results)} 项通过。阶段 8 的整条闭环成立。")
    print("=" * 68)
    print()
    print("说明:这次跑的是**真实模型 + 真实进程**。上面的模型回复片段是证据,")
    print("      `source=openjiuwen` 那一行说明走的是 OpenJiuwen 而不是直连。")
    print("      你的开发数据库 data/zhitu_dev.db 未被本次验收触碰。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

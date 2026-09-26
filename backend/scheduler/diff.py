"""`schedule_version`,以及两次方案之间的差异。

## 版本号是给"用户预览过的那一版"用的

`POST /schedule/apply` 要校验"即将写进数据库的,正是用户在屏幕上点头的那一份"。中间
隔着一次网络往返、一次界面渲染和一次点击,而这段时间里计划可能被别的东西改了(用户
自己勾了个完成、另一个人在同一账号上确认了一份提案)。

做法是把**输入**规范化后哈希一次,而不是哈希输出:输出是输入的函数,哈希输入代价更小,
而且"输入变了"正是需要重算的那件事。输入没变而输出变了,那是算法坏了 —— 那该由
`test_same_input_same_bytes` 抓,不该由版本号兜。

## 规范化必须彻底

`json.dumps(..., sort_keys=True)` 只解决字典键的顺序;列表的顺序、`Decimal` 与 `int`
的区别、`date` 与 `datetime` 的区别都还要自己处理。少处理一样,版本号就会在输入**没有
实质变化**时抖一下 —— 用户看到的是一句"计划已经变了,请重新预览",而他什么也没改。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal

from backend.scheduler.types import (
    ChurnSummary,
    ScheduleRequest,
    ScheduleResult,
)


def _normalize(value: object) -> object:
    """把任意值折成纯 JSON 且**稳定**的形式。"""
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in sorted(value.items(), key=lambda p: str(p[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_normalize(item) for item in value]
        # 集合没有顺序,所以排序;列表的顺序有意义(比如场次序号),所以保留。
        if isinstance(value, (set, frozenset)):
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str))
        return items
    if isinstance(value, Decimal):
        # `Decimal("0.80")` 与 `Decimal("0.8")` 是同一个数,规范化成同一个字符串。
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # uuid / 枚举 / 其它:一律取它的规范字符串形式。写成 `str(value)` 而不是抛错,
    # 是因为这里的目标是"稳定",不是"只接受已知类型"。
    return str(value)


def canonical_payload(request: ScheduleRequest) -> dict[str, object]:
    """排期输入的规范形式。**`schedule_version` 唯一的输入。**"""
    return {
        "today": _normalize(request.today),
        "horizonDays": request.horizon_days,
        "planRevision": request.plan_revision,
        "profile": _normalize(asdict(request.profile)),
        "windows": _normalize([asdict(window) for window in request.windows]),
        "exceptions": _normalize([asdict(item) for item in request.exceptions]),
        "nodes": _normalize(
            sorted((asdict(node) for node in request.nodes), key=lambda item: str(item["id"]))
        ),
        "dependencies": _normalize(
            sorted(
                (asdict(dependency) for dependency in request.dependencies),
                key=lambda item: (str(item["predecessor_id"]), str(item["successor_id"])),
            )
        ),
        "existing": _normalize(
            sorted((asdict(session) for session in request.existing), key=lambda item: str(item["id"]))
        ),
        "executions": _normalize(
            sorted(
                (asdict(fact) for fact in request.executions),
                key=lambda item: (str(item["node_id"]), str(item["session_id"])),
            )
        ),
    }


def schedule_version(request: ScheduleRequest) -> str:
    """输入的指纹。用 blake2b 而不是 sha256:这里没有对抗性,只要短、快、够散。"""
    body = json.dumps(
        canonical_payload(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.blake2b(body, digest_size=16).hexdigest()


def changes_between(previous: ScheduleResult, current: ScheduleResult) -> ChurnSummary:
    """两次方案之间,用户会看到什么变化。

    两个用途:重排之前(给用户看"这次会动多少"),以及复盘时(回答"这个月的计划被
    调过几次")。**刻意按"用户会发现的差异"来数**,不是按内部状态变化来数 ——
    一个场次从 60 分钟变成 60 分钟但序号挪了位,用户看不出来,就不该算成一次改动。
    """
    def index(result: ScheduleResult) -> dict[tuple[str, str], tuple[date, int]]:
        table: dict[tuple[str, str], tuple[date, int]] = {}
        for session in result.sessions:
            if session.session_id is None:
                continue
            table[(str(session.session_id), str(session.node_id))] = (
                session.scheduled_date,
                session.planned_minutes,
            )
        return table

    before, after = index(previous), index(current)
    moved = sum(
        1
        for key, value in before.items()
        if key in after and after[key] != value
    )
    created = sum(1 for session in current.sessions if session.session_id is None)
    cancelations = set(map(str, current.cancelations)) - set(map(str, previous.cancelations))
    kept = sum(1 for key in before if key in after and after[key] == before[key])
    return ChurnSummary(moved=moved, created=created, canceled=len(cancelations), kept=kept)


__all__ = ["canonical_payload", "changes_between", "schedule_version"]

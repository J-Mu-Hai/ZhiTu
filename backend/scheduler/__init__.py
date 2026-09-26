"""排期算法。**一个叶子包:只依赖标准库。**

## 为什么它必须是一个叶子

排期是"根据执行情况持续调整"这句话里唯一确定性的部分 —— 给定同一份输入,它必须
给出同一份输出,否则用户会看到计划在没人动它的时候自己变。要能验证这一点,它就不能
碰数据库、不能调模型、不能读时钟:一旦它自己取"今天","同输入同输出"就只在同一天
成立,而测试也只好去 mock 时间。

所以这里的纪律是硬性的,由 `tests/unit/scheduler/test_purity.py` 用 AST 扫描兜着:

- 不 import sqlalchemy / httpx / backend.agent / backend.services / backend.api
- 不 import `backend.db.*`(**包括 enums** —— 导入它的子模块会先执行 `backend/db/__init__.py`,
  而那里会建 sqlalchemy 引擎。见 `types.py` 顶部)
- 不调用 `date.today()` / `datetime.now()` —— "今天"由调用方作为参数传进来

## 模块分工

| 文件 | 职责 |
|---|---|
| `types.py` | 输入/输出的形状。零依赖的取值枚举与 frozen dataclass |
| `errors.py` | 错误码与坏输入。区分"排不出来"(它是结果的一部分)与"输入不成立"(抛) |
| `calendar.py` | 日期算术 + 每天的分钟池(可用时段、例外、每日上限) |
| `graph.py` | 拓扑序与 `earliest_start`(依赖与截止日那条规则) |
| `capacity.py` | 池子的记账与可行性闸门 + 缺口的结构化描述 |
| `pack.py` | 把一个任务的工时切成若干场次 |
| `schedule.py` | 主流程:`simulate()` 与三个补救选项的实测 |
| `diff.py` | 与上一版方案的差异(改了几场、新增几场)与 `schedule_version` |
"""

from backend.scheduler.schedule import recovery_options, simulate
from backend.scheduler.types import (
    AvailabilityWindow,
    CapacityProfile,
    ChurnSummary,
    DayException,
    ExecutionFact,
    ExistingSession,
    RecoveryOption,
    ScheduleRequest,
    ScheduleResult,
)

__all__ = [
    "AvailabilityWindow",
    "CapacityProfile",
    "ChurnSummary",
    "DayException",
    "ExecutionFact",
    "ExistingSession",
    "RecoveryOption",
    "ScheduleRequest",
    "ScheduleResult",
    "recovery_options",
    "simulate",
]

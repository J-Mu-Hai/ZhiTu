"""错误码,以及两类**完全不同**的坏情况。

## "排不出来"不是错误,"输入不成立"才是

这两件事在代码里长得像,在产品里完全相反:

- **排不出来**(容量不足)是一个**正常结果**。用户这周只有 4 小时,而计划要 8 小时 ——
  算法答"有 4 小时排不进去,卡在每日上限上",并给出三条出路。它是结果的正常一部分,
  所以它是 `CapacityGap`,走返回值,不抛异常。
- **输入不成立**(依赖成环、引用了不存在的节点)是**代码或数据的 bug**。这时任何输出
  都是坏的,而且坏得很安静:一个成环的图算出来的顺序依赖字典的插入顺序,今天和明天的
  结果可以不同。所以它必须抛。

把前者写成异常的话,调用方会长出 `try/except` 并顺手 `pass` 掉它 —— 于是"这周排不下"
变成一份看起来正常、实际漏了几场的计划。把后者写成返回值的话,没人会去读那个字段。

## 不变量

`SchedulerInvariantError` 继承 `AssertionError`,并且**恒开**(不靠 `-O` 之外的环境)。
它表示"算法自己坏了":跨空间每日总量超过上限、场次落到了截止日之后、挪动了冻结场次。
这些在正确的实现里不可能发生 —— 一旦发生,宁可让整个请求 500,也不要写入一份
违反不变量、而用户无从察觉的计划。
"""

from __future__ import annotations

from enum import StrEnum


class ScheduleErrorCode(StrEnum):
    """会出现在 `CapacityGap.reason_code` 里的取值。**闭集。**"""

    #: 截止日之前的日子都排满了。
    NO_CAPACITY_BEFORE_DEADLINE = "NO_CAPACITY_BEFORE_DEADLINE"
    #: 截止日已经过去了,这份工时无论如何安排不进去。
    DEADLINE_ALREADY_PASSED = "DEADLINE_ALREADY_PASSED"
    #: 前置任务排在截止日之后,链式推下来它连一天都轮不到。
    DEPENDENCY_CHAIN_UNSATISFIABLE = "DEPENDENCY_CHAIN_UNSATISFIABLE"
    #: 用户锁定的场次把池子占住了。**点名而不是偷偷挪开它** —— 锁定就是钉住。
    LOCKED_SESSION_CONFLICT = "LOCKED_SESSION_CONFLICT"
    #: 这个节点没有预计工时。不知道要做多久就排不出"哪天做多久"。
    NO_ESTIMATE = "NO_ESTIMATE"
    #: 当天的池子还有空,但**放不下一场最小的**场次(比如只剩 10 分钟,而最小一场 15)。
    BELOW_MIN_SESSION = "BELOW_MIN_SESSION"
    #: 排期视界到头了(未来 N 天内没有空位)。与"截止日之前排不下"不同:
    #: 那一个是"来不及",这一个是"我算得还不够远"。
    HORIZON_EXHAUSTED = "HORIZON_EXHAUSTED"


class BindingConstraint(StrEnum):
    """真正卡住的那个约束。规则 (h) 要求点名,否则缺口是一堵数字墙。

    用户看到"这周排不下"时能做的三件事(少做点 / 延期 / 多投入)分别对应这里的
    不同取值 —— 而如果不说清楚卡在哪,他只会盲点"增加投入"。
    """

    DAILY_MAX = "DAILY_MAX"
    WEEKLY_BUDGET = "WEEKLY_BUDGET"
    AVAILABILITY = "AVAILABILITY"
    DEADLINE = "DEADLINE"
    DEPENDENCY = "DEPENDENCY"
    LOCKED_SESSIONS = "LOCKED_SESSIONS"
    MIN_SESSION_SIZE = "MIN_SESSION_SIZE"
    NO_ESTIMATE = "NO_ESTIMATE"
    HORIZON = "HORIZON"


class SchedulerInputError(ValueError):
    """输入本身不成立。**不产生任何输出**,调用方必须让它炸出来。"""

    code = "SCHEDULER_INPUT_INVALID"


class DependencyCycleError(SchedulerInputError):
    """依赖成环。

    写路径(`node_service.add_dependency`)已经用递归可达性挡过一遍,所以走到这里
    说明数据被别的方式写坏了 —— 更要说出来。成环的图算出来的顺序取决于字典的插入
    顺序:同一份输入今天排出一个样、明天另一个样,而"计划在没人动它的时候自己变"
    是最难排查的一类问题。
    """

    code = "DEPENDENCY_CYCLE"


class UnknownNodeReferenceError(SchedulerInputError):
    """依赖或场次指向了一个不在本次请求里的节点。

    这不是"忽略掉就好"的小事:被忽略的依赖意味着"后置任务排在了前置任务之前",
    而被忽略的场次意味着那段时间凭空空了出来 —— 两者都会让排出来的计划看起来很合理。
    """

    code = "UNKNOWN_NODE_REFERENCE"


class SchedulerInvariantError(AssertionError):
    """算法违反了它自己声称的性质。继承 `AssertionError`,因为它就是一份断言。"""

    code = "SCHEDULER_INVARIANT_VIOLATED"


__all__ = [
    "BindingConstraint",
    "DependencyCycleError",
    "ScheduleErrorCode",
    "SchedulerInputError",
    "SchedulerInvariantError",
    "UnknownNodeReferenceError",
]

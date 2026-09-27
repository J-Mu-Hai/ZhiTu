"""业务错误。

每个错误都带一个**稳定的 code**。客户端按 code 分支,不按 message 文案分支 ——
文案会改,code 不会。

`http_status` 放在错误类上而不是在路由里逐条 if/else:状态码是"这个业务错误意味着
什么 HTTP 结果",属于错误本身的一部分。放一处,就不会出现同一类错误在不同路由里
返回不同状态码。
"""

from __future__ import annotations


class DomainError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def to_body(self) -> dict:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}

    def to_headers(self) -> dict[str, str]:
        """这个错误附带的响应头。默认没有。

        `WWW-Authenticate` 与 `Retry-After` 都属于"错误本身的一部分"——放在这里,
        就不会出现"429 只在某个路由里带了 Retry-After"这种不一致。
        """
        return {}


# ---------------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------------
class InvalidInput(DomainError):
    """形状合法、但业务上不接受(例如空间标题只有空白)。

    与 `RequestInvalid` 的分工:这一条是**领域**判断,400;那一条是**结构**判断,422。
    两个不同的状态码配两个不同的 code,客户端不必去猜"同样一个 code 为什么这次是 400
    上次是 422"。
    """

    code = "INVALID_INPUT"
    http_status = 400


class RequestInvalid(DomainError):
    """请求体不符合契约(缺字段、类型不对、格式不合法)。由 422 处理器抛出。"""

    code = "REQUEST_INVALID"
    http_status = 422


class EmailAlreadyRegistered(DomainError):
    code = "EMAIL_ALREADY_REGISTERED"
    http_status = 409


class PasswordTooWeak(DomainError):
    code = "PASSWORD_TOO_WEAK"
    http_status = 400


# ---------------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------------
class InvalidCredentials(DomainError):
    """邮箱不存在与密码错误返回**同一个**错误。

    分开会让接口变成账号枚举器:攻击者可以拿一堆邮箱逐个试,凭返回的差异筛出哪些
    邮箱注册过。用户看到的文案也一样,不区分。
    """

    code = "INVALID_CREDENTIALS"
    http_status = 401


class Unauthenticated(DomainError):
    code = "UNAUTHENTICATED"
    http_status = 401


class SessionExpired(DomainError):
    code = "SESSION_EXPIRED"
    http_status = 401


class TokenReuseDetected(DomainError):
    """已被轮换掉的令牌被再次使用 —— 按泄露处理,整族撤销。"""

    code = "TOKEN_REUSE_DETECTED"
    http_status = 401


class RateLimited(DomainError):
    """失败次数超限。

    带 `Retry-After`,客户端因此能明确告诉用户"请等 3 分钟再试",而不是笼统地说
    "操作太频繁"然后让用户自己乱猜。
    """

    code = "RATE_LIMITED"
    http_status = 429

    def to_headers(self) -> dict[str, str]:
        seconds = self.details.get("retryAfterSeconds")
        if not isinstance(seconds, (int, float)):
            return {}
        return {"Retry-After": str(int(seconds) + 1)}


# ---------------------------------------------------------------------------------
# 资源
# ---------------------------------------------------------------------------------
class NotFound(DomainError):
    code = "NOT_FOUND"
    http_status = 404


class WorkspaceNotFound(DomainError):
    """空间不存在,或不属于当前用户。

    **两种情况刻意返回同一个错误与同一个状态码。** 如果"是别人的空间"返回 403、
    "不存在"返回 404,那么任何人拿 id 逐个试就能测绘出系统里有哪些空间存在。
    这也是权限测试里"B 拿 A 的 id 期望 404 而不是 403"的原因。
    """

    code = "WORKSPACE_NOT_FOUND"
    http_status = 404


class NotImplementedYet(DomainError):
    code = "NOT_IMPLEMENTED"
    http_status = 501


# ---------------------------------------------------------------------------------
# 提案
#
# 这一组错误的状态码全部是 409,除了"找不到"是 404。理由:它们描述的都是
# "**你发的这个请求本身没错,但它此刻做不了**" —— 提案已经被处理过了、提案基于的
# 计划版本已经旧了、数据库里的节点和提案对不上了。这些都是冲突,不是客户端的错,
# 也不是服务端的错。用 400 会让客户端以为要改请求;用 500 会让人去查日志。
# ---------------------------------------------------------------------------------
class ProposalNotFound(DomainError):
    """提案不存在,或不属于当前用户的空间。

    与 `WorkspaceNotFound` 同一条纪律:两种情况返回同一个错误。若"别人的提案"
    返回 403,任何人都能拿 id 试出系统里有哪些提案。
    """

    code = "PROPOSAL_NOT_FOUND"
    http_status = 404


class ProposalNotActionable(DomainError):
    """这份提案已经不是"等你确认"的状态了 —— 已经确认过、被拒绝过、或校验失败过。

    界面上表现为"这份提案已经处理过了"。**不是错误页** —— 用户双击确认时,
    第二次点击落在这一条上,而他看到的东西应该和第一次一样。
    """

    code = "PROPOSAL_ALREADY_DECIDED"
    http_status = 409


class ProposalExpired(DomainError):
    """提案放太久了。

    过期是有意的:一份三天前基于"每周 6 小时"排的计划,今天确认时用户的时间预算
    可能早就变了。让它过期,用户就必须重新生成 —— 那份新的会基于今天的条件。
    """

    code = "PROPOSAL_EXPIRED"
    http_status = 409


class StaleBaseRevision(DomainError):
    """提案生成之后,计划被改过了。

    **这一条是"不能直接覆盖用户的改动"的落点。** 用户自己勾完成、编辑了节点、
    或者确认了另一份提案,都会让 `current_revision_version` 前进。此时把一份基于
    旧版本的提案照单应用,会静默回退掉用户刚做的那些改动 —— 而界面上不会有任何
    迹象。所以这里必须挡住,让用户看到"计划已经变了,请重新生成"。
    """

    code = "STALE_BASE_REVISION"
    http_status = 409


class ProposalNoLongerValid(DomainError):
    """提案里的变更放到**现在的**数据上已经不成立了。

    与 `StaleBaseRevision` 的区别:那个是"版本号对不上",这个是"版本号碰巧对上了,
    但重新校验发现某条变更现在会写出坏数据"(比如它要引用的那个节点已经被删了)。
    两者都要挡,但给出的话不一样:版本对不上是"有别的改动",校验失败是"这一条本身
    现在不成立了"。
    """

    code = "PROPOSAL_NO_LONGER_VALID"
    http_status = 409


class NodeNotFound(DomainError):
    """这个节点不在请求指向的那个空间里。

    与 `WorkspaceNotFound` 同样的理由:**别人的节点**和**不存在的节点**返回同一个
    错误。分开返回的话,拿 id 逐个试就能测绘出别人空间里有哪些节点。
    """

    code = "NODE_NOT_FOUND"
    http_status = 404


class SessionNotFound(DomainError):
    """这场安排不存在,或者不属于你。

    与 `WorkspaceNotFound` / `NodeNotFound` 是同一条纪律:不存在与不属于别人返回
    同一个错误与同一个状态码。分开返回的话,拿 id 逐个试就能测绘出系统里有哪些场次。
    """

    code = "SESSION_NOT_FOUND"
    http_status = 404


class RootNodeProtected(DomainError):
    """根目标是这个空间存在的理由,删不掉。

    AI 提案那条路径上同一个约束叫 `CANNOT_DELETE_ROOT`(它是提案校验的一个错误码,
    出现在 `proposalErrors` 里)。这里是一个 HTTP 错误,因为用户直接点了删除 ——
    没有"提案"这个中间层可以把错误码放进去。
    """

    code = "CANNOT_DELETE_ROOT"
    http_status = 409


class NodePurged(DomainError):
    """这个节点是被**彻底删除**的,恢复不了。

    与"上层还在归档里"(`ParentArchived`)分开成两个错误,因为用户能做的事不一样:
    这一条没有别的办法(要拿回来只能重新建);那一条可以先把上层恢复出来再试。
    合成一个"恢复失败"会让界面只能给一句没法照做的提示。
    """

    code = "NODE_PURGED"
    http_status = 409


class ParentArchived(DomainError):
    """要恢复的这个节点,它的父节点还在归档里 —— 先恢复上面那一支。

    为什么这不是"顺手把父亲也恢复了":用户点的是这一行的"恢复",父亲的归档是另一次
    操作、可能带走了另一片东西。替他做决定的结果是他以为只回来了一项,
    实际上回来了一片 —— 而那一片里可能有他当时特意归档掉的东西。

    不拒绝的后果更糟:一个活节点挂在归档节点下面,在**任何界面上都不可达**
    (父节点不出现,子节点就没人能导航到),它真的存在却永远找不到入口。
    """

    code = "PARENT_ARCHIVED"
    http_status = 409


class DependencyRejected(DomainError):
    """手工加一条依赖,但这条边会让计划出现环。

    和提案路径的区别:提案是**整批**校验、全成或全不成;这里是用户一次加一条边,
    所以拒绝的就是这一条,其余计划原样不动。同一条规则(`find_cycle`)在两处复用,
    免得"AI 加的依赖查环、用户加的依赖不查"。
    """

    code = "DEPENDENCY_CYCLE"
    http_status = 409


class InformationNodeNotSchedulable(DomainError):
    """想拿一个信息主题当排期依赖的端点。

    规范 §2.5:仅作为信息的节点不能成为硬排期依赖端点。理由是信息的**语义**:
    "我知道了我排名 38"不是一件有始有终、可以被前置的事情 —— 一条
    "「查完排名」完成后才能开始「写材料」"的边,前半截永远不会有"完成"的那一刻。

    ## 为什么必须在**写入时**拒,而不是在排期时忽略

    排期查询已经按 `purpose` 过滤掉了信息节点(见 `schedule_service`),所以这条边
    就算存在,也会在算排期时被静默丢掉 —— `live_ids` 里没有它,依赖图里那一端
    就凭空消失了。**静默丢掉一条用户亲手连的边**,和拒绝他连这条边,前者糟得多:
    用户看到边在画布上、在排期里毫无作用,而没有任何一处告诉他为什么。

    另一个方向的错也很容易犯:把它当成"未完成"的前置,于是**整个后继链永远排不出来**。
    那表现为"我的计划一直说排不下",而原因在一条用户早就忘了的边上。
    """

    code = "INFORMATION_NODE_NOT_DEPENDABLE"
    http_status = 409


class RelationNotFound(DomainError):
    """这条关系不存在,或者不在你能碰的空间里。

    与 `NodeNotFound` / `WorkspaceNotFound` 同一条纪律:两种情况返回同一个错误与同一个
    状态码,否则拿 id 逐个试就能测绘出别人空间里有哪些边。
    """

    code = "RELATION_NOT_FOUND"
    http_status = 404


class RelationRejected(DomainError):
    """这条边现在不能这么改。

    目前只有一个来源:**把一条边改成前置关系(或把前置关系改成别的)**。那意味着在
    `node_relations` 与 `dependencies` 两张表之间搬家,而后者没有说明列 —— 搬过去,
    用户写在这条边上的解释就没了。宁可不做,也不要静默丢掉别人写的东西。

    用 400 而不是 409:它不是"此刻做不了"(换个时间也不行),而是"这个请求本身
    在这里没有意义"。客户端按 code 分支,把这条渲染成"这一版不支持"。
    """

    code = "RELATION_TYPE_CHANGE_UNSUPPORTED"
    http_status = 400


class IdempotencyKeyReused(DomainError):
    """同一个幂等键配了不同的请求体。

    正常情况下一个键只对应一次"确认这份提案"。键重复但内容不同,说明客户端把键
    用错了地方(比如所有请求共用一个固定字符串)。这时如果照样返回上次的结果,
    用户会看到一份**别的提案**被"确认成功"。
    """

    code = "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY"
    http_status = 409


class ConcurrencyConflict(DomainError):
    """另一个请求抢先改了同一份提案。

    用户双击确认时会走到这里:两次请求都通过了前置检查,只有一个赢得了状态转移。
    输的那个不该拿到错误页 —— 处理方式是回读已经写好的结果并原样返回(200),
    所以这个错误只会在"赢家还没提交完"的极窄窗口里出现。
    """

    code = "CONCURRENCY_CONFLICT"
    http_status = 409


# ---------------------------------------------------------------------------------
# 排期
# ---------------------------------------------------------------------------------
class StaleSchedulePreview(DomainError):
    """用户预览过的那一份排期,放到此刻的数据上已经不是同一份了。

    与 `StaleBaseRevision` 是同一条纪律,只是对象不同:那个挡的是"提案基于旧的
    **节点版本**",这个挡的是"排期基于旧的**输入**"。输入包含节点、依赖、既有场次、
    执行记录、时间预算与可用时段 —— 任何一样变了,`schedule_version` 就对不上。

    **不能照样应用。** 用户点头的是他在屏幕上看到的那一份;输入变了之后重排出来的
    可能是另一份(任务挪到了别的日子),而他没有看过它。给一句"计划已经变了,请重新
    预览",比悄悄写下一份他没同意过的安排要好。
    """

    code = "STALE_SCHEDULE_VERSION"
    http_status = 409


# ---------------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------------
class DbUnavailable(DomainError):
    """数据库连不上或不可用。

    **这是本产品最不能含糊的一个错误。** 只要写入可能没成功,响应就必须说写入没成功 ——
    绝不允许"看起来保存了,其实在内存里"。用户据此决定要不要再录一次今天的学习记录;
    谎报成功会让那条记录永久消失,而用户以为它在。

    注意它**只覆盖"连不上/用不了"**这种基础设施故障(OperationalError / InterfaceError)。
    唯一约束冲突、外键违反之类的错误不会被翻译成它 —— 那些是代码问题,应该以 500 的
    原貌暴露出来,而不是伪装成"数据库暂时不可用"。

    ## `saved` 只说"写进去没有",不说别的

    写请求失败时响应体里带 `saved: false`,读请求不带这个字段(读没有"保存"这件事)。
    判断依据是 **HTTP 方法**,不是"这个路由会不会写" —— 后者要维护一张与路由表同步的
    清单,而它们一定会漂移。`HEAD` 与 `GET` 之外的方法一律视为可能有写入。
    """

    code = "DB_UNAVAILABLE"
    http_status = 503

    def __init__(self, message: str = "", *, saved: bool | None = None, **details: object) -> None:
        super().__init__(message, **details)
        self.saved = saved

    def to_body(self) -> dict:
        body = super().to_body()
        if self.saved is not None:
            body["saved"] = self.saved
        return body

"""注册、登录、轮换、登出、会话列表。

关于响应的两条纪律:

1. **明文令牌只在 register / login / refresh 的响应体里出现一次。** 之后无论查库还是
   查会话列表都拿不到它 —— 库里存的是 sha256,所以"库被读走"不等于"账号被登进去"。
2. **`UserProfile` 里没有 password_hash,响应模型里也没有任何 token_hash 字段。**
   这不是靠小心,是靠这些字段压根没被声明:即使服务层把一个 ORM 对象整个传回来,
   `from_attributes` 也只读声明过的字段。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import AuthContext, client_ip, get_auth_context, get_current_user
from backend.contracts.auth import (
    AuthResult,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SessionInfo,
    UserProfile,
)
from backend.core.security import CurrentUser
from backend.core.throttle import LOGIN_THROTTLE
from backend.db.session import get_db
from backend.services import auth_service
from backend.services.auth_service import IssuedSession
from backend.services.errors import InvalidCredentials, RateLimited

router = APIRouter()


def _auth_result(issued: IssuedSession) -> AuthResult:
    return AuthResult(
        token=issued.token,
        expires_at=issued.session.expires_at,
        absolute_expires_at=issued.session.absolute_expires_at,
        user=UserProfile.model_validate(issued.user),
    )


async def _issue(
    db: AsyncSession,
    request: Request,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
    timezone: str | None = None,
) -> AuthResult:
    """注册与登录共用的收尾。

    user_agent 与 ip 只用于会话列表里"这是哪台设备"的显示,不参与任何鉴权判断。
    """
    common = {
        "user_agent": request.headers.get("user-agent"),
        "ip": client_ip(request),
    }
    if display_name is not None:
        issued = await auth_service.register(
            db,
            email=email,
            password=password,
            display_name=display_name,
            timezone=timezone or "Asia/Shanghai",
            **common,
        )
    else:
        issued = await auth_service.login(db, email=email, password=password, **common)
    return _auth_result(issued)


@router.post(
    "/register",
    response_model=AuthResult,
    status_code=status.HTTP_201_CREATED,
    summary="注册并直接登录",
)
async def register(
    payload: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthResult:
    """注册成功即返回一张可用的令牌,省掉一次多余的登录往返。"""
    return await _issue(
        db,
        request,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        timezone=payload.timezone,
    )


@router.post("/login", response_model=AuthResult, summary="登录")
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthResult:
    """登录,带失败次数限流。

    限流键有两个,分别挡住两种不同的攻击:
    - **按邮箱**:盯住一个账号慢慢试密码(单账号暴破);
    - **按 IP**:用一个密码去试一大堆邮箱(撞库/喷洒)。

    只记失败,成功即清零 —— 正常用户打错几次密码不该被拦,而真正打得中密码的人
    也不需要再试了。

    限流状态在进程内存里,每个 worker 各算各的,重启清零(见 core/throttle.py)。
    """
    email_key = f"email:{payload.email}"
    ip_key = f"ip:{client_ip(request) or '-'}"

    for key in (email_key, ip_key):
        retry_after = LOGIN_THROTTLE.retry_after(key)
        if retry_after is not None:
            raise RateLimited(
                "登录尝试过于频繁,请稍后再试。", retryAfterSeconds=round(retry_after, 1)
            )

    try:
        result = await _issue(db, request, email=payload.email, password=payload.password)
    except InvalidCredentials:
        # 只记"凭证错误"这一类。数据库故障之类的失败不是猜密码的信号,
        # 把它们也计入会让一次故障把所有人锁在门外。
        LOGIN_THROTTLE.record_failure(email_key)
        LOGIN_THROTTLE.record_failure(ip_key)
        raise

    LOGIN_THROTTLE.clear(email_key)
    return result


@router.post("/refresh", response_model=AuthResult, summary="轮换令牌")
async def refresh(
    payload: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthResult:
    """用旧令牌换一张新的,旧的那张立即作废。

    这里**不**走 `get_auth_context`:要轮换的令牌可能刚好过期或已被轮换,而那正是需要
    轮换的场景。如果先过鉴权依赖,请求会在进入这个函数之前就被 401 掉。所以令牌走请求体。

    旧令牌被重放时会整族撤销(见 auth_service.resolve_session),这个接口因此也是
    "令牌泄露"的探测点。
    """
    issued = await auth_service.refresh(
        db,
        token=payload.token,
        user_agent=request.headers.get("user-agent"),
        ip=client_ip(request),
    )
    return _auth_result(issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="登出当前设备")
async def logout(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """撤销这张令牌对应的会话。**永远返回 204。**

    刻意不依赖 `get_auth_context`:那样第二次登出(令牌已经作废)会先被 401 拦下,
    而"把已经登出的会话再登出一次"本来就该成功。前端因此不需要把"登出时收到 401"
    当成成功来特判 —— 少一条每个调用方都得记住的规则。

    也因此这个接口**不需要登录**,它是唯一一个这样的写接口:没有令牌就无事可做,
    有令牌就已经等于持有那份访问权了,不存在"替别人登出"这个问题。
    响应恒为 204 也意味着它无法被用来探测某张令牌是否有效。
    """
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            await auth_service.logout_by_token(db, token.strip())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions", response_model=list[SessionInfo], summary="当前有效的登录")
async def list_sessions(
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> list[SessionInfo]:
    sessions = await auth_service.list_sessions(db, ctx.user.user_id)
    return [
        SessionInfo(
            id=session.id,
            issued_at=session.issued_at,
            expires_at=session.expires_at,
            absolute_expires_at=session.absolute_expires_at,
            user_agent=session.user_agent,
            current=session.id == ctx.session.id,
        )
        for session in sessions
    ]


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="登出指定设备",
)
async def revoke_session(
    session_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """撤销自己名下的某一条会话(比如"把图书馆那台电脑登出")。

    id 不是合法 UUID -> 422(契约问题);id 合法但不是自己的 -> 404(与"不存在"同码,
    理由见 `WorkspaceNotFound`)。整个 API 的路径参数都是这个规矩。
    """
    await auth_service.revoke_session(db, user.user_id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

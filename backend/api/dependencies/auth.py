"""认证依赖。

这是"接口默认开放"这件事的终结处。在此之前,`backend/core/security.py` 里那个
`get_current_user_id` 在**没有 Authorization 头时直接返回 `"dev_user"`** —— 也就是说
所有接口对所有人开放,而调用方看不出任何异常。

现在的规则很简单:**任何需要身份的接口,签名里必须有 `Depends(get_current_user)`**。
没有就是开放的,而这一点在 `test_authz_matrix.py` 里被逐条检查(路由清单从
`app.openapi()` 派生,新增路由若不在测试的分类表里,测试直接失败)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.security import CurrentUser
from backend.db.models import AuthSession, User
from backend.db.session import get_db
from backend.services import auth_service
from backend.services.errors import Unauthenticated

ALWAYS_ALLOWED_PATHS = frozenset({"/health", "/ready", "/docs", "/redoc", "/openapi.json"})


@dataclass(frozen=True, slots=True)
class AuthContext:
    """一次已认证的请求:谁、用的哪条会话、以及那条会话的 ORM 行。

    三个都在这里是有原因的:`user` 是给服务层用的纯值对象(不含任何可变的 ORM 状态),
    `session` 是登出要撤销的那一行,`account` 是 `GET /api/users/me` 要如实返回的
    完整档案。`resolve_session` 本来就把这三样都查出来了,再让路由各自重查一次
    既浪费又容易出现"这一处查了、那一处忘了"。
    """

    user: CurrentUser
    session: AuthSession
    account: User


def client_ip(request: Request) -> str | None:
    """客户端 IP。

    **只读 `request.client.host`。**刻意不看 `X-Forwarded-For`:那个头由客户端随便写,
    在没配可信代理的情况下信它,等于让任何人伪造自己的来源。将来真部署到反代后面,
    正确的做法是在 uvicorn 上开 `--proxy-headers --forwarded-allow-ips=<反代地址>`,
    让 uvicorn 自己把 `request.client` 改成真实地址 —— 而不是在业务代码里手工解析。

    而且这个值落库前会先做 sha256(见 `core/security.hash_ip`),所以即使它不准,
    代价也只是"会话列表里的来源标注不可靠",不会成为一条个人信息泄漏。
    """
    return request.client.host if request.client else None


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise Unauthenticated("需要登录。")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        # 明确说清正确格式,而不是含糊地报"认证失败" —— 前者是能自己改的,
        # 后者只会让人反复检查一个没写错的地方。
        raise Unauthenticated("Authorization 头必须形如 `Bearer <token>`。")
    return token.strip()


async def get_auth_context(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """解析令牌 -> (用户, 会话)。

    FastAPI 默认按请求缓存依赖结果,所以一个路由同时依赖 `get_auth_context` 与
    `get_current_user` 时,这次查询只会发生一次。
    """
    session, user = await auth_service.resolve_session(db, _extract_bearer(authorization))
    return AuthContext(
        user=CurrentUser(
            user_id=user.id,
            session_id=session.id,
            timezone=user.timezone,
            token_version=user.token_version,
        ),
        session=session,
        account=user,
    )


async def get_current_user(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> CurrentUser:
    return ctx.user

"""账户与会话的生命周期。

## 分层位置

`services/` 是**唯一允许写数据库**的一层(见 backend/agent/README.md 的分层约定)。
加密原语在 `core/security.py`(纯函数、不碰库),FastAPI 依赖在
`api/dependencies/auth.py`,本模块只负责"把一次注册/登录/轮换做成一次事务"。

## 令牌是"可撤销"这件事的实际执行者

会话在库里,所以登出、改密码、发现泄露都能立刻生效。下面 `resolve_session` 里那条
"轮换过的令牌被再次使用 -> 整族撤销"是这套设计真正的价值所在:JWT 做不到这件事,
而重放检测正是"令牌被偷了"最常见的信号。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.core.security import (
    account_problem,
    generate_token,
    hash_ip,
    hash_password,
    hash_token,
    normalize_email,
    password_problem,
    verify_password,
)
from backend.db.base import utcnow
from backend.db.models import AuthSession, User
from backend.services.errors import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidInput,
    NotFound,
    PasswordTooWeak,
    SessionExpired,
    TokenReuseDetected,
    Unauthenticated,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: 滑动过期。每次显式轮换顺延这么久。
SESSION_TTL = timedelta(days=30)
#: 绝对上限。无论怎么轮换都不越过 —— 否则"绝对过期"就不是绝对的。
ABSOLUTE_SESSION_TTL = timedelta(days=90)

_USER_AGENT_MAX = 400
#: 轮换链的遍历上限。链一旦因为某个 bug 成环,这里必须能停下来 ——
#: 登录接口挂死比撤销失败严重得多。
_MAX_FAMILY_WALK = 64

# 账户不存在时用来喂给 verify_password 的假哈希。
#
# 没有它的话,`user is None` 会立刻返回,而"用户存在但密码错"要先跑一次 scrypt
# (约 16 MiB 内存 + 数十毫秒)。两者响应时间的差异足够大到可以远程测量,
# 于是登录接口就变成了一个账号枚举器。跑一次真的哈希把这条信道堵死。
_DUMMY_PASSWORD_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password(generate_token())
    return _DUMMY_PASSWORD_HASH


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """一次登录的产物。**明文 token 只在这里存在,库里只有它的 sha256。**"""

    token: str
    session: AuthSession
    user: User


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


async def _find_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def _issue_session(
    db: AsyncSession,
    user: User,
    *,
    user_agent: str | None,
    ip: str | None,
    rotated_from: AuthSession | None = None,
    absolute_expires_at=None,
) -> IssuedSession:
    """签发一条新会话。

    `expires_at` 取 `now + SESSION_TTL` 与绝对上限的**较小值**,并且轮换时绝对上限是
    从旧会话**继承**下来的、不是重新计算的 —— 重新计算等于没有绝对上限。
    """
    now = utcnow()
    absolute = absolute_expires_at or (now + ABSOLUTE_SESSION_TTL)
    token = generate_token()

    session = AuthSession(
        user_id=user.id,
        token_hash=hash_token(token),
        # 记下签发时的 token_version,改密码后靠这个整数让全部会话同时失效。
        token_version=user.token_version,
        issued_at=now,
        expires_at=min(now + SESSION_TTL, absolute),
        absolute_expires_at=absolute,
        user_agent=_clip(user_agent, _USER_AGENT_MAX),
        ip_hash=hash_ip(ip),
        # 往前的这一半链接。
        rotated_from_id=rotated_from.id if rotated_from is not None else None,
    )
    db.add(session)
    await db.flush()

    if rotated_from is not None:
        rotated_from.revoked_at = now
        # 往回的那一半。
        #
        # **两个方向都必须写。** 只写 `rotated_from.rotated_to_id` 时,从新令牌出发
        # 找不到它的来源,`_revoke_family` 就只能单向走 —— 而"整族撤销"这个名字
        # 意味着两个方向都要能走到。第一次实现就是漏了这一行,被
        # `test_refresh_records_the_rotation_chain` 抓到。
        rotated_from.rotated_to_id = session.id
        await db.flush()

    return IssuedSession(token=token, session=session, user=user)


# ---------------------------------------------------------------------------------
# 注册 / 登录
# ---------------------------------------------------------------------------------
async def register(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    timezone: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> IssuedSession:
    problem = password_problem(password)
    if problem:
        raise PasswordTooWeak(problem)

    # 与 ORM 的 @validates 和数据库的 CHECK 用的是**同一个函数**。这里再归一化一次
    # 不只是为了查重:它保证了 flush 时不可能撞上 ck_users_email_is_canonical,
    # 于是下面捕获到的 IntegrityError 只可能来自唯一约束,可以放心报 409。
    email = normalize_email(email)
    if account_problem(email):
        raise InvalidInput("手机号或邮箱格式不正确。")

    if await _find_user_by_email(db, email) is not None:
        raise EmailAlreadyRegistered("这个账号已经注册过了。")

    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name.strip(),
        timezone=timezone,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        # 两个请求同时注册同一个邮箱时,先到的那条提交后这条才会撞上唯一约束。
        # 不捕获的话用户看到的是 500 —— 而这其实是"已经注册过了"这个正常结果。
        await db.rollback()
        raise EmailAlreadyRegistered("这个账号已经注册过了。") from exc

    # 刻意**不**在这里创建 UserCapacityProfile。那会把一个猜出来的每周时间预算
    # 当成用户已经确认过的事实存下来。预算必须由用户说出来(阶段 3 的对话里问)。
    issued = await _issue_session(db, user, user_agent=user_agent, ip=ip)
    await db.commit()
    return issued


async def login(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> IssuedSession:
    email = normalize_email(email)
    user = await _find_user_by_email(db, email)

    if user is None:
        verify_password(password, _dummy_hash())
        raise InvalidCredentials("账号或密码不正确。")
    if not verify_password(password, user.password_hash):
        raise InvalidCredentials("账号或密码不正确。")
    if not user.is_active:
        # 停用的账号与密码错误返回同一个错误。分开报会让"这个账号存在吗"这个问题
        # 又多一个可以问的地方。
        raise InvalidCredentials("账号或密码不正确。")

    issued = await _issue_session(db, user, user_agent=user_agent, ip=ip)
    await db.commit()
    return issued


# ---------------------------------------------------------------------------------
# 校验 / 轮换 / 登出
# ---------------------------------------------------------------------------------
async def _find_session_by_token(db: AsyncSession, token: str) -> AuthSession | None:
    result = await db.execute(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    )
    return result.scalar_one_or_none()


async def _revoke_family(db: AsyncSession, session: AuthSession) -> int:
    """把一条轮换链上的全部会话一起撤销。

    从当前会话出发**两个方向都走**:沿 `rotated_from_id` 回溯到链根,沿
    `rotated_to_id` 往前往后。`visited` 集合是兜底 —— 链一旦因为某个 bug 成环,
    这里必须停下来而不是转圈。
    """
    now = utcnow()
    visited: set[uuid.UUID] = set()
    queue: list[uuid.UUID] = [session.id]
    revoked = 0

    while queue and len(visited) < _MAX_FAMILY_WALK:
        current_id = queue.pop()
        if current_id in visited:
            continue
        visited.add(current_id)

        current = await db.get(AuthSession, current_id)
        if current is None:
            continue
        if current.revoked_at is None:
            current.revoked_at = now
            revoked += 1
        for neighbour in (current.rotated_from_id, current.rotated_to_id):
            if neighbour is not None and neighbour not in visited:
                queue.append(neighbour)

    return revoked


async def resolve_session(db: AsyncSession, token: str) -> tuple[AuthSession, User]:
    """令牌 -> (会话, 用户)。凭证无效一律抛 DomainError,不返回 None。

    返回元组而不是只返回用户:调用方几乎总要知道"是哪一条会话",而多查一次的意义
    只是多一次查询。
    """
    session = await _find_session_by_token(db, token)
    if session is None:
        raise Unauthenticated("登录状态无效,请重新登录。")

    if session.revoked_at is not None:
        if session.rotated_to_id is not None:
            # 已经被轮换掉的令牌又被拿来用。正常客户端只持有最新那张,所以这只可能
            # 意味着有人拿到了旧令牌 —— 按泄露处理:整族撤销,让新旧一起失效。
            await _revoke_family(db, session)
            # 这里**必须立即提交**。撤销是安全响应,不能因为后续请求失败被回滚 ——
            # 那等于"检测到泄露但没处理"。
            await db.commit()
            raise TokenReuseDetected("检测到登录凭证被重复使用,已退出全部设备,请重新登录。")
        raise Unauthenticated("登录状态已失效,请重新登录。")

    now = utcnow()
    if session.expires_at <= now or session.absolute_expires_at <= now:
        raise SessionExpired("登录已过期,请重新登录。")

    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise Unauthenticated("登录状态无效,请重新登录。")
    if session.token_version != user.token_version:
        # 密码改过 -> token_version 自增 -> 所有旧会话同时失效。
        raise SessionExpired("登录状态已失效,请重新登录。")

    return session, user


async def refresh(
    db: AsyncSession,
    *,
    token: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> IssuedSession:
    session, user = await resolve_session(db, token)
    issued = await _issue_session(
        db,
        user,
        user_agent=user_agent or session.user_agent,
        ip=ip,
        rotated_from=session,
        # 继承而不是重算 —— 否则每次轮换都把绝对上限往后推 90 天,"绝对"就没了。
        absolute_expires_at=session.absolute_expires_at,
    )
    await db.commit()
    return issued


async def logout_by_token(db: AsyncSession, token: str) -> bool:
    """撤销这张令牌对应的会话。返回是否真的撤销了什么。

    **按令牌而不是按"当前会话"来登出,是为了让登出成为幂等操作。**
    用户连点两次登出、或者网络重试,第二次拿的是一张已经作废的令牌 —— 如果登出接口
    先过鉴权,第二次会得到 401,前端不得不把"登出时收到 401"特判成成功。与其让每个
    调用方都记住这条特例,不如让登出本身永远成功:反正要登出的对象已经登出了。

    令牌不存在也不报错:那个会话本来就不可用,目标已经达成。
    """
    session = await _find_session_by_token(db, token)
    if session is None or session.revoked_at is not None:
        return False
    session.revoked_at = utcnow()
    await db.commit()
    return True


async def list_sessions(db: AsyncSession, user_id: uuid.UUID) -> list[AuthSession]:
    """列出仍然有效的会话。已撤销与已过期的不显示 —— 列表要能回答"我现在登录着几台设备"。"""
    now = utcnow()
    result = await db.execute(
        select(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
            AuthSession.absolute_expires_at > now,
        )
        .order_by(AuthSession.issued_at.desc())
    )
    return list(result.scalars())


async def revoke_session(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    """撤销指定的一条会话(只能在**自己**的会话里挑)。"""
    session = await db.get(AuthSession, session_id)
    if session is None or session.user_id != user_id:
        # 不存在与不属于自己同码同文案,理由与 WorkspaceNotFound 一样:否则拿 id
        # 逐个试就能问出"系统里有没有这个会话"。
        raise NotFound("没有找到这条登录记录。")
    if session.revoked_at is None:
        session.revoked_at = utcnow()
        await db.commit()


async def update_profile(db: AsyncSession, user: User, changes: dict) -> User:
    """更新档案。只动传进来的键 —— 调用方用 `model_fields_set` 决定这个字典的内容。"""
    for field, value in changes.items():
        setattr(user, field, value)
    await db.commit()
    return user

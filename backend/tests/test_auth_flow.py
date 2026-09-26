"""注册、登录、轮换、登出。

这里每一条都对应一个具体的"会出事"的方式,不是把接口再点一遍:

- 邮箱大小写/空白不同 -> 必须是同一个账号(否则一个人能注册出两个账号)
- 密码错误与账号不存在 -> 必须是同一个响应(否则接口是账号枚举器)
- 登出后旧令牌 -> 必须失效(否则登出是个装饰)
- 轮换后旧令牌被重放 -> 整族撤销(这是"令牌被偷了"最常见的信号)
- 任何响应里 -> 都不能出现 password_hash 或令牌明文
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.throttle import FailureThrottle
from backend.db.base import utcnow
from backend.db.models import AuthSession, User
from backend.tests.conftest import DEFAULT_PASSWORD, auth_headers


def _error(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


async def test_phone_account_register_login_and_duplicate(app_client: httpx.AsyncClient) -> None:
    payload = {"email": "13900001234", "password": DEFAULT_PASSWORD, "displayName": "知途用户"}
    registered = await app_client.post("/api/auth/register", json=payload)
    assert registered.status_code == 201
    assert registered.json()["user"]["email"] == payload["email"]
    duplicate = await app_client.post("/api/auth/register", json=payload)
    assert duplicate.status_code == 409
    login = await app_client.post("/api/auth/login", json={
        "email": payload["email"], "password": payload["password"],
    })
    assert login.status_code == 200
    assert login.json()["user"]["id"] == registered.json()["user"]["id"]
    wrong = await app_client.post("/api/auth/login", json={
        "email": payload["email"], "password": "wrong-password",
    })
    assert wrong.status_code == 401


@pytest.mark.parametrize("phone", ["12345", "12900001234", "139000012345", "１３９００００１２３４"])
async def test_invalid_phone_registration(app_client: httpx.AsyncClient, phone: str) -> None:
    result = await app_client.post("/api/auth/register", json={
        "email": phone, "password": DEFAULT_PASSWORD, "displayName": "知途用户",
    })
    assert result.status_code == 422


# ---------------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------------
async def test_register_returns_a_usable_token(app_client: httpx.AsyncClient) -> None:
    response = await app_client.post(
        "/api/auth/register",
        json={"email": "new@example.com", "password": DEFAULT_PASSWORD, "displayName": "小途"},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["token"]
    assert body["user"]["email"] == "new@example.com"
    assert body["user"]["timezone"] == "Asia/Shanghai"

    me = await app_client.get("/api/users/me", headers=auth_headers(body["token"]))
    assert me.status_code == 200
    assert me.json()["id"] == body["user"]["id"]


@pytest.mark.parametrize(
    "raw",
    ["Alice@Example.COM", "  alice@example.com  ", "ALICE@EXAMPLE.COM"],
    ids=["mixed-case", "padded", "upper"],
)
async def test_email_is_normalized_so_one_person_is_one_account(
    app_client: httpx.AsyncClient, raw: str
) -> None:
    """同一个邮箱的不同写法必须是同一个账号。

    如果归一化只做了"转小写"而没做"去空白",`'a@x.com '` 与 `'a@x.com'` 就是两个不同的
    字符串,唯一约束会放行 —— 于是用户第一次能注册成功,第二次用带空格的写法又注册了
    一个"新账号",而他以为登的是同一个。数据库那侧有 CHECK 兜底,这一条测的是接口这侧。
    """
    first = await app_client.post(
        "/api/auth/register",
        json={"email": "alice@example.com", "password": DEFAULT_PASSWORD, "displayName": "A"},
    )
    assert first.status_code == 201, first.text

    second = await app_client.post(
        "/api/auth/register",
        json={"email": raw, "password": DEFAULT_PASSWORD, "displayName": "A"},
    )
    assert second.status_code == 409, second.text
    assert _error(second) == "EMAIL_ALREADY_REGISTERED"

    # 而且用任何一种写法都能登进同一个账号。
    login = await app_client.post(
        "/api/auth/login", json={"email": raw, "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["id"] == first.json()["user"]["id"]


async def test_database_stores_the_canonical_email(
    app_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await app_client.post(
        "/api/auth/register",
        json={
            "email": "  Bob@Example.COM ",
            "password": DEFAULT_PASSWORD,
            "displayName": "B",
        },
    )
    stored = (await db.execute(select(User))).scalar_one()
    assert stored.email == "bob@example.com"


@pytest.mark.parametrize("password", ["", "short", "1234567"])
async def test_weak_password_is_rejected(app_client: httpx.AsyncClient, password: str) -> None:
    response = await app_client.post(
        "/api/auth/register",
        json={"email": "weak@example.com", "password": password, "displayName": "W"},
    )
    assert response.status_code == 400, response.text
    assert _error(response) == "PASSWORD_TOO_WEAK"


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "@example.com", "a@@b.com"])
async def test_bad_email_format_is_rejected(app_client: httpx.AsyncClient, email: str) -> None:
    response = await app_client.post(
        "/api/auth/register",
        json={"email": email, "password": DEFAULT_PASSWORD, "displayName": "X"},
    )
    assert response.status_code == 422, response.text
    assert _error(response) == "REQUEST_INVALID"


async def test_unknown_timezone_is_rejected(app_client: httpx.AsyncClient) -> None:
    """一个拼错的时区不会报错,只会让"今天是哪一天"安静地算错。"""
    response = await app_client.post(
        "/api/auth/register",
        json={
            "email": "tz@example.com",
            "password": DEFAULT_PASSWORD,
            "displayName": "T",
            "timezone": "Mars/Olympus",
        },
    )
    assert response.status_code == 422, response.text


async def test_password_is_hashed_not_stored(
    app_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """库里存的必须是 scrypt 哈希,不是口令本身,也不是它的无盐摘要。"""
    await app_client.post(
        "/api/auth/register",
        json={"email": "hash@example.com", "password": DEFAULT_PASSWORD, "displayName": "H"},
    )
    user = (await db.execute(select(User))).scalar_one()

    assert DEFAULT_PASSWORD not in user.password_hash
    assert user.password_hash.startswith("scrypt$")
    # 每次注册都要用新的盐 —— 相同口令的两个账号不能有相同的哈希。
    await app_client.post(
        "/api/auth/register",
        json={"email": "hash2@example.com", "password": DEFAULT_PASSWORD, "displayName": "H2"},
    )
    other = (await db.execute(select(User).where(User.email == "hash2@example.com"))).scalar_one()
    assert other.password_hash != user.password_hash


async def test_token_is_stored_only_as_a_hash(
    app_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """库里存的是令牌的 sha256。库被读走 != 账号被登进去。"""
    response = await app_client.post(
        "/api/auth/register",
        json={"email": "tok@example.com", "password": DEFAULT_PASSWORD, "displayName": "T"},
    )
    token = response.json()["token"]
    session = (await db.execute(select(AuthSession))).scalar_one()

    assert session.token_hash != token
    assert len(session.token_hash) == 64
    assert token not in session.token_hash


@pytest.mark.parametrize("response_field", ["password_hash", "passwordHash", "tokenHash"])
async def test_no_credential_material_leaks_into_responses(
    app_client: httpx.AsyncClient, response_field: str
) -> None:
    """响应体里不允许出现任何凭据字段名 —— 连字段名都不该有。"""
    response = await app_client.post(
        "/api/auth/register",
        json={"email": "leak@example.com", "password": DEFAULT_PASSWORD, "displayName": "L"},
    )
    assert response_field not in response.text

    me = await app_client.get("/api/users/me", headers=auth_headers(response.json()["token"]))
    assert response_field not in me.text


# ---------------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------------
async def test_wrong_password_and_unknown_account_are_indistinguishable(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """账号不存在与密码错误返回**完全相同**的状态码与 code。

    只要两者可区分,这个接口就是一个账号枚举器:拿一批邮箱逐个试,凭返回差异就能筛出
    哪些邮箱注册过。
    """
    account = await make_account(email="known@example.com")

    wrong = await app_client.post(
        "/api/auth/login", json={"email": account.email, "password": "wrong-password"}
    )
    unknown = await app_client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"}
    )

    assert wrong.status_code == unknown.status_code == 401
    assert _error(wrong) == _error(unknown) == "INVALID_CREDENTIALS"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_login_works_with_the_display_name_absent(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account(email="login@example.com")
    response = await app_client.post(
        "/api/auth/login", json={"email": account.email, "password": account.password}
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == account.email


async def test_repeated_failures_are_throttled(
    app_client: httpx.AsyncClient, make_account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败次数超限 -> 429,并带上 Retry-After。

    登录接口没有限流就是一个密码猜测器。这里把阈值调小来验证机制本身,
    与生产阈值无关 —— 生产是 10 次/15 分钟(见 core/throttle.py)。
    """
    from backend.api.routes import auth as auth_routes

    monkeypatch.setattr(
        auth_routes, "LOGIN_THROTTLE", FailureThrottle(limit=3, window_seconds=60)
    )
    account = await make_account(email="throttle@example.com")

    for _ in range(3):
        failed = await app_client.post(
            "/api/auth/login", json={"email": account.email, "password": "nope"}
        )
        assert failed.status_code == 401

    blocked = await app_client.post(
        "/api/auth/login", json={"email": account.email, "password": account.password}
    )
    assert blocked.status_code == 429, blocked.text
    assert _error(blocked) == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) >= 1

    # 被限流时**连正确的密码也不放行** —— 否则限流对攻击者毫无意义。
    assert "token" not in blocked.text


async def test_successful_login_clears_the_failure_counter(
    app_client: httpx.AsyncClient, make_account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """打错几次之后登对,计数清零 —— 正常用户不该被自己的手误锁在门外。"""
    from backend.api.routes import auth as auth_routes

    throttle = FailureThrottle(limit=3, window_seconds=60)
    monkeypatch.setattr(auth_routes, "LOGIN_THROTTLE", throttle)
    account = await make_account(email="clear@example.com")

    for _ in range(2):
        await app_client.post(
            "/api/auth/login", json={"email": account.email, "password": "nope"}
        )

    ok = await app_client.post(
        "/api/auth/login", json={"email": account.email, "password": account.password}
    )
    assert ok.status_code == 200
    assert throttle.retry_after(f"email:{account.email}") is None


# ---------------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------------
async def test_logout_revokes_the_token(app_client: httpx.AsyncClient, make_account) -> None:
    account = await make_account()

    logged_out = await app_client.post("/api/auth/logout", headers=account.headers)
    assert logged_out.status_code == 204

    after = await app_client.get("/api/users/me", headers=account.headers)
    assert after.status_code == 401
    assert _error(after) == "UNAUTHENTICATED"


async def test_logout_is_idempotent(app_client: httpx.AsyncClient, make_account) -> None:
    """连点两次登出都是 204,而不是第二次报 401。

    用户双击、或者客户端超时重试,都会发出第二次登出;而那时令牌已经作废。
    如果第二次返回 401,每个调用方都得记住"登出收到 401 要当成成功"这条特例 ——
    而这类特例正是"非 200 就当成后端没在运行"那类 bug 的温床。
    """
    account = await make_account()

    assert (await app_client.post("/api/auth/logout", headers=account.headers)).status_code == 204
    assert (await app_client.post("/api/auth/logout", headers=account.headers)).status_code == 204

    after = await app_client.get("/api/users/me", headers=account.headers)
    assert after.status_code == 401


async def test_logout_without_a_token_is_still_204(app_client: httpx.AsyncClient) -> None:
    """没有任何令牌也返回 204 —— 而且与"有令牌"的响应完全一样,所以它不能用来
    探测某张令牌是否有效。"""
    response = await app_client.post("/api/auth/logout")
    assert response.status_code == 204
    assert response.text == ""


async def test_sessions_list_shows_the_current_one(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()

    second_login = await app_client.post(
        "/api/auth/login", json={"email": account.email, "password": account.password}
    )
    assert second_login.status_code == 200
    assert second_login.json()["token"] != account.token

    listing = await app_client.get("/api/auth/sessions", headers=account.headers)
    assert listing.status_code == 200, listing.text
    sessions = listing.json()

    assert len(sessions) == 2
    # 恰好一条被标成当前 —— "我现在登录着几台设备"要能一眼看出来。
    current = [item for item in sessions if item["current"]]
    assert len(current) == 1
    # 而且标中的必须是**这次请求用的**那条,不是随便一条。
    assert current[0]["id"] == account.session_id

    other = next(item for item in sessions if not item["current"])
    assert other["id"] != account.session_id
    # 会话列表里不能出现任何形式的令牌。
    assert account.token not in listing.text
    assert second_login.json()["token"] not in listing.text


async def test_revoking_another_session_logs_that_device_out(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()
    other = (
        await app_client.post(
            "/api/auth/login", json={"email": account.email, "password": account.password}
        )
    ).json()

    revoked = await app_client.delete(
        f"/api/auth/sessions/{account.session_id}", headers=auth_headers(other["token"])
    )
    assert revoked.status_code == 204

    stale = await app_client.get("/api/users/me", headers=account.headers)
    assert stale.status_code == 401
    # 发起撤销的那条会话不受影响。
    assert (
        await app_client.get("/api/users/me", headers=auth_headers(other["token"]))
    ).status_code == 200


async def test_expired_session_is_rejected(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """过期会话 -> 401 SESSION_EXPIRED,与"无效令牌"是不同的 code。

    客户端据此可以区分"该静默重新登录"和"这个令牌根本不是我们的"。
    """
    account = await make_account()
    session = (
        await db.execute(select(AuthSession).where(AuthSession.id == uuid.UUID(account.session_id)))
    ).scalar_one()

    past = utcnow() - timedelta(minutes=1)
    session.expires_at = past
    session.absolute_expires_at = past
    await db.commit()

    response = await app_client.get("/api/users/me", headers=account.headers)
    assert response.status_code == 401
    assert _error(response) == "SESSION_EXPIRED"


async def test_password_change_invalidates_every_session(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """`User.token_version` 自增 -> 该用户所有会话同时失效。

    这条同时钉住了 `auth_sessions.token_version` 这一列存在的理由:没有它,
    "改密码立即失效"就只是一句注释(现在还没有改密码的接口,所以这里直接改库;
    等阶段 7 有了接口,这条测试会继续保护同一个不变量)。
    """
    account = await make_account()
    before = await app_client.get("/api/users/me", headers=account.headers)
    assert before.status_code == 200

    user = await db.get(User, uuid.UUID(account.id))
    user.token_version += 1
    await db.commit()

    after = await app_client.get("/api/users/me", headers=account.headers)
    assert after.status_code == 401
    assert _error(after) == "SESSION_EXPIRED"


# ---------------------------------------------------------------------------------
# 轮换
# ---------------------------------------------------------------------------------
async def test_refresh_rotates_and_the_old_token_dies(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()

    refreshed = await app_client.post("/api/auth/refresh", json={"token": account.token})
    assert refreshed.status_code == 200, refreshed.text
    new_token = refreshed.json()["token"]
    assert new_token != account.token

    # 新令牌可用。
    assert (
        await app_client.get("/api/users/me", headers=auth_headers(new_token))
    ).status_code == 200


async def test_replaying_a_rotated_token_revokes_the_whole_family(
    app_client: httpx.AsyncClient, make_account
) -> None:
    """**这条是整套令牌设计的核心。**

    正常客户端只持有最新那张令牌,所以"旧令牌又被用了"几乎只意味着一件事:有人在
    别处拿到了它。此时正确的反应不是"拒绝这一次",而是"这一族全部作废" ——
    攻击者与用户一起被踢出去,用户重新登录,攻击者手里那张也跟着失效。

    如果只拒绝旧令牌,攻击者手上那张新令牌可以一直用到 90 天后。
    """
    account = await make_account()
    rotated = (
        await app_client.post("/api/auth/refresh", json={"token": account.token})
    ).json()
    new_token = rotated["token"]

    replay = await app_client.post("/api/auth/refresh", json={"token": account.token})
    assert replay.status_code == 401, replay.text
    assert _error(replay) == "TOKEN_REUSE_DETECTED"

    # 整族撤销:刚换出来的那张也失效。
    assert (
        await app_client.get("/api/users/me", headers=auth_headers(new_token))
    ).status_code == 401


async def test_refresh_keeps_the_absolute_deadline(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """轮换**继承**绝对上限,不是重新算 90 天。

    重算的话就永远到不了绝对上限 —— 每次轮换都在往后推,等于只有滑动过期,
    "90 天绝对过期"这条保证就不存在了。
    """
    account = await make_account()
    session = (
        await db.execute(select(AuthSession).where(AuthSession.id == uuid.UUID(account.session_id)))
    ).scalar_one()
    original_absolute = session.absolute_expires_at

    refreshed = await app_client.post("/api/auth/refresh", json={"token": account.token})
    assert refreshed.status_code == 200

    new_session = (
        await db.execute(
            select(AuthSession).where(AuthSession.rotated_from_id == session.id)
        )
    ).scalar_one()

    assert new_session.absolute_expires_at == original_absolute
    # 滑动过期确实被顺延了(否则"滑动"这个词没意义)。
    assert new_session.expires_at > session.expires_at
    # 但无论如何不会越过绝对上限。
    assert new_session.expires_at <= new_session.absolute_expires_at


async def test_refresh_records_the_rotation_chain(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """轮换链必须双向可追 —— 整族撤销靠它走路。"""
    account = await make_account()
    await app_client.post("/api/auth/refresh", json={"token": account.token})

    old = await db.get(AuthSession, uuid.UUID(account.session_id))
    assert old.revoked_at is not None
    assert old.rotated_to_id is not None

    new = await db.get(AuthSession, old.rotated_to_id)
    assert new is not None
    assert new.rotated_from_id == old.id
    assert new.revoked_at is None


async def test_refresh_with_an_unknown_token_is_401(app_client: httpx.AsyncClient) -> None:
    response = await app_client.post("/api/auth/refresh", json={"token": "made-up-token"})
    assert response.status_code == 401
    assert _error(response) == "UNAUTHENTICATED"


# ---------------------------------------------------------------------------------
# 档案
# ---------------------------------------------------------------------------------
async def test_patch_me_updates_only_the_given_fields(
    app_client: httpx.AsyncClient, make_account
) -> None:
    account = await make_account()

    response = await app_client.patch(
        "/api/users/me",
        json={"school": "示例大学", "targetYear": 2028},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["school"] == "示例大学"
    assert body["targetYear"] == 2028
    assert body["email"] == account.email


async def test_patch_me_can_clear_a_field(app_client: httpx.AsyncClient, make_account) -> None:
    """传 null 是"清空",不传是"不动"。没有这个区分,PATCH 就表达不了"删掉这句简介"。"""
    account = await make_account()
    await app_client.patch(
        "/api/users/me", json={"bio": "写点什么"}, headers=account.headers
    )

    cleared = await app_client.patch(
        "/api/users/me", json={"bio": None}, headers=account.headers
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["bio"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"tokenVersion": 99},
        {"isActive": False},
        {"email": "attacker@example.com"},
        {"id": "00000000-0000-0000-0000-000000000000"},
    ],
    ids=["token-version", "is-active", "email", "id"],
)
async def test_patch_me_rejects_privileged_fields(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession, payload: dict
) -> None:
    """白名单之外的字段一律 422。

    `token_version` 能改就等于"随时伪造一次全员登出/让某人永远登不上";
    `is_active` 能改就是自我启用;`email` 能改就等于绕过唯一性与找回流程。
    这些字段不在白名单里,所以它们连**被忽略**的机会都没有 —— 直接报错。
    """
    account = await make_account()

    response = await app_client.patch("/api/users/me", json=payload, headers=account.headers)
    assert response.status_code == 422, response.text
    assert _error(response) == "REQUEST_INVALID"


async def test_owner_id_and_object_identity_are_not_leaked_in_the_list(
    app_client: httpx.AsyncClient, make_account, db: AsyncSession
) -> None:
    """空间列表只暴露契约里声明过的字段,不夹带 owner_id 之类的内部字段。

    `extra="forbid"` 只保证"传进来多余的字段会报错",不保证"多返回了字段" ——
    那由这条测试盯着。
    """
    account = await make_account()
    response = await app_client.get("/api/workspaces", headers=account.headers)

    assert set(response.json()[0]) == {"id", "title", "intent", "createdAt"}
    assert account.id not in response.text

"""账户与会话的 HTTP 契约。

## 令牌怎么传给服务端

`Authorization: Bearer <token>`,而且**只有这一种方式** —— 没有 cookie,也没有 query 参数。

这条选择同时解决了两件事:

- **CSRF 在结构上不可能。** CSRF 的前提是浏览器会自动附带凭据(cookie)。Bearer 头不会
  被跨站表单或 `<img src>` 带上,所以没有"替用户发请求"这条路可走,也就不需要 CSRF token
  那一整套机制。
- **没有"cookie 只对 /api/auth/* 生效"这种需要小心维护的边界。** 计划里原本写的是
  cookie 方案加路径限定;不用 cookie 之后,那条边界规则本身消失了。

代价要说清楚:**前端必须自己保存令牌**(localStorage 或内存),因此一旦发生 XSS,
令牌可被读取。httpOnly cookie 能挡住这一条,但会立刻把 CSRF 问题引回来。这个取舍在这里
是有意为之,不是没想到 —— 前端在阶段 3/5 重写时会一并处理 XSS 风险(不再把口令摘要存进
localStorage 就是其中一项)。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from backend.contracts.common import ApiModel
from backend.core.security import account_problem, normalize_email


def _validate_timezone(value: str) -> str:
    """时区必须是真实的 IANA 名字。

    一个拼错的时区不会报错,只会让"今天是哪一天"和每日时间池安静地算错一整天 ——
    这类错误在界面上表现为"我的计划怎么排到了昨天",极难追查。所以在入口就拦住。
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"未知时区: {value}") from exc
    return value


class RegisterRequest(ApiModel):
    email: str
    password: str
    display_name: str = Field(min_length=1, max_length=80)
    timezone: str = "Asia/Shanghai"

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        email = normalize_email(value)
        problem = account_problem(email)
        if problem:
            raise ValueError(problem)
        return email

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("显示名不能为空。")
        return name

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return _validate_timezone(value)


class LoginRequest(ApiModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        # 登录时**不做格式校验** —— 格式不对的邮箱直接走"查不到用户",
        # 返回与密码错误完全相同的 INVALID_CREDENTIALS。若这里报"邮箱格式不合法",
        # 就又多了一条能区分"这个账号存不存在"的信道。只归一化,不评判。
        return normalize_email(value)


class RefreshRequest(ApiModel):
    """轮换令牌。

    刻意要求把当前令牌放进请求体,而不是"读 Authorization 头再续期"。
    滑动续期必须是一个显式动作:如果每个请求都顺手续期,一个被偷走的令牌只要被用过
    一次就自动延长了 30 天,而用户与日志里都看不出任何异常。
    """

    token: str = Field(min_length=1)


class UserProfile(ApiModel):
    """当前用户档案。**没有 password_hash,也没有任何令牌字段。**

    字段与前端 `AccountProfile` 对齐(少一个 `passwordDigest` —— 那是前端旧方案在浏览器里
    自己算的摘要,服务端从来不需要,也不该有)。
    """

    id: uuid.UUID
    email: str
    display_name: str
    timezone: str
    school: str | None = None
    major: str | None = None
    year: str | None = None
    rank: int | None = None
    target_year: int | None = None
    target_goal: str | None = None
    bio: str | None = None
    created_at: datetime


class UpdateProfileRequest(ApiModel):
    """PATCH /api/users/me。所有字段可选,只更新传了的那些。

    用 `model_fields_set` 区分"没传"与"传了 null" —— 前者不动,后者清空。
    没有这个区分,PATCH 就没法表达"把 bio 清掉"。
    """

    display_name: str | None = Field(default=None, max_length=80)
    timezone: str | None = None
    school: str | None = Field(default=None, max_length=120)
    major: str | None = Field(default=None, max_length=120)
    year: str | None = Field(default=None, max_length=32)
    rank: int | None = Field(default=None, ge=0, le=100000)
    target_year: int | None = Field(default=None, ge=1900, le=2200)
    target_goal: str | None = Field(default=None, max_length=500)
    bio: str | None = Field(default=None, max_length=1000)

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("显示名不能为空。")
        return name

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone(value)


class IssuedToken(ApiModel):
    """一张新签发的会话令牌。

    `absolute_expires_at` 与 `expires_at` 一起给出,是为了让前端能如实告诉用户
    "这次登录最晚能用到什么时候" —— 只给滑动过期,用户会以为可以无限续下去。
    """

    token: str
    expires_at: datetime
    absolute_expires_at: datetime


class AuthResult(IssuedToken):
    user: UserProfile


class SessionInfo(ApiModel):
    """一条登录记录。**不带 token_hash,也不带明文令牌。**

    明文只在签发那一刻存在于内存里,之后无论谁查库都拿不到 —— 这正是"库被读走也
    登不进任何账号"的原因。
    """

    id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    user_agent: str | None = None
    #: 本次请求用的就是这一条。
    current: bool = False

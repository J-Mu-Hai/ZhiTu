"""鉴权原语:密码哈希、不透明令牌、当前用户。

**这一层是纯的** —— 不碰数据库,不 import FastAPI。会话的生命周期(创建、校验、
轮换、撤销)在 backend/services/auth_service.py,FastAPI 依赖在
backend/api/dependencies/auth.py。这样 core/ 可以在任何地方被安全 import。

## 为什么是不透明 bearer 令牌,而不是 JWT

每个请求本来就要查库 —— 计划、节点、排期统统要按 workspace 判归属,归属校验躲不掉
那次索引查询。既然要查库,把会话也放在库里就是免费的;而换来的好处很实在:
**可以立刻撤销**。改密码、登出、发现泄露,一条 UPDATE 就生效,JWT 做不到(签发出去的
令牌在过期前一直有效)。

代价是每个请求一次索引查询 —— 那正是归属校验本来就要付的成本,没有额外开销。
等将来真出现"跨服务验签且不能查同一个库"的需求,再引 PyJWT,线格式不变
(仍然是 `Authorization: Bearer <opaque>`)。

## 为什么用标准库 hashlib.scrypt

零依赖。argon2 / bcrypt 都要编译扩展,而这个项目已经有一个必须克隆 conda 环境的
现实约束(openjiuwen),再叠加编译型依赖只会让"换个机器跑不起来"更容易发生。
scrypt 是抗内存硬计算的 KDF,标准库实现基于 OpenSSL,足够。

密码哈希串自带参数(`scrypt$n$r$p$salt$hash`),所以将来调参不会让旧哈希失效。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass

# scrypt 参数。内存占用 = 128 * n * r = 16 MiB,登录一次的代价。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16
# 显式给出上限。OpenSSL 的默认 maxmem 是 32 MiB,不同构建可能不同 —— 依赖默认值
# 会让"在某台机器上 scrypt 突然报错"变成一个很难查的环境问题。
_SCRYPT_MAXMEM = 64 * 1024 * 1024

TOKEN_BYTES = 32

#: 密码长度上限。不设上限时,一个超长字符串会让每次登录都白烧 CPU。
MAX_PASSWORD_LENGTH = 128
MIN_PASSWORD_LENGTH = 8


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    """生成自描述哈希串:`scrypt$n$r$p$salt$hash`。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        ["scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), _b64(salt), _b64(derived)]
    )


def verify_password(password: str, stored: str | None) -> bool:
    """校验密码。**任何形式的损坏都返回 False,绝不抛异常。**

    数据库里一条格式异常的哈希不该让登录接口 500 —— 那既泄露了"这条记录有问题",
    也把一个数据问题变成了可用性问题。校验失败是 False,就是这么简单。
    """
    if not stored:
        return False
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, hash_raw = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(hash_raw)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_raw),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


def generate_token() -> str:
    """不透明令牌。只在这一刻存在于内存里,库里存的是它的 sha256。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """令牌指纹。sha256 存 64 位十六进制,正好对上 auth_sessions.token_hash VARCHAR(64)。

    这里**不需要**加盐或慢哈希:令牌是 32 字节的高熵随机值,不存在"被字典爆破"的问题;
    而且每个请求都要算它,慢哈希会让每次请求都变慢。慢哈希是给低熵密码用的。
    """
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def account_problem(value: str) -> str | None:
    """兼容旧邮箱账号与中国大陆手机号；手机号尚未通过短信核验。

    预览版沿用历史 email 字段存储登录标识，不伪造邮箱、不迁移旧账号。
    """
    if re.fullmatch(r"1[3-9][0-9]{9}", value):
        return None
    return email_problem(value)


def normalize_email(raw: str) -> str:
    """邮箱的规范形式:去掉首尾空白 + 转小写。

    **这是全仓库唯一的定义处**,三处必须一致,所以只能有一处:
      1. 数据库的 `ck_users_email_is_canonical`(`email = lower(trim(email))`);
      2. ORM 的 `@validates`(models/user.py 直接调用本函数);
      3. 注册/登录接口在查询前做的归一化。

    定义两遍就等于允许它们漂移,而漂移的后果是"用户用大写注册、用小写登不进去"
    或者更糟:"大小写不同的两个邮箱各注册了一个账号"。
    """
    return raw.strip().lower()


def email_problem(email: str) -> str | None:
    """返回不合规的原因,合规则返回 None。

    刻意用一组朴素规则而不是引 `email-validator`:这个项目的注册量级不需要 RFC 5322
    级别的完备性,而多一个编译/传递依赖就多一处"换台机器装不上"。真正的把关在别处 ——
    邮箱能不能收到信,只有发一封信才知道;格式校验挡的是打错字,不是攻击。
    """
    if not email:
        return "邮箱不能为空。"
    if len(email) > 320:
        return "邮箱过长。"
    if any(char.isspace() for char in email):
        return "邮箱不能包含空白字符。"
    if email.count("@") != 1:
        return "邮箱必须恰好包含一个 @。"
    local, _, domain = email.partition("@")
    if not local:
        return "邮箱缺少 @ 之前的部分。"
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return "邮箱域名不合法。"
    return None


def hash_ip(raw: str | None) -> str | None:
    """IP 只以指纹形式落库。

    会话列表要能显示"这条登录来自别处",但没有任何理由把用户的完整 IP 长期存下来 ——
    那是一条一旦泄露就无法撤回的个人信息。指纹足够做"与上次不同吗"这个判断。
    """
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def password_problem(password: str) -> str | None:
    """返回不合规的原因,合规则返回 None。"""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符。"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"密码不能超过 {MAX_PASSWORD_LENGTH} 个字符。"
    return None


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """已认证的请求主体。由 api/dependencies/auth.py 构造。"""

    user_id: uuid.UUID
    session_id: uuid.UUID
    timezone: str
    token_version: int

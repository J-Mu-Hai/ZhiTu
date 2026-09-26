"""API 契约的公共底座。

## 为什么是 camelCase

前端既有代码用的是 camelCase —— `WorkspaceSummary.createdAt`、`AccountProfile.passwordDigest`、
`GrowthNode.estimateMinutes`。契约层统一把线格式定成 camelCase,前端就不必为了接后端而
改字段名。Python 侧仍然写 snake_case(`populate_by_name=True` 让两种写法都能被接受),
所以这个约定只影响 JSON,不影响代码可读性。

## 为什么请求一律 extra="forbid"

多出来的字段直接 400,而不是静默忽略。理由与"非法模型输出不产生部分写入"是同一条:
一个打错字的字段名(比如 `titel`)如果被静默丢掉,用户看到的是"我明明填了,它就是没保存"。
宁可当场报错。

## 绝不能出现在任何响应契约里的东西

`password_hash`、`token_hash`、会话令牌明文。所有响应都走**显式声明的模型**,并且不声明
这些字段 —— 即使服务层不小心把一个 ORM 对象整个传回来,`from_attributes` 也只会读取
声明过的字段,`password_hash` 没有任何路径能出去。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """所有请求/响应契约的基类。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        # 让 Python 侧可以用 snake_case 构造(测试与内部代码更自然),
        # 同时线格式保持 camelCase。
        populate_by_name=True,
        # 允许从 ORM 对象直接校验,省掉一层手写 to_dict()。
        from_attributes=True,
        extra="forbid",
    )


class ApiErrorBody(ApiModel):
    """错误体里 `error` 那一层。

    `code` 是接口的一部分,和字段名同级 —— 客户端按它分支。`message` 只是给人看的
    文案,会改;按文案分支的客户端会在某次"把话说得更清楚"的改动里静默失灵。
    """

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApiErrorResponse(ApiModel):
    """**所有**非 2xx 响应的形状。

    它在这里不是"顺便描述一下":前端要处理的错误响应和成功响应一样多,而一个没有
    契约的错误体只能靠读后端源码来对接。真正生成这些响应的是
    `backend/services/errors.py::DomainError.to_body()`,契约与它的绑定由
    `backend/tests/test_contract_drift.py` 钉住 —— 改了一边而没改另一边,测试会红。
    """

    error: ApiErrorBody

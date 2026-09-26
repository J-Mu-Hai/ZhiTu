"""异常 -> HTTP 的**唯一**翻译处。

## 为什么错误体是 `{"error": {"code", "message", "details"}}`

客户端按 `code` 分支,不按 `message` 文案分支。文案会改(改得更清楚、更友好),
code 不会 —— 它是接口的一部分,和字段名同级。

## 为什么数据库故障要单独处理而不是让它 500

`DB_UNAVAILABLE`(503)与 500 的区别不是措辞,是**用户该做什么**:503 的意思是
"这次没保存上,请重试",500 的意思是"我们写错了"。把两者混在一起,用户就无法判断
要不要重录一次今天的学习记录 —— 而这个判断错了,那条记录就永久消失。

## 为什么 `IntegrityError` **不**翻译成 503

它也是 `SQLAlchemyError`,但它的含义是"这次写入违反了某条约束",也就是代码问题。
翻译成"数据库暂时不可用"会把一个必须修的 bug 伪装成一次网络抖动。它照常 500。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from backend.services.errors import (
    DbUnavailable,
    DomainError,
    RequestInvalid,
    SessionExpired,
    TokenReuseDetected,
    Unauthenticated,
)

logger = logging.getLogger(__name__)

#: 401 时附带 `WWW-Authenticate` —— 这是 HTTP 的规定,客户端据此知道该用哪种凭证。
_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

_UNAUTHORIZED_ERRORS = (Unauthenticated, SessionExpired, TokenReuseDetected)


def _field_path(location: tuple[object, ...]) -> str:
    """把 pydantic 的 loc 变成人能读的字段路径。

    去掉第一段(`body` / `query` / `path` 这种位置标记),剩下的拼成
    `counts.nodes` 这样的路径。只剩位置标记时退回它本身,免得返回空字符串。
    """
    parts = [str(part) for part in location[1:]] or [str(part) for part in location]
    return ".".join(parts)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _handle_domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        headers = dict(exc.to_headers())
        if isinstance(exc, _UNAUTHORIZED_ERRORS):
            headers.update(_BEARER_CHALLENGE)
        return JSONResponse(
            status_code=exc.http_status, content=exc.to_body(), headers=headers or None
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """契约校验失败 -> 422,形状与其他错误一致。

        不套这一层的话,前端要同时处理两种错误体(FastAPI 默认的 `{"detail": [...]}` 与
        我们自己的 `{"error": {...}}`),而漏处理的那种通常表现为界面上什么都不显示。
        """
        fields = [
            {"field": _field_path(tuple(error.get("loc", ()))), "reason": error.get("msg", "")}
            for error in exc.errors()
        ]
        error = RequestInvalid("请求内容不符合要求。", fields=fields)
        return JSONResponse(status_code=error.http_status, content=error.to_body())

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def _handle_db_unavailable(request: Request, exc: Exception) -> JSONResponse:
        # 必须留日志。这个 handler 把异常变成了一个正常的 503 响应,如果不记,
        # "数据库连不上"在日志里就彻底消失了,只剩下用户说"刚才保存失败了"。
        logger.exception("database unavailable while handling %s %s", request.method, request.url.path)
        # 写请求的响应体里必须**明说这一条没保存**(`saved: false`)。用户据此决定要不要
        # 再录一次学习记录;谎报成功会让那条记录永久消失,而用户以为它在。
        #
        # 判断依据是 HTTP 方法而不是"这个路由会不会写":后者要维护一张与路由表同步的
        # 清单,而两张表一定会漂移。GET / HEAD 没有"保存"这回事,所以不带这个字段。
        error = DbUnavailable(
            "数据库暂时不可用,本次操作没有保存,请稍后重试。",
            saved=None if request.method in ("GET", "HEAD") else False,
        )
        return JSONResponse(status_code=error.http_status, content=error.to_body())

"""FastAPI 应用入口。

约定:本层只做 HTTP —— 路由、鉴权、校验、序列化。业务逻辑放 backend/services/,
模型调用一律走 backend/agent/。这两条不是风格偏好,是 docs/04 里的硬约束。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.agent.runtime.openjiuwen_runtime import warm_up
from backend.api.dependencies.agent import get_reasoner
from backend.api.errors import register_exception_handlers
from backend.api.routes import (
    auth,
    execution,
    plan,
    questions,
    reminders,
    review,
    schedule,
    users,
    workspaces,
)
from backend.core.config import settings
from backend.db.health import (
    assert_db_reachable,
    assert_schema_current,
    check_db_reachable,
    describe_target,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动期:先确认数据库,再预热 openJiuwen。

    数据库连不上就拒绝启动。这个产品有"确认计划"这类写操作,一个连不上库却照常提供
    服务的进程会让用户以为计划已经保存 —— 那比启动失败严重得多。只有显式设置
    ALLOW_DEGRADED_DB=1(仅供本地排查)才允许带病启动,且会在日志里高调告警。

    **openJiuwen 起不来则照常启动。** 两者的区别是"产品还能不能做事":没有库就没有
    任何真话可说,而没有模型还有直连那条路和规则兜底 —— 用户至少能问出缺的条件。
    所以这里只记日志,并把结果如实说清楚,不拦启动。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    await assert_db_reachable()
    # 连得上不等于能用。库里的结构落后于这份代码时(部署时忘了跑迁移),服务会照常
    # 启动,然后在第一个碰到新列的请求上炸成一个和真实原因毫不相干的 500。
    await assert_schema_current()

    # 预热放在启动阶段,而不是第一次请求:导入 openjiuwen 会注册一堆连接器与文档
    # 解析器(实测要几秒),让它落在用户的第一次对话上,那一次会莫名其妙地慢。
    #
    # 没有 key 就不预热 —— 那条路上 `build_reasoner` 只会给出 `RuleFallbackReasoner`,
    # 模型一次都不会被调用,预热只是白等几秒(测试进程里每个用例都是这样)。判据用
    # "这一轮到底会不会走模型",而不是"现在是不是在测试"。
    if settings.llm_api_key and settings.agent_reasoner.strip().lower() in ("auto", "openjiuwen"):
        if await warm_up():
            logger.info("openJiuwen 运行时已就绪,规划请求走 openJiuwen。")
        else:
            logger.warning(
                "openJiuwen 运行时不可用,规划请求将走直连模型;"
                "响应里的 source 会如实标注,界面上显示「直连模型」。"
            )

    # 脚本模式(**测试脚手架**,见 agent/runtime/scripted.py)的配置**在这里**验。
    #
    # `get_reasoner` 是懒构造的 —— 它要到第一条需要推理的请求才建。配置错只留在
    # 构造里的话,它会在那条消息上炸成 500,而**跨域的浏览器把"500 且响应里没有
    # CORS 头"显示成「连不上后端服务,请确认后端已经启动」**:验收的人去查进程,
    # 而进程活得好好的。所以配坏脚本这件事必须在进程起来的那一刻说清楚 ——
    # 也是 scripted.py 里"配了模式没配脚本 = 启动即失败"那句承诺的兑现处。
    #
    # 顺带把 `build_reasoner` 里那行"正在念一份写死的脚本"记进启动日志:进程一起来
    # 就能看出这一轮没有模型参与。
    if settings.agent_reasoner.strip().lower() == "script":
        get_reasoner()
    yield


app = FastAPI(
    title="知途 Growth Agent API",
    version="0.1.0",
    docs_url="/docs" if settings.app_env != "production" else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # False,而不是 True。这个 API **不用 cookie**,凭据一律走 Authorization 头,
    # 所以浏览器从来不需要"带上凭据"这个能力。把它关掉,CORS 这一层也就和
    # "CSRF 结构上不可能"对齐了 —— 少一个将来被人顺手改成 True 的地方。
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(workspaces.router, prefix="/api/workspaces", tags=["workspaces"])
# 同一个前缀,不同的关注点:上面那个管"空间是什么",这个管"空间里的计划与提案"。
app.include_router(plan.router, prefix="/api/workspaces", tags=["plan"])
# 还是同一个前缀:这个管"哪天做"。**注意它的写入范围不止路径里那个空间** ——
# 时间池按人算,一份排期天然横跨用户的全部活动空间(见 routes/schedule.py)。
app.include_router(schedule.router, prefix="/api/workspaces", tags=["schedule"])
# 复盘:偏差事实与"按执行情况调整"。事实那一半永远可用,不依赖模型。
app.include_router(review.router, prefix="/api/workspaces", tags=["review"])
# 问题节点:AI 提问、用户回答。**与提案分开** —— 问题落库即成卡片,不等待确认;
# 回答之后模型提的计划变更仍然走 plan.py 那套待确认提案。
app.include_router(questions.router, prefix="/api/workspaces", tags=["questions"])

# 执行反馈与「今天」。前缀是 `/api` 而不是 `/api/sessions`:「今天」跨全部活动空间,
# 路径上根本没有空间 id —— 用户问的是"我今天要做什么",不是"我这个空间今天做什么"。
app.include_router(execution.router, prefix="/api", tags=["execution"])
# 站内提醒。**不是推送** —— 它只在用户打开界面时出现,见 routes/reminders.py。
app.include_router(reminders.router, prefix="/api/reminders", tags=["reminders"])


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """存活探针。不碰数据库 —— 进程活着就该返回 200。"""
    return {"status": "ok", "env": settings.app_env}


@app.get("/ready", tags=["meta"])
async def ready() -> dict[str, object]:
    """就绪探针。数据库不可用时返回 503。

    与 /health 分开是有意的:运维需要能区分"进程死了"和"进程活着但存不了数据"。
    """
    from fastapi.responses import JSONResponse

    ok, reason = await check_db_reachable()
    body: dict[str, object] = {
        "ready": ok,
        "env": settings.app_env,
        "database": describe_target(),
        "reasoner": settings.agent_reasoner,
    }
    if not ok:
        body["reason"] = reason
        return JSONResponse(status_code=503, content=body)
    return body

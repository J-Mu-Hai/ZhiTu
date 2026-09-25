"""FastAPI 应用入口。

约定:本层只做 HTTP —— 路由、鉴权、校验、序列化。
业务逻辑放 services/,模型调用一律走 backend/agent/。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import agent, execution, journal, plans, tasks, users
from backend.core.config import settings

app = FastAPI(
    title="知途 Growth Agent API",
    version="0.1.0",
    docs_url="/docs" if settings.app_env != "production" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(plans.router, prefix="/api/plans", tags=["plans"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(execution.router, prefix="/api/execution", tags=["execution"])
app.include_router(journal.router, prefix="/api/journal", tags=["journal"])
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}

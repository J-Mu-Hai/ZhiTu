"""运行时配置。所有值来自环境变量(本地通过 .env 注入)。

约束:密钥与连接串只走 .env,任何形式的 key/token/连接串都不进仓库。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/core/config.py -> backend -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DEV_DB = REPO_ROOT / "data" / "zhitu_dev.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_secret_key: str = "change-me"
    api_base_url: str = "http://localhost:8000"

    # 留空表示回落到 resolved_database_url 的本地 SQLite 默认值。
    database_url: str = ""
    # 开发期打印每条 SQL 会淹没日志(18 张表),默认关闭。
    db_echo: bool = False

    # 连不上数据库时是否仍允许启动。**只能用于本地排查,生产绝不能打开** ——
    # 打开它等于允许"写不进去但看起来保存成功了"。
    allow_degraded_db: bool = False

    llm_api_key: str = ""
    # DeepSeek 官方 API 只有 deepseek-chat / deepseek-reasoner。
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com"
    llm_timeout_seconds: float = 30.0
    #: 一次规划回复的 token 上限。
    #:
    #: 8192 而不是 4096。一次"从头排一份三个月计划"的真实回复实测要 1500-2500 个
    #: completion token,而它是有可能长得多的(每个节点都带说明和验收标准)。
    #: 上限卡在 4096 时,模型会把 JSON 写到一半被截断 —— 截断的 JSON 解析不了,
    #: 于是一整轮变成"模型输出无法解析"。用户看到的是一句和真实原因毫不相干的
    #: 抱怨,而原因只是预算给少了。
    agent_max_tokens: int = 8192
    #: 规划智能体 V0.1:新建目标空间是否走程序控制的三阶段工作流。
    #:
    #: **默认关闭** —— 既有 workspace 与既有测试的行为完全不变(会话 `workflow_stage`
    #: 保持 NULL)。本地体验与 V0.1 Demo 通过 `.env` 的 `V01_PLANNING=on` 打开。
    v01_planning: bool = False
    #: 规划智能体重构 V1(P1:阶段一画布与节点讨论)的开关。
    #:
    #: **默认关闭** —— 既有 workspace、V0.1 空间与既有测试完全不变。开启后,**新建**
    #: 目标空间进入 "先想清楚" 的推理画布(三组固定分析容器),而不是旧的问卷/路线。
    #: 老空间不迁移:它们的 `v1_stage` 为 NULL,行为与加列之前完全一样。
    planning_v1: bool = False
    #: 公开研究工具的提供方。**默认 `none` = 不联网**。首期只支持 `tavily`。
    #:
    #: 这是一个**显式开关**:没有配置时如实返回“未配置”,不允许假装查过。
    research_provider: str = "none"
    #: Tavily 的 API Key。**绝不返回给 API / UI / 日志。**
    tavily_api_key: str = ""
    #: 总开关。默认 false —— 即使填了 provider 也不发请求。
    research_enabled: bool = False
    #: 每轮对话最多几次真实公网检索。
    research_max_calls_per_turn: int = 1
    #: 单次查询最多返回几条来源。
    research_max_results: int = 3
    #: 单次调用超时(秒)。
    research_timeout_seconds: float = 6.0
    #: 关键词长度上限。
    research_max_query_chars: int = 180
    #: 全局每日真实请求上限(按 `research_timezone` 的自然日)。
    research_max_calls_per_day: int = 100
    #: 相同规范化查询的缓存有效期(秒)。
    research_cache_ttl_seconds: int = 86400
    #: 出网租约多久算过期(秒):过期后其他 worker 可接管,避免卡死。
    research_lease_seconds: int = 15
    #: 每日额度按哪个时区算自然日。
    research_timezone: str = "Asia/Shanghai"
    #: 可选域名白名单(逗号分隔,子串匹配)。空 = 不限制(仅公开网页)。
    research_allowed_domains: str = ""
    #: **测试专用**:受控的 mock 研究 provider。默认关闭;即便开了,生产环境
    #: (`APP_ENV=production`)也不会启用 —— 三道闸见 `research_service.provider_ready`。
    #: 生产只支持真实 `tavily`。
    research_mock_enabled: bool = False
    # 规划调用的实现路径:auto | openjiuwen | direct_llm | rule
    # auto 表示每次请求时探测 openjiuwen 是否可用,不可用则降级并在响应里如实标注。
    agent_reasoner: str = "auto"

    #: Agent 运行轨迹诊断抽屉的显式开关。
    #:
    #: **本地开发(`APP_ENV=development`)默认可用**;其余环境(尤其生产)默认关闭,
    #: 必须显式设成 true 才会暴露入口与读取 API。轨迹本身始终由服务端真实边界写入,
    #: 这个开关只控制"谁能读到/看到"。
    agent_trace_ui_enabled: bool = False

    cors_origins: str = (
        "http://127.0.0.1:5173,http://localhost:5173,http://localhost:3000"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """之前这个字段叫 cors_origins_raw,导致 .env 里的 CORS_ORIGINS 被静默丢弃。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def resolved_database_url(self) -> str:
        """DATABASE_URL 优先;开发环境留空时落到仓库内的本地 SQLite 文件。"""
        if self.database_url:
            return self.database_url
        if self.app_env == "development":
            return f"sqlite+aiosqlite:///{_DEFAULT_DEV_DB.as_posix()}"
        raise RuntimeError(
            "非开发环境必须显式配置 DATABASE_URL(例如 "
            "postgresql+asyncpg://user:password@host:5432/dbname)"
        )

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

"""数据访问层:声明基类、引擎/会话、模型与仓储。

分层约定:
- `backend/db/`          表和纯数据访问,不含业务规则。
- `backend/services/`    业务逻辑,是**唯一**的写入者。
- `backend/scheduler/`   纯函数排期算法,不碰数据库。
- `backend/agent/`       只负责调用模型并提出变更,不写数据库。
- `backend/api/`         只做 HTTP。
"""

from backend.db.base import Base, JsonDict, TimestampMixin, UtcDateTime, UuidPk, utcnow
from backend.db.session import SessionLocal, engine, get_db

__all__ = [
    "Base",
    "JsonDict",
    "SessionLocal",
    "TimestampMixin",
    "UtcDateTime",
    "UuidPk",
    "engine",
    "get_db",
    "utcnow",
]

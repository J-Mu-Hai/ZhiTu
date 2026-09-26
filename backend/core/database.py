"""兼容转发。

引擎与会话的真实定义在 backend/db/session.py。这个模块此前定义了 engine /
SessionLocal / get_db 但**全仓库无人 import**,同时又与实际使用的存储路径不一致,
是"看起来有数据库层其实没有"的来源之一。

保留文件只是为了不打断任何外部 import;新代码请直接 import backend.db.session。
"""

from __future__ import annotations

from backend.db.session import DATABASE_URL, SessionLocal, engine, get_db, is_sqlite

__all__ = ["DATABASE_URL", "SessionLocal", "engine", "get_db", "is_sqlite"]

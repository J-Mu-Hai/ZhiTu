"""时区相关的日期计算。

**"今天是哪一天"只能从这里取。** 这个模块存在的唯一目的,是让"用哪一天"这件事
只有一个答案 —— 只要有第二个地方自己写 `date.today()`,就会有两套日期,而它们在
东八区的晚上会差一天。

`date.today()` 用的是**服务器本地时区**。部署在腾讯云(UTC)上时,它在东八区的
每天 0:00 到 8:00 之间返回的是前一天。这个错误不会报错,只会让"这周还剩几天"算错,
而用户没有任何办法看出来。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

#: 认不出时区名时的落点。选东八区是因为产品面向的用户在这里 ——
#: 宁可"按产品主要用户所在时区算",也不要"按服务器的 UTC 算"再错一天。
_FALLBACK = "Asia/Shanghai"

_cache: dict[str, ZoneInfo] = {}


def resolve_zone(name: str | None) -> ZoneInfo:
    """时区名 -> ZoneInfo,认不出就退回默认并记一条日志。

    Windows 上没有系统时区库,`tzdata` 包是可选的依赖(见 requirements.txt)。
    包不在时 `ZoneInfo(...)` 抛 `ZoneInfoNotFoundError`,在请求处理里抛异常会让
    **整个接口 500** —— 用户只是想知道今天几号,不该因此打不开工作台。
    所以这里兜住,并留下日志:静默退回是有代价的,不能连痕迹都没有。
    """
    key = (name or "").strip() or _FALLBACK
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        zone = ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("无法识别时区 %r,按 %s 处理。", name, _FALLBACK)
        zone = ZoneInfo(_FALLBACK) if key != _FALLBACK else ZoneInfo("UTC")
    _cache[key] = zone
    return zone


def today_in(timezone_name: str | None) -> date:
    """用户所在时区的"今天"。"""
    return datetime.now(resolve_zone(timezone_name)).date()


def now_in(timezone_name: str | None) -> datetime:
    return datetime.now(resolve_zone(timezone_name))

"""Reasoner 的注入点。

## 为什么它必须是一个依赖,而不是模块级单例

两件事同时成立:

1. **测试要用假实现。** 全套后端测试必须在不配任何模型 key 的情况下跑通,而且不是
   靠"记得把 key 清空"——那是一条纪律,纪律会失效。依赖可以被
   `app.dependency_overrides[get_reasoner]` 直接替换,没有 key 也永远不会碰到网络。

2. **构造一次,不是每次请求构造一次。** `DirectLLMReasoner` 本身很轻,但阶段 4 的
   openJiuwen 适配器不是 —— `Runner.resource_mgr` 是进程级全局,按请求重建是明确的
   生命周期 bug。所以这里用 `lru_cache` 建一次挂在进程上;需要按请求变化的配置
   由 `Settings` 在启动时读定,不在请求里变。

原来的 `backend/agent/workflows/proposal.py` 是一个模块级单例,而且它的 `confirm()`
是**同步函数**却被 async 路由直接调用 —— 阻塞事件循环,并且第二次调用直接抛错,
用户双击得到的是报错而不是幂等成功。这两件事都是同一个毛病的两种表现:
把"有状态的东西"写成了模块级全局。
"""

from __future__ import annotations

from functools import lru_cache

from backend.agent.runtime import Reasoner, build_reasoner
from backend.core.config import settings


@lru_cache(maxsize=1)
def get_reasoner() -> Reasoner:
    """进程内唯一的 Reasoner。测试通过 dependency_overrides 替换它。"""
    return build_reasoner(settings)


def reset_reasoner_cache() -> None:
    """配置改变后重建(测试用)。"""
    get_reasoner.cache_clear()

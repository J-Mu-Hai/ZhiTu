"""契约产物:把 `backend/contracts/` 里的 Pydantic 模型渲染成一份 JSON Schema。

## 为什么是"生成",不是"手写"

线格式的**权威**在代码里 —— 它是 `backend/contracts/` 那些模型,因为真正会拒绝非法
形状的地方是 FastAPI 的请求校验和响应序列化。手写一份 JSON Schema 意味着线格式有两个
来源,而它们一定会漂移;那么"契约文件"就从保证变成了文档,而且是一份会撒谎的文档。

所以这里的做法是:**从代码生成,签入仓库,再用测试双向钉住。**

- 生成物与签入文件不一致 -> 测试失败(`test_contract_drift.py`);
- 签入文件里的每一个定义都必须对应一个真实的模型 -> 同一条测试的另一半。

它仍然是"唯一源":任何人(前端、Mobile、文档)都必须与它一致,任何不一致都让构建
失败。它只是不以它为**人工编辑**的位置 —— 要改契约,改 Pydantic 模型,然后跑

```
python -m backend.contracts.schema
```

## 只收公开模型

`__all__` 里声明的,或者(没有 `__all__` 的模块)不以 `_` 开头的、**定义在该模块里**的
`ApiModel` 子类。下划线开头的判别联合底座(`_ActionBase` 之类)不直接面向接口,但它们
被引用时仍会作为 `$defs` 出现在文件里 —— 那是 JSON Schema 的引用完整性要求,不是我们
把它们当成了公开契约。
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from backend.contracts.common import ApiModel

#: 产物的标识。跟着仓库走,不指向任何真实的解析地址 —— 它存在的意义是让文件里的
#: 每一个 `$ref` 有一个绝对的解析基准。
SCHEMA_ID = "https://zhitu.local/schemas/domain.schema.json"

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 签入仓库的产物路径。
ARTIFACT_PATH = REPO_ROOT / "shared" / "schemas" / "domain.schema.json"

_DESCRIPTION = (
    "知途 ZhiTu 的线格式契约。**这份文件是生成的,不要手改** —— "
    "权威在 backend/contracts/ 的 Pydantic 模型里;改契约请改模型,"
    "然后跑 `python -m backend.contracts.schema`。"
    "backend/tests/test_contract_drift.py 会双向比对这份文件与模型,任何漂移都让测试失败。"
)


def public_models() -> tuple[type[ApiModel], ...]:
    """所有公开的契约模型,按限定名排序。

    排序是必需的,不是讲究:产物的字节必须只由模型决定。依赖 `pkgutil` 的枚举顺序
    会让同一份代码在两台机器上生成出两份不同的文件,而那样的"漂移检测"第一次运行
    就会误报 —— 然后所有人学会无视它。
    """
    import backend.contracts as package

    found: dict[str, type[ApiModel]] = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        exported = getattr(module, "__all__", None)
        for name, value in vars(module).items():
            if not inspect.isclass(value) or not issubclass(value, ApiModel):
                continue
            if value is ApiModel or value.__module__ != module.__name__:
                continue
            if exported is None:
                if name.startswith("_"):
                    continue
            elif name not in exported:
                continue
            found[f"{module.__name__}.{name}"] = value
    return tuple(found[key] for key in sorted(found))


def render() -> str:
    """生成产物的完整文本(**含末尾换行**)。

    比对与写入用的是同一个函数 —— 一个只在内存里比、另一个自己拼字符串的写法,迟早
    会在"文件末尾有没有换行"这种事上分叉。
    """
    models = public_models()
    _, definitions = models_json_schema(
        [(model, "validation") for model in models],
        ref_template="#/$defs/{model}",
    )
    document: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "知途 ZhiTu 领域契约",
        "description": _DESCRIPTION,
        "$defs": definitions["$defs"],
    }
    # `sort_keys` + `ensure_ascii=False`:前者让输出与字典插入顺序无关,后者让中文
    # 说明在原文件里可读 —— 一份看不懂的契约文件没人会去核对它。
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write() -> Path:
    """把产物写进仓库。返回写入的路径。"""
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(render(), encoding="utf-8", newline="\n")
    return ARTIFACT_PATH


if __name__ == "__main__":
    print(f"wrote {write()}")

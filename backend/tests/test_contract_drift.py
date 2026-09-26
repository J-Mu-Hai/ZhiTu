"""契约产物与代码的双向漂移检测。

## 这个文件在防什么

`shared/schemas/domain.schema.json` 是从 `backend/contracts/` 生成的。生成物最大的
风险不是"生成错了",而是**它慢慢变成一份没人核对的文档**:有人改了模型没重新生成,
文件里留着一个早就不存在的字段,而前端照着它写类型 —— 一路绿,直到线上。

所以这里三件事:

1. **签入的文件必须与模型重新生成的结果逐字节一致。** 不一致就失败,并告诉你怎么修。
   这是双向的:多一个定义、少一个定义、改一个默认值,都会让它红。
2. **没有任何一个契约模块是"隐身"的。** 第一条的盲点是"生成器自己漏掉了一整个模块"
   —— 那种情况下重新生成后文件与代码照样一致,只是两边一起错了。所以这里另外钉住
   "每个契约模块都要贡献模型",以及几个关键模型必须存在。
3. **产物覆盖真实 HTTP 面。** 从 `app.openapi()` 现算响应模型名,断言它们都在产物里。
   一个读者(前端、Mobile)依赖这份文件的前提是:接口真的会返回的东西,文件里都有。

## 失败时怎么修

```
python -m backend.contracts.schema
```

然后看一眼 diff 是不是你想改的东西。**不要手改那个 JSON。**
"""

from __future__ import annotations

import json
import pkgutil

from backend.api.main import app
from backend.contracts.common import ApiModel
from backend.contracts.schema import ARTIFACT_PATH, public_models, render

#: 生成器等式的右半边常数。**不含**这些名字的产物等于没覆盖核心能力 ——
#: 它们是"一条线"的每一站:目标 -> 计划 -> 提案 -> 排期 -> 执行 -> 复盘。
CORE_MODELS = {
    "PlanPayload",
    "ProposalView",
    "SchedulePreviewResponse",
    "RecordExecutionResponse",
    "TodayResponse",
    "ReplanResponse",
    "RemindersResponse",
    "UserProfile",
}

#: 契约包里不该被扫描的模块:契约底座、生成器本身。
_NON_CONTRACT_MODULES = {"common", "schema"}


def _artifact() -> dict:
    return json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))


def test_the_artifact_exists() -> None:
    """产物必须在仓库里。

    没有这一条,后面几条会在"文件不存在"上报出一个关于 `FileNotFoundError` 的错误,
    而真正要说的话是"你漏签入了生成物"。
    """
    assert ARTIFACT_PATH.exists(), (
        f"{ARTIFACT_PATH} 不在仓库里 —— 跑 `python -m backend.contracts.schema` 生成它"
    )


def test_artifact_matches_the_models() -> None:
    """签入的产物 == 从模型重新生成的结果。**这是本文件的核心断言。**

    比对走 `render()` 而不是在这里自己拼一份 —— 写入与比对必须是同一段代码,否则
    "生成"和"检测"会各自漂移,而检测恰恰是用来发现漂移的。

    读取用默认的 universal newlines:Windows 上 git 可能把 LF 签出成 CRLF,那是行尾
    策略,不是契约漂移。比对的是内容,不是行尾字节。
    """
    checked_in = ARTIFACT_PATH.read_text(encoding="utf-8")
    generated = render()

    assert checked_in == generated, (
        "shared/schemas/domain.schema.json 与 backend/contracts 里的模型不一致。\n"
        "跑 `python -m backend.contracts.schema` 重新生成,然后确认 diff 是你想要的改动。\n"
        "**不要手改那个 JSON** —— 它的权威在模型里。"
    )


def test_every_contract_module_contributes_models() -> None:
    """每个契约模块都至少贡献一个公开模型。

    防的是第一条的盲点:一个模块如果整个被生成器漏掉(改名、`__all__` 写错、
    类都以下划线开头),重新生成后产物与代码仍然一致 —— 只是那份产物里少了一整块
    接口的形状,而它看起来完全正常。
    """
    import backend.contracts as package

    discovered = {model.__module__ for model in public_models()}
    expected = {
        f"{package.__name__}.{info.name}"
        for info in pkgutil.iter_modules(package.__path__)
        if info.name not in _NON_CONTRACT_MODULES
    }

    assert expected - discovered == set(), (
        f"这些契约模块没有任何公开模型被收进产物: {sorted(expected - discovered)}"
    )


def test_core_models_are_present() -> None:
    """核心模型一个都不能少。"""
    names = set(_artifact()["$defs"])
    assert names >= CORE_MODELS, f"产物里缺少: {sorted(CORE_MODELS - names)}"


def test_no_definition_is_orphaned() -> None:
    """产物里的每个定义,要么是公开模型,要么被别的定义引用。

    "被引用"这一条不是为了完整性(JSON Schema 允许有未被引用的定义),而是为了
    **发现手改**:往 `$defs` 里塞一个模型代码里没有的定义,它通常谁都不引用,于是
    在这里被逮住。手删一个模型、留下它的定义,也一样。
    """
    artifact = _artifact()
    definitions = artifact["$defs"]

    referenced: set[str] = set()
    for schema in definitions.values():
        _collect_refs(schema, referenced)

    public = {model.__name__ for model in public_models()}
    orphaned = set(definitions) - referenced - public
    assert orphaned == set(), (
        f"这些定义既不是公开模型、也没有被任何地方引用,像是手工塞进来的: {sorted(orphaned)}"
    )


def _collect_refs(node: object, into: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                into.add(value.removeprefix("#/$defs/"))
            else:
                _collect_refs(value, into)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, into)


#: FastAPI 为 422 自动生成的两个形状。**它们永远不会出现在线上的响应里** ——
#: `backend/api/errors.py` 覆写了 `RequestValidationError` 的处理器,校验失败返回的是
#: 我们自己的错误体。所以它们出现在 openapi 里只是框架的默认行为,不是我们的契约。
_FRAMEWORK_SCHEMAS = {"HTTPValidationError", "ValidationError"}


def test_artifact_covers_the_http_surface() -> None:
    """接口真的会返回的每一个模型,产物里都有。

    这一条把产物与**运行中的应用**连起来。前两条都只比对产物与模型的关系,而它们
    一起漂移是可能的(比如契约模型改了名,产物也重新生成了,但路由响应用的是另一个
    模块里的同名类)。这里问的是:"照着这份文件写前端的人,能不能描述全部响应?"

    `app.openapi()` 是唯一能拿到全部路由的地方 —— FastAPI 把 `include_router` 的
    路由包在 `_IncludedRouter` 里,`app.routes` 看不到它们。
    """
    components = set(app.openapi().get("components", {}).get("schemas", {}))
    names = set(_artifact()["$defs"])

    assert components - _FRAMEWORK_SCHEMAS <= names, (
        "这些响应模型出现在接口上,却不在契约产物里: "
        f"{sorted(components - _FRAMEWORK_SCHEMAS - names)}"
        " —— 跑 `python -m backend.contracts.schema` 重新生成"
    )


def test_the_framework_422_schemas_are_still_overridden() -> None:
    """`ValidationError` 不出现在契约里,前提是**它真的不会被返回**。

    上面那条测试把 FastAPI 的两个校验错误形状排除在外。这个排除只有在"我们的处理器
    仍然接管着校验失败"时才成立 —— 哪天有人把那个 handler 删掉,前端就会收到一个
    契约文件里没有描述过的 `{"detail": [...]}`。
    """
    from fastapi.exceptions import RequestValidationError

    assert RequestValidationError in app.exception_handlers, (
        "校验失败的处理器没了 —— 422 会退回 FastAPI 的默认形状,而契约里没有它"
    )


def test_the_error_envelope_matches_its_contract() -> None:
    """`DomainError.to_body()` 造出来的每一个错误体,都符合 `ApiErrorResponse`。

    **契约文件里描写错误体的那一块,不许是一句承诺。** 每个客户端都要处理非 2xx,
    所以错误体是接口里被依赖得最多的形状之一;而它偏偏是唯一一个不由 Pydantic 生成
    响应、而是由 `to_body()` 手工拼出来的形状 —— 也就是说,没有任何框架层面的东西
    会拦住它漂移。

    这里遍历**所有**错误类,而不是抽查几个:漏掉的那个恰好就是将来没人核对的那个。
    """
    import inspect

    from backend.contracts.common import ApiErrorResponse
    from backend.services import errors as errors_module
    from backend.services.errors import DomainError

    subclasses = [
        value
        for _, value in vars(errors_module).items()
        if inspect.isclass(value) and issubclass(value, DomainError) and value is not DomainError
    ]
    assert len(subclasses) >= 10, f"只发现 {len(subclasses)} 个错误类,枚举方式可能坏了"

    for error_class in subclasses:
        # 带一个 details 的实例 —— 不带 details 的路径会少走一段分支,
        # 而"哪些东西可以放进 details"正是最容易失控的地方。
        error = error_class("说明文字", fields=[{"field": "title", "reason": "太长了"}])
        parsed = ApiErrorResponse.model_validate(error.to_body())
        assert parsed.error.code == error_class.code
        assert parsed.error.message == "说明文字"
        assert parsed.error.details["fields"][0]["field"] == "title"


def test_discovery_rule_finds_the_models_it_claims_to() -> None:
    """`public_models()` 的规则本身被钉住:排除下划线开头的内部底座。

    这不是在测实现细节 —— "哪些模型算公开契约"是一个判断,而这个判断如果悄悄变了
    (比如某天把 `_ActionBase` 也收进来),产物会多出一批不该被外部依赖的形状,而
    第一条测试照样绿。
    """
    import backend.contracts as package

    models = public_models()
    assert models, "一个模型都没发现,生成器坏了"
    assert all(not model.__name__.startswith("_") for model in models)
    assert all(
        model.__module__.startswith(package.__name__) and model is not ApiModel for model in models
    )
    assert len({model.__name__ for model in models}) == len(models), (
        "有两个同名模型 —— 产物会互相覆盖"
    )

# shared/schemas

跨端数据契约。这里只有一个文件:[domain.schema.json](domain.schema.json)。

## 它是生成的,不要手改

```
python -m backend.contracts.schema
```

权威在 `backend/contracts/` 的 Pydantic 模型里。这份 JSON 由它们现算而来
([backend/contracts/schema.py](../../backend/contracts/schema.py)),签入仓库只是为了
让前端、移动端和评审者有一个不跑 Python 也能读的形状。

**改字段的正确顺序是:改模型 → 重新生成 → 看一眼 diff。** 反过来(先改 JSON)不会
有任何东西生效,而 [test_contract_drift.py](../../backend/tests/test_contract_drift.py)
会在下一次 `pytest` 时红给你看。

## 为什么不手写

真正的校验发生在后端 —— 拒绝非法模型输出全靠 Pydantic,它本来就需要自定义校验器和
别名生成。Pydantic 2 原生输出 JSON Schema,零依赖。反过来做要在 Python 侧引
`datamodel-code-generator`、TS 侧引 `json2ts`,两套生成器,而现有契约有跨文件
`$ref`,往返并不干净。

它仍然是**唯一源**:所有人必须与它一致,任何漂移让构建失败。只是不以它为**人工编辑**
的位置。

## 漂移检测是双向的

`test_contract_drift.py` 不只看"文件与模型是否一致"(那有盲点:生成器自己漏掉一整个
模块时,两边一起错,重新生成照样一致)。它另外钉住:

- 每个契约模块都必须贡献模型;
- 一条业务主线上每个关键模型都必须存在(目标 → 计划 → 提案 → 排期 → 执行 → 复盘);
- `app.openapi()` 里出现的每个响应模型都在产物里;
- `DomainError.to_body()` 造出的错误体符合 `ApiErrorResponse`;
- 产物里没有"谁都不引用"的定义(那是手改留下的痕迹)。

## 前端类型

`apps/web` 侧应当由这份产物派生 TS 类型(`json-schema-to-typescript`)。**这一条尚未
接线** —— 当前前端的 `apps/web/src/types/growth.ts` 是手写的,与产物之间没有自动
比对。在它接上之前,前端类型漂移不会被任何测试拦住。

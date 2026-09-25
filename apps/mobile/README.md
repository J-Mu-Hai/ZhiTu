# apps/mobile

Flutter 客户端。

`android/` 与 `ios/` 目前只有占位,需要在本机执行一次以生成本地工程文件:

```bash
cd apps/mobile
flutter create . --platforms=android,ios
```

`lib/` 的组织:

| 目录 | 放什么 |
| --- | --- |
| [screens/](lib/screens/) | 页面:`today` / `plan` / `conversation` / `journal` / `profile` |
| [widgets/](lib/widgets/) | 可复用组件 |
| [models/](lib/models/) | 数据模型,与 `shared/schemas/` 对应 |
| [services/](lib/services/) | API 客户端与本地存储 |
| [providers/](lib/providers/) | 状态管理 |

与 Web 端共用同一份契约,见 [shared/schemas/](../../shared/schemas/)。

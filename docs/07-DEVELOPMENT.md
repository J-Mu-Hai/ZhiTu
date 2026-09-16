# Development

## 环境准备

```bash
cp .env.example .env
docker compose up -d db redis
```

## 后端

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate     # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
uvicorn backend.api.main:app --reload
pytest
```

Agent 编排层用 openJiuwen,接入方式待定,见 [06-AGENT-DESIGN.md](06-AGENT-DESIGN.md)。

## Web

```bash
cd apps/web
npm install
npm run dev
```

技术栈已定:React + Next.js + React Flow。

## 移动端

```bash
cd apps/mobile
flutter pub get
flutter run
```

`android/` 与 `ios/` 目前只有占位,首次需要在本机生成:

```bash
cd apps/mobile && flutter create . --platforms=android,ios
```

## 原型

原型放在 [../prototypes/](../prototypes/) 下,**不进生产构建**。
在原型里怎么改都行;验证成功的东西才迁移到 `apps/`。

## 团队节奏

```
Mother Demo → Parallel Exploration → Integration → Next Version
```

**每 2~3 天 Integration。**

## 改动规则

- 改动数据契约时,先改 [shared/schemas/](../shared/schemas/),再同步 Web / Mobile / Backend,并更新 [05-DATA-MODEL.md](05-DATA-MODEL.md)
- 产品行为变化后,更新文档
- 大改动之前先说明影响范围,见 [../AGENTS.md](../AGENTS.md)

## 待补全

- [ ] 代码风格与 lint 规则
- [ ] 分支模型与提交信息规范
- [ ] 测试策略:单测 / 集成测试的边界
- [ ] 数据库迁移流程(见 [../scripts/database/](../scripts/database/))
- [ ] 部署流程(见 [../scripts/deploy/](../scripts/deploy/))

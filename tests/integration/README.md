# tests/integration

跨服务的集成测试。单个服务内部的测试放在各自的 `backend/tests/` 或 `apps/*/` 里。

放这里的场景:HTTP 契约、Agent 编排的端到端流程、数据库迁移、Agent 输出与 `shared/schemas/` 的一致性校验。

**待补全:** 测试如何启动依赖(db / redis)尚未确定,见 [docs/07-DEVELOPMENT.md](../../docs/07-DEVELOPMENT.md)。

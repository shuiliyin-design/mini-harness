# Applications

Phase 3 的最终用户应用放在这里，并作为独立 application layer 使用 Harness。应用可以
依赖稳定的 Harness façade、MCP tool boundary 和按需 Environment integration；Harness
Runtime、Bridge transport 与 Environment implementation 不得反向导入应用。

第一个应用是 [`digest_agent/`](digest_agent/README.md)：AI Digest Subscription Agent。当前已
实现 generation、Feedback/Profile/Explainable Ranking 与 Delivery 三条完全离线垂直切片；
真实 Brave、真实 LLM、HTTP 与 scheduler 仍留给后续切片。

应用级 User、Subscription、Profile、Digest、Delivery、Feedback 与 SQLite persistence
都留在 `apps/`。Harness 继续只拥有执行、Authority、Observation/Evidence、Artifact
acceptance 与 Authoritative Result。

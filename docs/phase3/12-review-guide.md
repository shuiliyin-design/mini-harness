# Phase 3 Review Guide

## 1. Ten-minute product review

按顺序读：

1. [`00-overview.md`](00-overview.md) 的全链路与 tree；
2. [`01-product-scope.md`](01-product-scope.md) 的 V1/out-of-scope；
3. [`03-subscription-schema.md`](03-subscription-schema.md) 的一等 constraints；
4. [`06-output-contracts.md`](06-output-contracts.md) 的 deterministic/semantic split；
5. [`09-failure-and-recovery.md`](09-failure-and-recovery.md) 的 matrix。

验收问题：用户能否从自然语言开始，手动得到有 sources 的 bounded Digest，反馈后明确改变下一次
排序；且没有 scheduler、auth、vector DB 或真实网络 test dependency？

## 2. Boundary review

```text
apps/digest_agent -> Harness façade / integrations
mini_harness_core -X-> apps
Bridge transport  -X-> app domain/environment authority
Environment       -X-> Subscription/Profile/Result decisions
Model             -X-> persistence/ranking/contract/delivery truth
```

检查未来源码：domain 是否 import sqlite/HTTP/Harness；Brave 是否只在 adapter；CRUD 是否误用 Agent
Run；application 是否自己伪造 Evidence/Artifact/Result；delivery 是否绕过 authorized dispatch。

## 3. Data review

- Subscription 是否保存 `max_chars/max_items` 并严格校验 int-not-bool？
- Digest 是否绑定 subscription version、period key、Harness run/result/artifact identity？
- SourceRef 是否只能引用 selected candidate 与 accepted current-run evidence？
- Profile 是否能由 Interaction + rule version 重算？
- SQLite transaction/unique keys 是否让 Result projection 与 Feedback 幂等？
- API key/raw response 是否可能进入 DB、Session、Evidence、Artifact、logs？
- Brave 空 topic tags 是否只经 bounded lexical rule 派生，而非因 HTTP 200 自动获得相关性？

## 4. Authority and failure review

- Search server metadata 是否被错误当成 local policy/effect？
- Model 是否能宣称字符数、排序、source validity 或 completed？
- no results 是否诚实 `incomplete`，而非空成功？
- Search 成功但 Digest incomplete 时，smoke 是否安全返回 non-zero 而不访问空 projection？
- generation completed + delivery failed 是否保留两个 truth？
- side-effecting notification unknown 是否禁止 blind resend？
- SQLite projection failure 是否只重试 projection，不重跑 Search/LLM？

## 5. Test review

第一条 slice 的三条 E2E 覆盖 valid、overlong、invalid source；当前 feedback slice 再覆盖 empty
profile、liked 上升、dismissed 下降三条 E2E。Real Brave slice 另有 16 条离线 adapter/workflow/smoke
测试；真实 Brave/Provider/Termux smoke 必须 opt-in。
Architecture test 应阻止 core→apps、domain→infrastructure，并证明当前 slice 无网络/API；Termux
mapping 必须依赖注入的 authorized Environment dispatcher，不能直接执行设备命令。

## 6. Core-change gate

若实现者认为必须修改 Harness core，先回答：

1. 现有 `MCPClient/MCPRegistry`、workspace Artifact 或 integration seam 为什么无法表达？
2. 需求是 Digest-specific，还是至少两个独立应用共有？
3. 新 schema 如何保持旧 Result/Bundle/Replay compatibility？
4. 是否可以先在 app adapter/workflow 中实现而不削弱 Authority？

当前实现答案是：**Fake 与 Real Brave fixed workflow 都不需要修改 core。只有未来自主多轮搜索
才重新评估 post-observation acceptor；historical schema、Authority model 与 Artifact/Result
semantics 均无需改变。**

上一页：[`11-design-decisions.md`](11-design-decisions.md) · 下一篇：
[`13-first-vertical-slice.md`](13-first-vertical-slice.md)

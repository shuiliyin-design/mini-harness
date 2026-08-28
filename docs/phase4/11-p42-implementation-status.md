# P4.2 Implementation Status — Updates / Feed Detail

## 1. 结论

P4.2 已把 loopback Web 从开发者式单页改为轻量移动端阅读入口。回访用户先看到可阅读更新，其次看到需要留意、
正在准备和暂时无更新的 Feed；点击卡片进入内容、来源、生成时间、历史范围和推荐解释。

```text
Updates
  -> ready content
  -> needs attention / failed
  -> preparing
  -> no update
  -> Feed Detail: items + sources + time + history + definition + why
```

页面不显示 Run、Digest、Outbox、Provider、Evidence、stage、Harness 或 CLI。没有修改 `mini_harness_core/`，没有新增
schema、第三方依赖、scheduler、Delivery automation 或前端框架。

## 2. Product read model

`DigestApplication` 新增 sealed `UpdatesHomeView` 与 `FeedDetailView`：

- Feed relationship 从 Subscription、ProductSubscription 与 UserSubscription 的组合验证为
  `active / paused / needs_attention`；缺 companion facts 时不猜测成功。
- latest update 从现有 first-briefing reservation、application work、safe failure provenance 与已保存内容投影为
  `preparing / ready / no_update / failed / needs_attention`。
- `ACTIVE + failed/incomplete/blocked` 是合法正交组合；失败只影响本期文案，不反写 Feed success。
- Home 先分组再按 `(updated_at, update_id, feed_id)` 倒序，ready 永远在 preparing 之前。
- `/api/updates` 与 `/api/feeds/{feed_id}` 仅返回产品 DTO；Web 只调用 application façade，不访问 repository。
- 所有 GET 与页面 polling 都是 pure read；只有 P4.1 commit/startup wake 会推进既有 durable first-briefing work。

`search_empty_results` 被翻译为“本期没有值得推荐的新内容”；其他已知 incomplete/failed 使用不含内部原因的可行动
文案；unknown/blocked 明确提示不会自动重做，避免用户制造重复内容。

## 3. Feed Detail、历史与 provenance

- 每期展示 item 标题、摘要、生成日期和来源链接；来源标题、domain、发布时间来自生成时已保存的
  `ContentCandidate`，URL 来自已验证的 canonical source reference。
- repository 只新增读取既有 `content_candidates` 的方法；没有新增 durable state 或重新搜索来源。
- 当前关注范围来自 canonical current definition（legacy Feed 回退到当前 Subscription）。
- 每个历史 briefing 的范围来自该次 `DigestRunRecord.subscription_snapshot`，不会用当前 definition 重解释旧内容。
- “为什么推荐”忽略 provider 自由文本，只从该期已保存的 topic tags、score breakdown、Profile snapshot、freshness 与
  seen penalty 派生 1–3 句。旧 briefing 在 feedback、definition 更新和应用重启后保持不变。
- API/UI 不返回 Evidence ID、projection identity、raw score 或内部 failure provenance。

## 4. IA 与范围控制

- `/`：Updates；不放运行、交付、Profile raw weights 或订阅操作台。
- `/create`：保留 P4.1 Create Feed / multi-turn confirmation journey。
- `/feeds/{feed_id}`：内容和历史阅读页。
- `/following`：关注列表和进入详情的入口。

P4.5 才定义 product-safe pause/resume 与 versioned definition edit。现有 compatibility `enable/disable` 只更新
Subscription，不能安全代表 ProductSubscription/UserSubscription 的原子关系变化；因此 P4.2 没有在 Following
伪装提供该控制，旧 HTTP compatibility endpoint 保持不变。

## 5. 与设计 / prompt 的边界

正式 P4.2 acceptance criterion 要求 source/history/current definition，但把完整 Why Recommended + Feedback
Acknowledgement 排在 P4.3。本次实现 prompt 又明确要求展示“已有 deterministic recommendation explanation 能支持的
为什么推荐”。实现选择只交付 **read-only deterministic explanation**，没有加入反馈按钮、反馈确认、reason-code DTO
或 Interest UI，因此没有扩张到 P4.3 的闭环。

## 6. Acceptance evidence

| P4.2 criterion | 实现 / 测试证据 |
|---|---|
| sealed Home/Feed DTO；Web thin client | façade DTO、两个 product GET API、architecture test |
| ready 优先、排序 deterministic | 多 Feed application/HTTP tests；相同时以稳定 identity tie-break |
| Feed 与 briefing 正交 | Product E2E 同时断言 `active + failed`、`active + preparing`、`active + ready` |
| GET/poll pure read | 调用前后 Search/Generation/Delivery call counts 与 durable outbox 完全相同 |
| source/history/current definition | Feed Detail application、HTTP 与 render tests |
| historical snapshots 不漂移 | feedback + definition change + repository reopen 后旧 definition/why 保持不变 |
| failure product copy | INCOMPLETE/FAILED/BLOCKED/empty mapping tests；Web 不出现内部 error |
| 无用户侧内部概念 | DTO key scan、HTML contract scan、architecture gate |
| core boundary | `git diff --exit-code -- mini_harness_core` 为空 |

## 7. 新发现但不阻塞 P4.2 的 gap

1. **Unread/read read model**：当前没有 durable “用户已读某一期”事实，Updates 只能把每个 Feed 的最新内容视为可读，
   不能兑现设计中的“未读优先、已读历史退出首屏”。这是 Product/Application read-model gap，不是 Harness gap。
2. **Latest briefing series identity**：当前 Product durable reservation 明确覆盖首篇；后续手动 legacy generation 没有
   application-owned latest-request series。持续更新、失败对应哪一期以及 cadence 去重属于 P4.6。
3. **Product-safe management**：pause/resume 需要同时维护 product/relation/compatibility truth，属于 P4.5；不能把旧
   Subscription toggle 当作完整实现。
4. **P4.3 acknowledgement**：本轮解释可读但没有反馈动作、幂等 UI event acknowledgement 或 reason codes。

以上都没有证明 Harness core 能力不足；P4.2 复用现有 Search、Evidence acceptance、Output Contract、durable Result 和
历史 snapshot 即可完成。

## 8. Verification record

完成实现后执行并记录：

```text
focused deterministic/application/HTTP/repository/architecture tests: PASS（56 tests）
python -m unittest -q: PASS（873 tests）
python mini_harness.py --self-check: PASS
JavaScript syntax check（Node.js v22.22.1）: PASS
git diff --check + docs trailing-whitespace scan: PASS
changed-scope secret scan + new runtime artifact scan: PASS
git diff --exit-code -- mini_harness_core: PASS（empty）
Browser Engine E2E: NOT RUN（仓库没有 Browser Engine automation）
```

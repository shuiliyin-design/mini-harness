# P4.6 Verified EVENT Implementation Status

## 已实现的产品链

唯一受支持需求是“OpenAI 发布新模型时告诉我”。validated Definition 只有 exact
`OpenAI / MODEL_RELEASED / MODEL / PUBLIC_AVAILABILITY / FUTURE_FROM_ACTIVATION` shape 才选择 `EVENT`；其他 EVENT、
CONDITION 与 UNKNOWN 继续 deterministic fail closed，不 fallback BRIEFING，也不信任 Model 自报 workflow。

当前 Fake vertical slice 是：

```text
natural-language confirmation
  -> durable EVENT Subscription + initial cycle/temporal cursor
  -> typed Fake OpenAI Source Observation + immutable Evidence
  -> existing Agent Harness Result containing bounded Event Candidate
  -> application openai_model_release_v1 verifier + verification Evidence
  -> Verified Event
  -> EVENT Update -> UserSubscription Distribution
  -> existing Distribution-aware Notification -> HTTP/UI projection
```

EVENT commit 不创建 `BriefingReservation`、`FIRST_BRIEFING_REQUESTED`、Briefing application run 或 CONDITION request。
schema v17 增加窄 EVENT Definition/policy/cycle/temporal/Observation/Candidate/Verification/Verified Event 表，并把 shared
`tracking_updates` 扩为 CONDITION/EVENT；v16 的 CONDITION/Distribution/Delivery 数据原样迁移且 foreign keys 继续有效。

## Candidate 与 verifier authority

Fake candidate adapter 通过现有 `run_agent`、append-only Audit 与 durable Result 运行；stable cycle-bound Harness Result 在
process crash/restart 后直接复用。Agent 只能提出 entity/type/model/time 和本 Observation 内 exact source refs/spans，strict
contract 拒绝未知字段、多个 candidate、虚构 source ref 与不存在的 span。

`openai_model_release_v1` verifier 由 Application 拥有，按顺序检查 coverage、official HTTPS hostname/publisher、entity/type、
deterministic model normalization、release assertion、conflict marker、source-owned published time、activation/freshness scope与
logical identity。Candidate、URL、报道数量、cycle 或 Agent run 均不拥有 event truth，也不进入 logical event identity。

## Outcomes 与 dedupe

- complete coverage + zero candidate、duplicate logical event、明确 out-of-scope是 successful `NO_UPDATE`；Subscription保持ACTIVE。
- 缺官方支持、冲突、source time/模型名/release semantics无法确认或coverage truncated是
  `VERIFICATION_INCOMPLETE`；不推进 success watermark，不创建 Update/Distribution/Notification。
- provider timeout/error、invalid Observation、Agent contract/Harness/Evidence persistence failure是failed cycle；下一 cadence继续。
- Verified Event identity是`entity + event type + object type + canonical model key`。同一 Model X 多来源、下一cycle或restart replay
  只有一个 logical Verified Event、一个 subscription Update/Distribution；Model Y形成新事件。

## Temporal、Notification 与 UI

EVENT 复用 P4.4 的 immediate initial、6h product default、Fake Clock、anchored due、missed-slot coalescing、claim recovery、
pause/resume与bounded overlap；不使用 CONDITION latch/crossing/re-arm，也没有Flight expiry。pause不观察，resume只创建一个
immediate cycle且不补发pause窗口。

Verified EVENT Distribution 直接复用 P4.5 eligibility、logical delivery/attempt identity、accepted/failure/unknown certainty与
Termux adapter；NO_UPDATE、incomplete、duplicate和feed-only调用数都是0。Notification failure不修改Feed中的Verified Event Update。

HTTP/UI只显示“正在关注 OpenAI 新模型”“暂无新动态”“发现并验证了新模型发布”“本次信息暂时无法确认”以及safe official
source projection；不暴露 Candidate/Evidence/verifier/internal event key、Harness run、attempt或adapter。

## 验收边界

Fake correctness tests覆盖：没有新模型；Model X官方充分支持；多来源与下一cycle duplicate；缺官方；冲突；coverage truncated；
Model X→duplicate→Model Y；exactly two Update/Distribution/Notification；crash/restart Result reuse；pause/resume与HTTP/UI safe
projection。

本实现 checkpoint 的实际 verification record：

```text
P4.6 focused Fake EVENT tests: PASS (7 tests)
python -m unittest -q: PASS (926 tests, 285.786s)
python mini_harness.py --self-check: PASS
git diff --check: PASS
JavaScript syntax: PASS (4 inline scripts; Node.js v22.22.1)
docs links: PASS (625 local links, 74 Markdown files)
configured-secret/high-confidence changed-line scan + runtime artifact scan: PASS
git diff --exit-code -- mini_harness_core: PASS (empty)
real demo DB v16 -> v17: PASS (35 historical tables preserved; EVENT rows=0; integrity/FK clean)
real OpenAI/Brave network: NOT RUN (out of scope)
```

本 slice 不证明真实 OpenAI/Brave Observation、互联网coverage、生产daemon、correction/retraction、generic EVENT ontology/DSL、
shared execution或read receipt。未修改 `mini_harness_core`；现有 Observation/Evidence/Result/recovery seams 足够，未发现core gap。

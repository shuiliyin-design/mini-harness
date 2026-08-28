# Agent Capability Map

## 1. 判断方法

只有需要理解开放语言、处理语义歧义或生成自然语言内容的步骤才交给 Agent。凡是涉及 identity、状态迁移、
权限、数值限制、排序、幂等、调度和事实投影，都保持 deterministic。

## 2. 能力地图

| 用户体验 | Agent capability | Deterministic owner / gate | 当前支持 | Product gap |
|---|---|---|---|---|
| 描述想关注的主题 | 从自然语言提取 intent candidate 与 supporting user turn | input policy、strict protocol、field allowlist、preference evidence check | Fake/Vertex Definition adapters 已有 | 当前 UI 不展示 conversation history |
| 多轮澄清 | 判断下一件最有价值的问题，提出 `NEXT_QUESTION` | server-owned conversation、idempotency、turn ceiling | 已支持任意多轮（bounded） | accepted 后不能自然“调整并继续” |
| 完成理解 | 提出 `DONE {intent}` | application defaults materialization + business validation | 已支持 | durable Definition 不是 Conversation schema |
| 选择 tracking workflow | 可提出 intent/signal candidate | application 按 validated definition shape 确定性选择/拒绝 | 已支持 `BRIEFING` 与窄 flight `CONDITION`；其他 shape fail closed | EVENT 与更多 CONDITION executor 未实现 |
| 拒绝不支持请求 | 提出 bounded safe reason | exact `REJECT` schema、产品 allowlist | 已支持 | 用户恢复路径不足 |
| 是否需要 Definition Confirmation | 无 | 初次/重大变更的 deterministic product policy | commit endpoint 可承载明确动作 | 当前浏览器没有确认 gate |
| 创建 Feed | 无 | transaction、ownership、idempotency、resource identities | 已支持 | UI 投影不足 |
| 获取外部 Observation | 可辅助形成查询或调用工具 | Tool policy、Authority、Observation schema、Evidence acceptance | Search 与 one-shot Fake flight price 已支持 | 真实价格、持续 cadence、event source 未实现 |
| 选择哪些内容 | 无 | deterministic ranking、seen penalty、profile weights | 已支持 | Home/Feed read model 缺失 |
| 生成 briefing | 基于已选候选合成简洁内容 | selected prefix、source refs、limits、Output Contract | 已支持 | 连续自动触发缺失 |
| 判断价格是否低于阈值 | 无 | typed Observation + deterministic comparator + unit/rule version | 窄 flight `price < threshold` 已实现 | 通用 CONDITION DSL 明确不在范围 |
| 识别事件候选 | 可从 Observation 提出 event candidate | supported event type、Evidence binding、fact validation、dedupe | 未实现 | EVENT vertical slice |
| 创建 Update | 无 | verified trigger、definition snapshot、identity、transaction | Digest read adapter + CONDITION Update 已支持 | EVENT Update 未实现 |
| 分发给用户 | 无 | active UserSubscription、Distribution identity/state | CONDITION 已有独立 Distribution | BRIEFING Delivery 仍直接绑定 digest/user |
| 外部通知 | 无 | preference、channel policy、attempt identity、effect certainty | Digest Delivery 已支持部分语义 | 需改为引用 Distribution |
| 为什么推荐 | 可把受约束 facts 润色成一句话 | reason codes 必须由 score/profile/definition 派生 | payload 已有 candidate reason + breakdown | 缺 sealed user-facing explanation |
| 点赞/减少/收藏/打开 | 无 | stable feedback event、atomic profile rule | 已支持 | 缺反馈确认和兴趣历史 |
| 自由文本偏好（未来） | 提出 topic/direction candidate | allowlist、bounds、version CAS、可选用户确认 | 未实现 | 先验证需求，不进 P4.1 |
| 兴趣演化 | 无 | ProfileUpdate/Interaction replayable rules | current weights/history 已 durable | 缺用户语言 projection |
| pause/resume/history | 无 | business lifecycle/versioning | 部分支持 | material definition edit/versioning 不完整 |
| cadence / scheduling | 无 | application scheduler、clock、dedupe、quota | cadence 字段已有；自动执行未实现 | 真实“持续”承诺的主要缺口 |

## 3. Definition Agent 的边界

```text
User turns
  -> Agent proposes NEXT_QUESTION | REJECT | DONE
  -> conversation protocol / clarification policy validation
  -> deterministic defaults materialization + provenance
  -> durable definition business validation
  -> product confirmation policy
  -> explicit user confirmation
  -> application transaction
```

任何前一步都不能跳过后一步。尤其：

- `DONE` 不是 Subscription；
- Conversation schema 不是 Definition schema；
- required internal fields 不是 required user questions；
- structured-output valid 不是 definition valid；
- definition valid 不是用户确认；
- 用户确认不是 Harness Approval；
- Harness completed 不是 product commit。

## 4. Why Recommended 的边界

推荐理由适合“deterministic facts + optional Agent phrasing”，而不是让 Agent回忆为什么推荐：

```text
deterministic reason facts
  {matched_focus, explicit_feedback, freshness, unseen}
      -> optional constrained phrasing candidate
      -> exact fact-reference validation
      -> user-facing sentences
```

P4 初期直接用模板更小、更可解释，也不需要增加一次模型调用。只有模板明显损害理解时，再评估 Agent phrasing。

## 5. 三类 workflow 的责任链

```text
validated Tracking Definition
  -> application workflow selector
     BRIEFING  -> Observe/Search -> deterministic Select -> Agent Generate -> validate -> Update
     CONDITION -> Observe        -> deterministic Evaluate              -> Update or no_update
     EVENT     -> Observe        -> Agent/Rules detect -> Evidence validate -> Update or no_update
```

Application selector 的最小规则：

| Validated intent shape | Selected workflow | Fail-closed rule |
|---|---|---|
| 用户要一段时间内的主题变化整理，没有离散 event 或 machine-evaluable predicate | `BRIEFING` | 不能把未知/不支持 shape 默认成 BRIEFING |
| 有完整且受支持的 `metric + operator + value + unit` predicate | `CONDITION` | 缺 metric/value/unit 时继续必要澄清或拒绝，不能交给 LLM 判断 |
| 有明确 entity + discrete event criterion，目标是发生即更新 | `EVENT` | 没有受支持 event type 或 verification contract 时拒绝 commit |

一个 definition 只能选择一个当前 workflow。未来复合 intent 需要独立 product decision；当前 selector 不拆分、不级联，
也不因内部 cadence/presentation defaults 改变 workflow。

- Model 可以理解 subject、constraints、goal、trigger，并提出 signal candidate；它不拥有 selector 或 commit Authority。
- selector 不信任 Model 的枚举自报，而是验证 signal shape、required criterion、受支持 metric/event type 与字段一致性。
- CONDITION 的数值比较、单位匹配、边界运算和去重完全 deterministic，LLM 不参与最终 truth evaluation。
- EVENT detection 可以使用 Agent 处理开放语义，但候选只有绑定 accepted Evidence 并通过 application validator 后
  才能成为 Update。
- 三类 workflow 复用同一个 Agent Harness 的 tool policy、Evidence、Output Contract、Result 与 recovery 语义；
  `mini_harness_core` 不出现 flight/briefing/event 分支。

## 6. “越来越懂我”的诚实范围

V1 可承诺：系统会根据用户明确的 liked/dismissed/saved/opened 信号调整后续排序。V1 不应承诺：

- 从任意行为推断长期人格；
- 跨用户/跨 Feed 的隐式 embedding memory；
- Agent 自主修改 Profile；
- 把 generated explanation 当作偏好变化证据。

透明的 deterministic learning 比不可审计的“Agent memory”更符合当前教学目标。

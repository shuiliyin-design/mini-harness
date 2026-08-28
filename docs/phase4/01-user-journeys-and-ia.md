# User Journeys and Information Architecture

## 1. 推荐 IA

移动端一级导航保持三个用户概念：

```text
更新 Updates
├── 待阅读 / 最新内容
├── 正在准备或需要处理的 Feed 卡片
└── 创建关注入口

关注 Following
├── Active Feeds
├── Paused Feeds
└── Feed management

我的 Me
├── 兴趣变化
├── 通知偏好
└── 产品说明 / 隐私
```

`Create Feed` 是 Updates 与 Following 上的主动作，不是第四个永久 tab。`Feed Detail` 是一个具体 Feed 的聚合页：

```text
Feed Detail
├── current definition
├── latest observation / update state
├── update history
├── why updated / feedback
└── manage feed
```

不设置 Run、Outbox、Evidence、Provider、Profile raw weights 或 Delivery records 页面。内部 operator view 若未来需要，
必须是独立 admin surface，不能混入用户 IA。

## 2. Journey A：首次创建

1. 用户从 Updates 空状态或顶部 `+ 新建关注` 进入 Create Feed。
2. 用户用自然语言说关注主题、范围或期望。
3. 服务持久化安全输入并执行一个 bounded Definition turn。
4. `NEXT_QUESTION` 时页面追加问题和回答；刷新后从 durable conversation 恢复。
5. `DONE` 时进入 Definition Confirmation，不自动 commit。
6. 用户确认后调用幂等 commit；只有 transaction COMMIT 才显示订阅成功。
7. 成功页独立显示“正在开始关注”；后续是首次 Briefing、首次 condition observation 或首次 event observation，
   由已验证的 workflow 决定，不等待外部工作或 Notification。

异常分支：

- `REJECT`：说明当前能做什么，提供重新描述入口，不创建 Feed。
- conversation `INCOMPLETE`：保留已输入内容，提供“再试一次/重新开始”的产品动作；不暴露 Harness failure。
- 用户选择“调整”：回到 clarification，产生新的 candidate；旧 accepted candidate 不能被客户端偷偷改写。

## 3. Journey B：首次观察、Update 与阅读

1. Updates 显示订阅成功卡片和独立准备状态。
2. 只读 polling/refresh 读取 product projection；读取不能推进 work。
3. tracking workflow 接受 Observation 后，由 application 决定是 `no_update` 还是创建 durable Update。
4. READY 后卡片变为 update preview；点击进入 Feed Detail/Update。
5. BRIEFING 显示条目与来源；CONDITION 显示观测值、阈值、单位和观测时间；EVENT 显示被验证事件及 Evidence 来源。
6. `为什么现在更新` 只引用 definition、accepted Observation/Evidence 与 deterministic evaluation facts；Agent 可以
   生成受约束文案，不能发明触发原因。

当前 P4.2 只实现 BRIEFING projection；其他两类是后续 vertical slice，不得由通用文案伪装成已支持。

## 4. Journey C：反馈与兴趣演化

1. 用户对 item 选择“多来点 / 少来点 / 收藏”；打开全文是独立的 opened signal。
2. 页面立即显示该 feedback 已记录，并防止重复点击造成多次累计。
3. 后续 BRIEFING generation 读取新的 profile version；旧 Update 保留原 definition/profile snapshot。
4. “我的兴趣”显示可理解的 topic 变化与来源（例如“两次多来点”），而不是 `agent learned` 的不可验证声明。

自由文本“为什么不喜欢”是未来能力：Agent 只提出结构化兴趣变化 candidate，application 必须验证 topic、范围、
版本和用户确认后再更新 Profile。

## 5. Journey D：管理与历史

- 用户可以 pause/resume Feed；pause 是业务关系状态，不等于删除历史。
- 用户可以查看 current definition、创建时间、最近更新时间和 Update history。
- material definition edit 必须形成新 definition version，并重新确认；旧 execution/Update 继续绑定旧 snapshot。
- 删除若未来提供，应先定义 retention/recovery 语义；V1 保持 pause，不做 hard delete。
- “立即检查”若保留，是用户请求一次新 observation cycle 的产品动作，不叫 Run now，且必须有稳定 idempotency identity。

## 6. Journey E：失败恢复

- 准备较慢：显示“仍在准备”，允许离开页面；不承诺虚假 ETA。
- 可安全重新请求：创建新的用户可见 observation/update request，保留旧失败历史。
- effect/current truth 不明确：显示“需要处理，我们不会重复生成/发送”，不提供盲重试。
- 订阅成功后 observation/update 失败：Feed 保持 Active；用户可以稍后重试或 pause。
- Notification 失败：Update 仍在 Updates 可读；失败不改写 Feed、Update 或 Distribution truth。

## 7. Update / Distribution / Notification 的 IA 边界

```text
Tracking execution -> Update (发生了什么、为什么成立)
                     -> Distribution (哪个 UserSubscription 可以看到、已读与否)
                       -> Notification (是否尝试通过某个渠道送达)
```

- `Update` 是共享事实/内容对象，不拥有 recipient、已读或渠道状态。
- `Distribution` 是 Update 与 `UserSubscription` 的用户级绑定，是 Updates/未读投影的来源。
- `Notification` 是某个 Distribution 的外部送达尝试，保留 attempt、channel 与 effect certainty。
- `UserSubscription` 是分发真源；不能从 Update 的 `subscription_id` 或 Delivery 的 `user_id` 猜 recipient。
- 本轮只设计未来 fan-out cardinality；后续第一个 vertical slice 仍允许一条 Update 对一个 UserSubscription，
  不实现 shared execution。

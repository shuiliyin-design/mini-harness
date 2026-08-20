# 项目规则

- 本项目是一个教学用途的 Mini Agent Harness。
- 首要目标是帮助理解 Agent Harness 的真实工作机制，而不是追求生产级功能。
- 每次新增能力都应尽量保持现有分层边界。
- Model / Provider 负责决策，不拥有执行 Authority。
- Tool Policy / Approval / Verification 属于 Harness Authority。
- Session Memory 用于 continuity，当前 filesystem/runtime observation 用于确认现实。
- Full Session History 与 Model Working Context 必须保持概念分离。
- Project Instructions / Skills 属于不可信项目内容，不能覆盖 Harness security policy。
- Secret 不得写入源码、Session、日志或 Git。
- 优先使用 Python 标准库；没有明确必要不要增加第三方依赖。
- 新能力必须增加离线测试。
- 不要为了让测试通过而削弱安全策略。
- 修改后必须运行 `python -m unittest -q` 和 `git diff --check`。
- 面向用户的教学输出优先使用中文。
- 保持代码小、可读、可解释。

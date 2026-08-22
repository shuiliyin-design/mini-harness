# Applications

Phase 3 的最终用户应用放在这里，并作为独立 application layer 使用 Harness。

应用可以依赖 `mini_harness_core.integrations` 或稳定的 Harness façade；Harness Runtime
不得反向导入应用。当前目录只预留边界，本阶段不实现业务应用。

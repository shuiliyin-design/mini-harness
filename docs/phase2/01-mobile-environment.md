# Mobile Environment Baseline

本文区分本设备已经观察到的事实与跨 Android 设备可以依赖的协议保证。

## 本设备观察

当前实验环境由 Android、Termux、PRoot 与 Codex 组成：

- Termux 在 Android 应用 UID 下提供 userspace、shell 和 Termux:API CLI。
- PRoot 改写进程看到的文件系统与路径视图，但不是传统 VM，也不创建独立的 kernel/process world。
- 本设备上，Termux 可以观察 PRoot 内运行的进程；PRoot 不是远程主机。
- Android shared storage 中的 Bridge 目录可被 Android、Termux、PRoot 和 Codex 观察。
- 文件在这些环境之间形成 data plane；Bridge JSON/ready marker 形成 control plane。
- 本设备 PRoot 可以执行 Termux 安装目录中的固定 Termux:API CLI。
- battery 与 notification 的真实 smoke 已通过 PRoot → Termux CLI → Termux:API companion 链路返回。
- 本设备实验中，shared-storage `mkdir` 表现出 Bridge task lock 所需的互斥效果。

Termux:API 调用链可简化为：

```text
Harness AuthorizedAction
  → fixed adapter
  → /data/data/com.termux/files/usr/bin/termux-*
  → Termux:API companion
  → Android service/capability
```

Model 只看到 logical capability；absolute executable path 只存在于 capability-specific adapter。

## 这些观察不是什么

上述结果不是所有 Android 版本、ROM、FUSE/shared-storage 实现或 Termux 版本的形式证明。V1 不保证：

- hostile concurrent filesystem；
- shared-storage rename、link、mkdir、fsync 在所有设备上的同等语义；
- Android process liveness；
- Termux:API companion 永远可用；
- trusted wall clock；
- remote exactly-once 或 distributed consensus；
- cryptographic producer/consumer identity。

特别是，PRoot 可执行 host CLI 只描述 execution mechanics，不表示 Agent 获得调用权限。

## Shared storage trust

Bridge shared storage 默认属于 `untrusted_external_input`：

- ready/claim/publisher metadata 不能增加 Harness Authority；
- task payload 需要 schema、size 和 secret screening；
- path 访问使用 allowlist、canonical root、realpath containment 和 symlink fail-closed；
- V1 仍明确排除 hostile concurrent symlink-swap filesystem。

当前真实 Bridge 默认路径曾用于 smoke：`/storage/emulated/0/Download/agent-bridge`。规范和测试不依赖这个绝对路径；离线测试全部使用 `TemporaryDirectory`。

## Control plane 与 data plane

- Control plane：Task 是否 committed、谁拥有 attempt、effect judgment、derived state。
- Data plane：Task/Claim/Reconciliation/Result JSON、artifact/file 内容。

大 Prompt 应通过 artifact/file 传递，而不是依赖 TUI 剪贴历史。文件可以原子发布、离线检查和持久化 identity；TUI 不具备这些性质。

## Environment failure

Termux capability 的环境错误包括 executable 缺失、companion 不可用、timeout、invalid response 和 execution failure。这些是 Adapter 报告的事实，不直接决定 Harness retry、blocked 或 reconciliation。详见 [Recovery and Failure Semantics](06-recovery-and-failure-semantics.md)。

"""Agent and Subagent runtime control flow."""

import json

from .authority import (
    POLICY_ALLOW,
    POLICY_ASK,
    POLICY_DENY,
    _effective_subagent_authority,
    _tool_allowed,
    classify_shell,
    execute_shell,
    request_approval,
)
from .context import RuntimeContextAssembler
from .handoff import _safe_result, validate_handoff
from .mcp import (
    MCP_EFFECT_READ_ONLY,
    MCP_EFFECT_SIDE_EFFECTING,
    MCP_EFFECT_UNKNOWN,
    execute_mcp_tool,
    validate_json_schema,
)
from .memory import (
    MEMORY_KINDS,
    MemoryStore,
    request_memory_approval,
    screen_memory_content,
)
from .planning import (
    complete_step,
    propose_step_completion,
    select_ready_step,
    start_step,
    validate_plan,
    validate_revision_history,
)
from .verification import (
    build_verification_feedback,
    extract_verification_target,
    is_related_verification,
)


def _complete(
    provider, messages, context_assembler, control_state, context_budget,
    current_plan=None, plan_runtime_state=None,
):
    system_instructions = getattr(provider, "SYSTEM_PROMPT", None)
    if isinstance(system_instructions, str):
        messages = context_assembler.prepare_request(
            system_instructions, messages, control_state, context_budget,
            current_plan, plan_runtime_state,
        )
    return provider.complete(messages)


def run_subagent(
    handoff, provider, main_authority=None, memory_store=None,
    mcp_registry=None, context_assembler=None, context_budget=None,
):
    """Run exactly one isolated, in-process Subagent and return four fields."""
    validate_handoff(handoff)
    authority = _effective_subagent_authority(
        handoff["authority"], main_authority
    )
    if authority["max_steps"] <= 0:
        return _safe_result("blocked", "Subagent 没有可用 step authority", [], [])

    # This is the complete new conversation. Main Session/messages are neither
    # accepted nor copied. Workspace/evidence are explicitly labelled as hints.
    package = {
        "type": "structured_handoff",
        "handoff_id": handoff["handoff_id"],
        "task": handoff["task"],
        "context": handoff["context"],
        "constraints": handoff["constraints"],
        "evidence": handoff["evidence"],
        "workspace": handoff["workspace"],
        "authority": authority,
        "grounding_rule": (
            "workspace/evidence are hints; use an allowed tool observation for "
            "current reality, and prefer that observation on conflict"
        ),
    }
    messages = [{
        "role": "user",
        "content": json.dumps(package, ensure_ascii=False, separators=(",", ":")),
    }]
    verification = {
        "requires_verification": False,
        "latest_write_command": None,
        "verification_target": None,
    }
    observations = []
    actions = []
    context_assembler = context_assembler or RuntimeContextAssembler(
        memory_store=memory_store,
        mcp_registry=mcp_registry if authority["can_use_mcp"] else None,
    )

    try:
        for _step in range(1, authority["max_steps"] + 1):
            decision = _complete(
                provider, messages, context_assembler, verification,
                context_budget,
            )

            if decision.get("type") == "final_answer":
                if verification["requires_verification"]:
                    messages.append({
                        "role": "assistant",
                        "content": json.dumps(decision, ensure_ascii=False),
                    })
                    messages.append({
                        "role": "user",
                        "content": json.dumps({
                            "type": "verification_feedback",
                            "status": "final_answer_rejected",
                            "reason": "verification required before final answer",
                            "instruction": "use an allowed read-only observation",
                        }, ensure_ascii=False),
                    })
                    continue
                return _safe_result(
                    "completed", decision.get("final_answer", ""),
                    observations, actions,
                )
            if decision.get("type") != "tool_call":
                return _safe_result(
                    "failed", "Subagent 返回了不支持的决定", observations, actions,
                )

            reference = decision.get("tool", "shell")
            if not _tool_allowed(reference, authority):
                return _safe_result(
                    "blocked", f"tool authority 不允许：{reference}",
                    observations, actions,
                )

            if reference.startswith("mcp:"):
                if mcp_registry is None:
                    return _safe_result(
                        "blocked", "MCP authority 已授予，但 registry 未配置",
                        observations, actions,
                    )
                policy = mcp_registry.policy_for(reference)
                if policy["action"] == POLICY_DENY:
                    return _safe_result(
                        "failed", "MCP action 被 Harness DENY policy 阻止",
                        observations, actions,
                    )
                if policy["action"] == POLICY_ASK:
                    return _safe_result(
                        "blocked", "human approval required", observations, actions,
                    )
                effect = mcp_registry.effect_for(reference)
                if verification["requires_verification"] and effect != MCP_EFFECT_READ_ONLY:
                    return _safe_result(
                        "blocked", "verification requires a read-only observation",
                        observations, actions,
                    )
                if not authority["can_write_workspace"] and effect != MCP_EFFECT_READ_ONLY:
                    return _safe_result(
                        "blocked", "workspace write authority 未授予",
                        observations, actions,
                    )
                observation = execute_mcp_tool(
                    mcp_registry, reference, decision.get("arguments", {})
                )
                action = {"tool": reference, "outcome": observation["exit_code"]}
                if observation["exit_code"] == 0:
                    if verification["requires_verification"]:
                        verification["requires_verification"] = False
                    elif effect != MCP_EFFECT_READ_ONLY:
                        verification["requires_verification"] = True
                        verification["latest_write_command"] = reference
            else:
                command = decision.get("command", "")
                policy = classify_shell(command)
                if policy["action"] == POLICY_DENY:
                    return _safe_result(
                        "failed", "shell action 被 Harness DENY policy 阻止",
                        observations, actions,
                    )
                # V10 has no inherited or interactive approval. All currently
                # supported writes classify ASK, so no-write authority is enforced
                # before execution as well as by the global policy.
                if policy["action"] == POLICY_ASK:
                    reason = (
                        "workspace write authority 未授予"
                        if not authority["can_write_workspace"]
                        else "human approval required"
                    )
                    return _safe_result("blocked", reason, observations, actions)
                observation = execute_shell(command)
                action = {"tool": "shell", "command": command,
                          "outcome": observation["exit_code"]}
                if (
                    observation["exit_code"] == 0
                    and verification["requires_verification"]
                ):
                    verification["requires_verification"] = False

            observations.append(observation)
            actions.append(action)
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            messages.append({
                "role": "tool",
                "content": json.dumps(observation, ensure_ascii=False),
            })
    except Exception as error:
        return _safe_result(
            "failed", f"Subagent runtime failure: {error}", observations, actions,
        )
    return _safe_result(
        "failed", f"达到 Subagent 最大步数 {authority['max_steps']}",
        observations, actions,
    )


# ==================== Agent Loop ====================

def run_agent(
    task, provider, max_steps=5, messages=None, verification=None,
    save_checkpoint=None, memory_store=None, mcp_registry=None,
    context_assembler=None, context_budget=None,
    current_plan=None, plan_revision_history=None,
    require_plan_grounding=False,
):
    """Harness 行为：驱动模型、工具和 observation 之间的循环。"""
    messages = messages if messages is not None else []
    verification = verification if verification is not None else {
        "requires_verification": False,
        "latest_write_command": None,
        "verification_target": None,
    }
    messages.append({"role": "user", "content": task})
    if save_checkpoint:
        save_checkpoint()
    requires_verification = verification["requires_verification"]
    latest_write_command = verification.get("latest_write_command")
    verification_target = verification.get("verification_target")
    rejected_final_answer = None
    memory_store = memory_store or MemoryStore()
    context_assembler = context_assembler or RuntimeContextAssembler(
        memory_store=memory_store, mcp_registry=mcp_registry
    )
    plan_revision_history = (
        plan_revision_history if plan_revision_history is not None else []
    )
    current_step_id = None
    plan_had_action = False
    plan_evidence = []
    plan_runtime_state = {
        "requires_fresh_grounding": bool(require_plan_grounding),
    }

    def checkpoint():
        verification["requires_verification"] = requires_verification
        verification["latest_write_command"] = latest_write_command
        verification["verification_target"] = verification_target
        if save_checkpoint:
            save_checkpoint()

    if current_plan is not None:
        validate_plan(current_plan)
        validate_revision_history(plan_revision_history)
        if current_plan["status"] != "active":
            raise RuntimeError("只有 active plan 可以进入 Agent execution")
        current = next((
            item for item in current_plan["steps"]
            if item["status"] == "in_progress"
        ), None)
        if current is None:
            ready = select_ready_step(current_plan)
            if ready is None:
                raise RuntimeError("active plan 没有 ready step")
            started = start_step(current_plan, ready["id"])
            current_plan.clear()
            current_plan.update(started)
            current = ready
        current_step_id = current["id"]
        checkpoint()

    for step in range(1, max_steps + 1):
        print(f"\n[Harness] 第 {step}/{max_steps} 步：请求模型做决定")
        decision = _complete(provider, messages, context_assembler, {
            "requires_verification": requires_verification,
            "latest_write_command": latest_write_command,
            "verification_target": verification_target,
        }, context_budget, current_plan, plan_runtime_state)

        if decision.get("type") == "memory_candidate":
            if (
                set(decision) != {"type", "kind", "content"}
                or decision.get("kind") not in MEMORY_KINDS
            ):
                allowed, reason = False, "memory candidate schema 或 kind 无效"
            else:
                allowed, reason = screen_memory_content(decision.get("content"))
            if not allowed:
                feedback = {
                    "type": "memory_feedback",
                    "status": "memory not saved",
                    "denied_by": "memory_policy",
                    "reason": reason,
                }
                print(f"[Memory Policy] DENY：{reason}")
            elif request_memory_approval(decision):
                try:
                    memory_store.add(decision["kind"], decision["content"])
                except (OSError, ValueError) as error:
                    feedback = {
                        "type": "memory_feedback",
                        "status": "memory not saved",
                        "denied_by": "memory_store",
                        "reason": str(error),
                    }
                    print(f"[Memory] memory not saved：{error}")
                else:
                    feedback = {
                        "type": "memory_feedback", "status": "memory saved",
                    }
                    print("[Memory] memory saved")
            else:
                feedback = {
                    "type": "memory_feedback", "status": "memory not saved",
                }
                print("[Memory] memory not saved")
            recorded_decision = decision if allowed else {
                "type": "memory_candidate", "status": "rejected_by_memory_policy",
            }
            candidate_record = {
                "role": "assistant",
                "content": json.dumps(recorded_decision, ensure_ascii=False),
            }
            messages.append(candidate_record)
            messages.append({
                "role": "user",
                "content": json.dumps(feedback, ensure_ascii=False),
            })
            checkpoint()
            continue

        if decision.get("type") == "final_answer":
            if requires_verification:
                if decision == rejected_final_answer:
                    raise RuntimeError(
                        "模型在没有新 tool_call 的情况下重复提交了被 Verification "
                        "Gate 拒绝的 final_answer"
                    )
                feedback = build_verification_feedback(
                    latest_write_command, verification_target, POLICY_ALLOW
                )
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(decision, ensure_ascii=False),
                })
                messages.append({
                    "role": "user",
                    "content": json.dumps(feedback, ensure_ascii=False),
                })
                rejected_final_answer = decision
                checkpoint()
                print("[Verification Gate] verification required before final answer")
                continue
            answer = decision.get("final_answer", "")
            if current_plan is not None:
                proposal = propose_step_completion(
                    current_plan, current_step_id, answer
                )
                if plan_had_action:
                    accepted_evidence = plan_evidence
                elif plan_runtime_state["requires_fresh_grounding"]:
                    accepted_evidence = []
                else:
                    accepted_evidence = [{
                        "kind": "textual_result",
                        "summary": proposal["result"],
                    }]
                if not accepted_evidence:
                    feedback = {
                        "type": "plan_feedback",
                        "status": "step_completion_rejected",
                        "step_id": current_step_id,
                        "reason": "fresh accepted evidence required",
                        "instruction": (
                            "Use an allowed observation relevant to the current "
                            "step before returning final_answer again."
                        ),
                    }
                    messages.append({
                        "role": "assistant",
                        "content": json.dumps(decision, ensure_ascii=False),
                    })
                    messages.append({
                        "role": "user",
                        "content": json.dumps(feedback, ensure_ascii=False),
                    })
                    checkpoint()
                    print("[Plan Evidence Gate] step completion requires evidence")
                    continue
                completed = complete_step(
                    current_plan, current_step_id, accepted_evidence
                )
                current_plan.clear()
                current_plan.update(completed)
                print(f"[Plan] step completed：{current_step_id}")
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            checkpoint()
            print(f"[模型最终答案] {answer}")
            return answer

        if decision.get("type") == "tool_call" and str(
            decision.get("tool", "")
        ).startswith("mcp:"):
            reference = decision.get("tool")
            if current_plan is not None:
                plan_had_action = True
            arguments = decision.get("arguments")
            effect = None
            rejected_final_answer = None
            print(f"[模型请求 MCP capability] {reference}")
            try:
                if mcp_registry is None:
                    raise ValueError("MCP registry 未配置")
                client, name, detail = mcp_registry.resolve(reference)
                validate_json_schema(
                    arguments, detail.get("inputSchema", {"type": "object"})
                )
            except ValueError as error:
                observation = {
                    "result": None, "error": str(error), "exit_code": 1,
                    "denied_by": "capability_validation",
                }
                policy = None
                approved = False
                print(f"[MCP Validation] DENY：{error}")
            else:
                policy = mcp_registry.policy_for(reference)
                effect = mcp_registry.effect_for(reference)
                print(f"[Policy] {policy['action']}：{policy['reason']}")
                print(f"[MCP Effect] {effect}")
                approved = policy["action"] == POLICY_ALLOW
                blocked_by_verification = False
                if requires_verification and effect != MCP_EFFECT_READ_ONLY:
                    observation = {
                        "result": None,
                        "error": "verification tool must be read-only",
                        "exit_code": 126,
                        "denied_by": "verification_gate",
                    }
                    approved = False
                    blocked_by_verification = True
                elif policy["action"] == POLICY_ASK:
                    approved = request_approval(reference, policy["reason"])
                if approved:
                    observation = execute_mcp_tool(
                        mcp_registry, reference, arguments
                    )
                    if observation["exit_code"] == 0:
                        if requires_verification and effect == MCP_EFFECT_READ_ONLY:
                            requires_verification = False
                            verification_target = None
                        elif effect in {
                            MCP_EFFECT_SIDE_EFFECTING, MCP_EFFECT_UNKNOWN,
                        }:
                            requires_verification = True
                            latest_write_command = reference
                            verification_target = None
                    print("[MCP Tool Execution] 调用完毕")
                elif policy["action"] == POLICY_DENY:
                    observation = {
                        "result": None, "error": "tool execution was denied by policy",
                        "exit_code": 126, "denied_by": "policy",
                    }
                elif not blocked_by_verification:
                    observation = {
                        "result": None, "error": "tool execution was denied by user",
                        "exit_code": 126, "denied_by": "user",
                    }
            print(f"[Observation] exit_code={observation['exit_code']}")
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            messages.append({
                "role": "tool",
                "content": json.dumps(observation, ensure_ascii=False),
            })
            if (
                current_plan is not None
                and observation["exit_code"] == 0
                and effect == MCP_EFFECT_READ_ONLY
            ):
                plan_evidence.append({
                    "kind": "tool_observation",
                    "message_index": len(messages) - 1,
                    "summary": f"{reference} read-only observation succeeded",
                    "verified": True,
                })
                plan_runtime_state["requires_fresh_grounding"] = False
            checkpoint()
            continue

        if decision.get("type") != "tool_call" or not decision.get("command"):
            raise ValueError(f"模型返回了无效决定：{decision!r}")

        command = decision["command"]
        if current_plan is not None:
            plan_had_action = True
        rejected_final_answer = None
        print(f"[模型请求执行的命令] {command}")
        policy = classify_shell(command)
        print(f"[Policy] {policy['action']}：{policy['reason']}")

        approved = policy["action"] == POLICY_ALLOW
        if requires_verification and policy["action"] == POLICY_ASK:
            approved = False
            observation = {
                "status": "denied",
                "denied_by": "verification_gate",
                "stdout": "",
                "stderr": "verification tool must be read-only",
                "exit_code": 126,
            }
            print("[Verification Gate] 验证工具必须是只读 ALLOW 命令")
        elif (
            requires_verification
            and policy["action"] == POLICY_ALLOW
            and verification_target is not None
            and not is_related_verification(command, verification_target)
        ):
            approved = False
            observation = {
                "status": "denied",
                "denied_by": "verification_quality",
                "stdout": "",
                "stderr": (
                    "verification evidence is not related to the modified target"
                ),
                "exit_code": 126,
                "verification_target": verification_target,
            }
            print("[Verification Quality] 验证证据与修改目标无关")
        elif policy["action"] == POLICY_ASK:
            approved = request_approval(command, policy["reason"])

        if approved:
            observation = execute_shell(command)
            print("[Tool Execution] 命令执行完毕")
            if observation["exit_code"] == 0:
                if requires_verification and policy["action"] == POLICY_ALLOW:
                    requires_verification = False
                    verification_target = None
                    print("[Verification Gate] 只读验证成功，已解除门禁")
                elif policy["action"] == POLICY_ASK:
                    requires_verification = True
                    latest_write_command = command
                    verification_target = extract_verification_target(command)
                    if verification_target is None:
                        print(
                            "[Verification Quality] 无法可靠识别目标，"
                            "显式降级为 V3 验证行为"
                        )
                    print("[Verification Gate] 写操作成功，需要只读验证")
        elif not (
            requires_verification
            and (
                policy["action"] == POLICY_ASK
                or (
                    policy["action"] == POLICY_ALLOW
                    and verification_target is not None
                    and not is_related_verification(command, verification_target)
                )
            )
        ):
            denied_by = "policy" if policy["action"] == POLICY_DENY else "user"
            observation = {
                "status": "denied",
                "denied_by": denied_by,
                "stdout": "",
                "stderr": f"tool execution was denied by {denied_by}",
                "exit_code": 126,
            }
            print(f"[Tool Execution] 未执行：denied_by={denied_by}")
        print(f"[Observation] exit_code={observation['exit_code']}")
        print(f"[Observation] stdout={observation['stdout'].rstrip()!r}")
        print(f"[Observation] stderr={observation['stderr'].rstrip()!r}")

        # Harness 行为：保存模型决定，并把工具结果作为 observation 发回模型。
        messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        messages.append({"role": "tool", "content": json.dumps(observation, ensure_ascii=False)})
        if (
            current_plan is not None
            and observation["exit_code"] == 0
            and policy["action"] == POLICY_ALLOW
        ):
            plan_evidence.append({
                "kind": "tool_observation",
                "message_index": len(messages) - 1,
                "summary": f"{command} read-only observation succeeded",
                "verified": True,
            })
            plan_runtime_state["requires_fresh_grounding"] = False
        checkpoint()

    raise RuntimeError(f"达到最大步数 {max_steps}，Agent 已停止，以防止无限循环。")

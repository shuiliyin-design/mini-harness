"""Pure transition helpers shared by historical integrity paths."""

from .integrity import canonical_json_bytes, sha256_identity


def historical_evidence_accepted(record, run_id, plan, verification_required):
    if record["run_id"] != run_id:
        return False
    if (record["freshness"].get("scope") != "run"
            or record["freshness"].get("run_id") != run_id):
        return False
    kind = record["evidence_type"]
    verification = record["verification"]
    if record["subject"].get("kind") == "plan_step" and (
        plan is None
        or record["subject"].get("target")
        not in set(plan["completed_step_ids"])
    ):
        return False
    if kind in {"subagent_return", "mcp_observation"}:
        return False
    if kind in {"verification", "tool_observation"}:
        return verification.get("accepted") is True
    if kind == "reconciliation":
        return verification.get("result") in {"applied", "not_applied"}
    if kind == "reasoning_result":
        return bool(
            not verification_required and plan is not None
            and plan["status"] == "completed"
            and record["subject"].get("kind") == "plan_step"
        )
    return False


def evaluate_result_transition(inputs, forbidden_reason_patterns=()):
    """Replay-safe Result transition over already-normalized pure data."""
    candidate = inputs["candidate"]
    eligible_artifacts = {item["artifact_id"] for item in inputs["accepted_artifacts"]}
    eligible_evidence = {item["evidence_id"] for item in inputs["accepted_evidence"]}
    output = inputs["output_contract"]
    artifact_ids = set(output["accepted_artifact_ids"] if output else [])
    evidence_ids = set()
    contradictions = []
    if candidate is not None:
        if set(candidate["artifact_refs"]) - eligible_artifacts:
            contradictions.append("invalid artifact reference")
        if set(candidate["evidence_refs"]) - eligible_evidence:
            contradictions.append("invalid evidence reference")
        artifact_ids.update(set(candidate["artifact_refs"]) & eligible_artifacts)
        evidence_ids.update(set(candidate["evidence_refs"]) & eligible_evidence)
    artifact_ids.update(eligible_artifacts)
    evidence_ids.update(eligible_evidence)
    control, plan = inputs["run_control"], inputs["plan"]
    reason = None
    if control["state"] in {"cancel_requested", "cancelled"}:
        status, reason = "cancelled", control["reason"] or "run cancelled"
    elif inputs["terminal_failure"] is not None or (plan and plan["status"] == "failed"):
        status, reason = "failed", inputs["terminal_failure"] or "plan failed"
    elif inputs["blocking_reason"] is not None or (plan and plan["status"] == "blocked"):
        status, reason = "blocked", inputs["blocking_reason"] or "plan blocked"
    elif plan is not None and plan["status"] != "completed":
        status, reason = "incomplete", "plan not completed"
    elif output is not None and not output["satisfied"]:
        status, reason = "incomplete", "output contract unsatisfied"
    elif inputs["verification_required"] and not inputs["accepted_evidence"]:
        status, reason = "incomplete", "required evidence unsatisfied"
    elif candidate is None:
        status, reason = "incomplete", "final candidate missing"
    else:
        status = "completed"
    if candidate is not None and candidate["claimed_status"] is not None and candidate["claimed_status"] != status:
        contradictions.insert(0, "claimed status mismatch")
    elif candidate is not None and status != "completed":
        contradictions.insert(0, "completion gates unsatisfied")
    if candidate is not None and not candidate["answer_allowed"]:
        contradictions.append(candidate["answer_rejection_reason"])
    contradiction = bool(contradictions)
    if contradiction and reason is None:
        reason = "; ".join(contradictions)
    if reason is not None and any(pattern.search(reason) for pattern in forbidden_reason_patterns):
        reason, contradiction = "result reason removed by secret screening", True
    return {
        "authoritative_status": status,
        "accepted_artifact_ids": sorted(artifact_ids),
        "accepted_evidence_ids": sorted(evidence_ids),
        "reason": reason, "contradiction": contradiction,
    }

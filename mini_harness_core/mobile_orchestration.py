"""Narrow orchestration facts for the Phase 2 battery/notification workflow.

This module evaluates one fixed integer condition and binds its Evidence and
output identities.  It does not classify Policy, ask for Approval, authorize,
or dispatch either Environment capability.
"""

import copy
import json
import os
import re

from .evidence import (
    EvidenceError, create_condition_decision_evidence,
    evidence_integrity_check, validate_evidence,
)
from .integrity import (
    ImmutableRecordConflict, atomic_json_publish, sha256_identity,
)
from .planning import create_plan, validate_plan


WORKFLOW_KIND = "battery_threshold_notification"
BATTERY_STEP_ID = "observe_battery"
NOTIFICATION_STEP_ID = "conditional_notification"
BATTERY_CAPABILITY = "termux:battery_status"
NOTIFICATION_CAPABILITY = "termux:notification"
OUTPUT_SCHEMA_VERSION = 1
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
EVIDENCE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
OUTPUT_FIELDS = frozenset({
    "workflow_output_schema_version", "run_id", "plan_id", "workflow_kind",
    "threshold", "battery_percentage", "battery_evidence_id",
    "condition_evidence_id", "notification_required",
    "notification_request_accepted", "notification_evidence_id", "branch",
    "satisfied", "unsatisfied_requirements", "evidence_ids",
    "output_fingerprint",
})
BRANCHES = frozenset({
    "not_required", "accepted", "approval_denied", "unknown",
})


class MobileWorkflowError(ValueError):
    pass


def validate_mobile_workflow(value):
    if not isinstance(value, dict) or set(value) != {"kind", "threshold"}:
        raise MobileWorkflowError("mobile workflow schema is invalid")
    threshold = value["threshold"]
    if (
        value["kind"] != WORKFLOW_KIND
        or not isinstance(threshold, int) or isinstance(threshold, bool)
        or not 0 <= threshold <= 100
    ):
        raise MobileWorkflowError("mobile workflow threshold is invalid")
    return {"kind": WORKFLOW_KIND, "threshold": threshold}


def create_mobile_workflow_plan(threshold, plan_id=None):
    workflow = validate_mobile_workflow({
        "kind": WORKFLOW_KIND, "threshold": threshold,
    })
    condition = {
        "source_step_id": BATTERY_STEP_ID,
        "evidence_id": None,
        "decision_evidence_id": None,
        "expression": {
            "left": "battery_percentage", "operator": "lt",
            "right": workflow["threshold"],
        },
        "outcome": None,
    }
    return create_plan(
        "检查电量并按阈值决定是否请求通知",
        [
            {"id": BATTERY_STEP_ID, "description": "观察当前电量",
             "depends_on": []},
            {"id": NOTIFICATION_STEP_ID,
             "description": "按确定性条件满足通知义务",
             "depends_on": [BATTERY_STEP_ID], "condition": condition},
        ],
        plan_id=plan_id,
    )


def _accepted_battery(evidence, run_id):
    try:
        validate_evidence(evidence)
    except EvidenceError as error:
        raise MobileWorkflowError(str(error)) from error
    source = evidence["source"]
    freshness = evidence["freshness"]
    safe = evidence["content_identity"].get("safe_observation") or {}
    percentage = safe.get("percentage")
    if (
        evidence["evidence_type"] != "termux_observation"
        or source.get("capability") != BATTERY_CAPABILITY
        or evidence["verification"].get("accepted") is not True
        or evidence["run_id"] != run_id
        or freshness.get("scope") != "run"
        or freshness.get("run_id") != run_id
        or not isinstance(percentage, int) or isinstance(percentage, bool)
        or not 0 <= percentage <= 100
    ):
        raise MobileWorkflowError(
            "condition requires accepted fresh current-run battery Evidence"
        )
    return percentage


def evaluate_battery_condition(evidence, threshold, run_id):
    """Evaluate the sole P2.7 condition with Harness-owned integer logic."""
    validate_mobile_workflow({"kind": WORKFLOW_KIND, "threshold": threshold})
    percentage = _accepted_battery(evidence, run_id)
    return {
        "battery_evidence_id": evidence["evidence_id"],
        "battery_percentage": percentage,
        "operator": "lt", "threshold": threshold,
        "outcome": percentage < threshold,
    }


def create_mobile_condition_evidence(run_id, decision, step_id=NOTIFICATION_STEP_ID):
    if not isinstance(decision, dict) or set(decision) != {
        "battery_evidence_id", "battery_percentage", "operator", "threshold",
        "outcome",
    } or decision["operator"] != "lt":
        raise MobileWorkflowError("condition decision schema is invalid")
    return create_condition_decision_evidence(
        run_id, step_id, decision["battery_evidence_id"],
        decision["battery_percentage"], decision["threshold"],
        decision["outcome"],
    )


def bind_mobile_condition(plan, decision, condition_evidence_id):
    """Bind correlation to the existing Step without granting action authority."""
    validate_plan(plan)
    updated = copy.deepcopy(plan)
    step = next((item for item in updated["steps"]
                 if item["id"] == NOTIFICATION_STEP_ID), None)
    if step is None or "condition" not in step:
        raise MobileWorkflowError("mobile notification step is missing")
    condition = step["condition"]
    if condition["expression"] != {
        "left": "battery_percentage", "operator": "lt",
        "right": decision["threshold"],
    }:
        raise MobileWorkflowError("condition differs from persisted Plan")
    condition.update({
        "evidence_id": decision["battery_evidence_id"],
        "decision_evidence_id": condition_evidence_id,
        "outcome": decision["outcome"],
    })
    validate_plan(updated)
    return updated


def condition_allows_notification(
    plan, evidence_store, run_id, audit_directory=None,
):
    """Recheck both accepted Evidence records immediately before Authority."""
    validate_plan(plan)
    step = next(item for item in plan["steps"]
                if item["id"] == NOTIFICATION_STEP_ID)
    condition = step.get("condition") or {}
    if condition.get("outcome") is not True:
        return False
    try:
        battery = evidence_store.load(condition["evidence_id"])
        decision_record = evidence_store.load(condition["decision_evidence_id"])
        decision = evaluate_battery_condition(
            battery, condition["expression"]["right"], run_id,
        )
    except (EvidenceError, MobileWorkflowError, KeyError, TypeError):
        return False
    content = decision_record.get("content_identity") or {}
    integrity_ok = bool(
        audit_directory is None
        or (
            evidence_integrity_check(
                battery["evidence_id"], evidence_store.directory,
                audit_directory,
            )
            and evidence_integrity_check(
                decision_record["evidence_id"], evidence_store.directory,
                audit_directory,
            )
        )
    )
    return bool(
        integrity_ok
        and decision["outcome"] is True
        and decision_record.get("run_id") == run_id
        and decision_record.get("evidence_type") == "condition_decision"
        and decision_record.get("verification", {}).get("accepted") is True
        and decision_record.get("freshness", {}).get("scope") == "run"
        and decision_record.get("freshness", {}).get("run_id") == run_id
        and decision_record.get("source", {}).get("battery_evidence_id")
        == battery["evidence_id"]
        and content.get("outcome") is True
    )


def find_step_evidence(evidence_store, run_id, capability, step_id):
    """Find one accepted Environment Evidence identity for recovery."""
    try:
        names = sorted(os.listdir(evidence_store.directory))
    except FileNotFoundError:
        return None
    matches = []
    for name in names:
        evidence_id = name[:-5] if name.endswith(".json") else ""
        if not EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
            continue
        record = evidence_store.load(evidence_id)
        if (
            record["run_id"] == run_id
            and record["evidence_type"] == "termux_observation"
            and record["source"].get("capability") == capability
            and record["verification"].get("accepted") is True
            and record["references"].get("step_id") == step_id
        ):
            matches.append(record)
    return matches[-1] if matches else None


def find_condition_evidence(evidence_store, run_id, battery_evidence_id):
    try:
        names = sorted(os.listdir(evidence_store.directory))
    except FileNotFoundError:
        return None
    for name in names:
        evidence_id = name[:-5] if name.endswith(".json") else ""
        if not EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
            continue
        record = evidence_store.load(evidence_id)
        if (
            record["run_id"] == run_id
            and record["evidence_type"] == "condition_decision"
            and record["source"].get("battery_evidence_id")
            == battery_evidence_id
        ):
            return record
    return None


def _output_fingerprint(value):
    stable = {key: item for key, item in value.items()
              if key != "output_fingerprint"}
    return sha256_identity(stable)


def validate_mobile_workflow_output(value, verify_fingerprint=True):
    if not isinstance(value, dict) or set(value) != OUTPUT_FIELDS:
        raise MobileWorkflowError("mobile workflow output schema is invalid")
    if (
        value["workflow_output_schema_version"] != OUTPUT_SCHEMA_VERSION
        or not RUN_ID_PATTERN.fullmatch(str(value["run_id"]))
        or not isinstance(value["plan_id"], str) or not value["plan_id"]
        or value["workflow_kind"] != WORKFLOW_KIND
        or value["branch"] not in BRANCHES
        or not isinstance(value["threshold"], int)
        or isinstance(value["threshold"], bool)
        or not 0 <= value["threshold"] <= 100
        or not isinstance(value["battery_percentage"], int)
        or isinstance(value["battery_percentage"], bool)
        or not 0 <= value["battery_percentage"] <= 100
        or not isinstance(value["notification_required"], bool)
        or value["notification_request_accepted"] is not None
        and not isinstance(value["notification_request_accepted"], bool)
        or not isinstance(value["satisfied"], bool)
        or not isinstance(value["unsatisfied_requirements"], list)
        or not all(isinstance(item, str) and item
                   for item in value["unsatisfied_requirements"])
        or not isinstance(value["evidence_ids"], list)
    ):
        raise MobileWorkflowError("mobile workflow output value is invalid")
    for field in ("battery_evidence_id", "condition_evidence_id"):
        if not EVIDENCE_ID_PATTERN.fullmatch(str(value[field])):
            raise MobileWorkflowError("mobile workflow Evidence identity is invalid")
    notification_id = value["notification_evidence_id"]
    if notification_id is not None and not EVIDENCE_ID_PATTERN.fullmatch(str(notification_id)):
        raise MobileWorkflowError("notification Evidence identity is invalid")
    expected = [value["battery_evidence_id"], value["condition_evidence_id"]]
    if notification_id is not None:
        expected.append(notification_id)
    if value["evidence_ids"] != expected:
        raise MobileWorkflowError("mobile workflow Evidence refs are not exact")
    required = value["battery_percentage"] < value["threshold"]
    branch_rules = {
        "not_required": (False, None, True, [], None),
        "accepted": (True, True, True, [], notification_id),
        "approval_denied": (
            True, None, False, ["notification_not_authorized"], None,
        ),
        "unknown": (
            True, None, False, ["notification_delivery_unknown"], None,
        ),
    }
    rule = branch_rules[value["branch"]]
    if (
        value["notification_required"] is not required
        or (required, value["notification_request_accepted"], value["satisfied"],
            value["unsatisfied_requirements"], notification_id) != rule
    ):
        raise MobileWorkflowError("mobile workflow branch contract mismatch")
    if verify_fingerprint and value["output_fingerprint"] != _output_fingerprint(value):
        raise MobileWorkflowError("mobile workflow output fingerprint mismatch")
    return value


def build_mobile_workflow_output(
    run_id, plan, battery_evidence, condition_evidence, branch,
    notification_evidence=None,
):
    validate_plan(plan)
    condition = evaluate_battery_condition(
        battery_evidence,
        next(item for item in plan["steps"]
             if item["id"] == NOTIFICATION_STEP_ID)["condition"]
        ["expression"]["right"],
        run_id,
    )
    if condition_evidence["source"].get("battery_evidence_id") != battery_evidence["evidence_id"]:
        raise MobileWorkflowError("condition Evidence dependency mismatch")
    notification_id = None
    accepted = None
    if branch == "accepted":
        if (
            notification_evidence is None
            or notification_evidence["run_id"] != run_id
            or notification_evidence["source"].get("capability")
            != NOTIFICATION_CAPABILITY
            or notification_evidence["verification"].get("accepted") is not True
            or notification_evidence["content_identity"].get(
                "safe_observation", {}
            ).get("request_accepted") is not True
        ):
            raise MobileWorkflowError("accepted notification Evidence required")
        notification_id = notification_evidence["evidence_id"]
        accepted = True
    requirements = {
        "not_required": [], "accepted": [],
        "approval_denied": ["notification_not_authorized"],
        "unknown": ["notification_delivery_unknown"],
    }[branch]
    output = {
        "workflow_output_schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": run_id, "plan_id": plan["plan_id"],
        "workflow_kind": WORKFLOW_KIND, "threshold": condition["threshold"],
        "battery_percentage": condition["battery_percentage"],
        "battery_evidence_id": battery_evidence["evidence_id"],
        "condition_evidence_id": condition_evidence["evidence_id"],
        "notification_required": condition["outcome"],
        "notification_request_accepted": accepted,
        "notification_evidence_id": notification_id,
        "branch": branch, "satisfied": branch in {"not_required", "accepted"},
        "unsatisfied_requirements": requirements,
        "evidence_ids": [
            battery_evidence["evidence_id"], condition_evidence["evidence_id"],
        ] + ([notification_id] if notification_id else []),
        "output_fingerprint": "",
    }
    output["output_fingerprint"] = _output_fingerprint(output)
    return validate_mobile_workflow_output(output)


def mobile_output_answer(output):
    validate_mobile_workflow_output(output)
    return json.dumps({
        "battery_percentage": output["battery_percentage"],
        "notification_required": output["notification_required"],
        "notification_request_accepted": output[
            "notification_request_accepted"
        ],
        "unsatisfied_requirements": output["unsatisfied_requirements"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MobileWorkflowOutputStore:
    def __init__(self, directory):
        self.directory = directory

    def _path(self, run_id):
        if not RUN_ID_PATTERN.fullmatch(str(run_id)):
            raise MobileWorkflowError("mobile workflow run_id is invalid")
        return os.path.join(self.directory, run_id + ".json")

    def save(self, output):
        validate_mobile_workflow_output(output)
        try:
            atomic_json_publish(self._path(output["run_id"]), output,
                                temporary_prefix=".workflow-",
                                temporary_suffix=".tmp")
        except ImmutableRecordConflict as error:
            raise MobileWorkflowError("mobile workflow output conflict") from error
        return output

    def load(self, run_id, missing_ok=False):
        try:
            with open(self._path(run_id), encoding="utf-8") as stream:
                return validate_mobile_workflow_output(json.load(stream))
        except FileNotFoundError:
            if missing_ok:
                return None
            raise MobileWorkflowError("mobile workflow output is missing")
        except (OSError, json.JSONDecodeError) as error:
            raise MobileWorkflowError("mobile workflow output is unreadable") from error


def replay_mobile_workflow_output(output, evidence_store):
    """Recompute the output from historical Evidence; performs no dispatch."""
    try:
        validate_mobile_workflow_output(output)
        battery = evidence_store.load(output["battery_evidence_id"])
        condition = evidence_store.load(output["condition_evidence_id"])
        decision = evaluate_battery_condition(
            battery, output["threshold"], output["run_id"],
        )
        if (
            decision["battery_percentage"] != output["battery_percentage"]
            or decision["outcome"] != output["notification_required"]
            or condition["source"].get("battery_evidence_id")
            != battery["evidence_id"]
            or condition["content_identity"].get("outcome")
            != decision["outcome"]
        ):
            return "MISMATCH"
        if output["branch"] == "accepted":
            notification = evidence_store.load(output["notification_evidence_id"])
            if notification["content_identity"].get(
                "safe_observation", {}
            ).get("request_accepted") is not True:
                return "MISMATCH"
        return "MATCH"
    except (EvidenceError, MobileWorkflowError, KeyError, TypeError, OSError):
        return "UNAVAILABLE"

"""Shared Result-binding replay without importing Result or Run Envelope."""

import json
import os

from .artifacts import (
    ArtifactStore, OutputContractStore, artifact_integrity_check,
    current_artifacts,
)
from .evidence import EvidenceStore, evidence_integrity_check
from .historical_types import (
    evaluate_result_transition, historical_evidence_accepted,
)


def replay_result_binding(inputs, audit_directory=None, resolver=None,
                          forbidden_reason_patterns=()):
    if audit_directory is None and resolver is None:
        return "UNAVAILABLE", None
    try:
        artifact_directory = (
            os.path.join(audit_directory, "artifacts") if resolver is None else None
        )
        evidence_directory = (
            os.path.join(audit_directory, "evidence") if resolver is None else None
        )
        run_artifacts = (
            resolver.list("artifact", inputs["run_id"])
            if resolver is not None else
            ArtifactStore(artifact_directory).list_run(inputs["run_id"])
        )
        current_ids = {
            item["artifact_id"] for item in current_artifacts(run_artifacts)
            if item["status"] == "accepted"
        }
        for identity in inputs["accepted_artifacts"]:
            artifact = (
                resolver.load("artifact", identity["artifact_id"])
                if resolver is not None else
                ArtifactStore(artifact_directory).load(identity["artifact_id"])
            )
            if (artifact["run_id"] != inputs["run_id"]
                    or artifact["artifact_id"] not in current_ids
                    or artifact["artifact_fingerprint"] != identity["artifact_fingerprint"]):
                return "UNAVAILABLE", None
            if resolver is None and not artifact_integrity_check(
                artifact["artifact_id"], artifact_directory,
                evidence_directory, audit_directory,
            ):
                return "UNAVAILABLE", None
        for identity in inputs["accepted_evidence"]:
            evidence = (
                resolver.load("evidence", identity["evidence_id"])
                if resolver is not None else
                EvidenceStore(evidence_directory).load(identity["evidence_id"])
            )
            if (evidence["run_id"] != inputs["run_id"]
                    or evidence["evidence_fingerprint"] != identity["evidence_fingerprint"]
                    or not historical_evidence_accepted(
                        evidence, inputs["run_id"], inputs["plan"],
                        inputs["verification_required"],
                    )):
                return "UNAVAILABLE", None
            if resolver is None and not evidence_integrity_check(
                evidence["evidence_id"], evidence_directory, audit_directory,
            ):
                return "UNAVAILABLE", None
        contract_identity = inputs["output_contract"]
        if contract_identity is not None:
            contract = (
                resolver.load("output_contract", inputs["run_id"])
                if resolver is not None else
                OutputContractStore(os.path.join(
                    audit_directory, "output_contracts",
                )).load(inputs["run_id"])
            )
            if contract["contract_fingerprint"] != contract_identity["contract_fingerprint"]:
                return "UNAVAILABLE", None
        output = evaluate_result_transition(inputs, forbidden_reason_patterns)
        return "MATCH", output
    except (KeyError, TypeError, ValueError, OSError):
        return "UNAVAILABLE", None


def load_local_result_transition(run_id, audit_directory):
    """Read only the unique immutable binding needed by Result integrity."""
    path = os.path.join(audit_directory, "envelopes", run_id + ".json")
    with open(path, encoding="utf-8") as stream:
        envelope = json.load(stream)
    if envelope.get("run_id") != run_id or not isinstance(envelope.get("transitions"), list):
        raise ValueError("invalid result envelope view")
    transitions = [
        item for item in envelope["transitions"]
        if isinstance(item, dict) and item.get("transition_type") == "result_binding"
    ]
    if len(transitions) != 1 or not isinstance(transitions[0].get("input"), dict):
        raise ValueError("result binding transition unavailable")
    return transitions[0]

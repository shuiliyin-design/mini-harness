"""Digest generation: app orchestration over existing Harness authority/truth."""

from dataclasses import replace
import copy
import hashlib
import os
import uuid

from mini_harness_core.agent import run_agent
from mini_harness_core.artifacts import (
    ArtifactStore, OutputContractStore, create_artifact, create_producer,
    observe_workspace_file,
)
from mini_harness_core.audit import AuditWriter, safe_observation_summary
from mini_harness_core.dispatch import authorize_action, dispatch_authorized_action
from mini_harness_core.durability import create_action_checkpoint
from mini_harness_core.evidence import (
    EvidenceStore, artifact_ref, create_mcp_observation_evidence,
    create_verification_evidence,
)
from mini_harness_core.integrity import canonical_json_bytes
from mini_harness_core.mcp import (
    MCPRegistry, MCP_EFFECT_READ_ONLY, MCP_EFFECT_SIDE_EFFECTING,
    POLICY_ALLOW, execute_mcp_tool,
)
from mini_harness_core.result import ResultStore

from .adapters.provider import FinalCandidateProvider
from .adapters.search import (
    SEARCH_ERROR_CODES, SearchAdapterError, validate_safe_search_result,
)
from .adapters.workspace import WorkspaceArtifactClient
from .contracts import evaluate_digest_contract
from .domain import (
    ApplicationResult, Digest, DomainError, InterestProfile, SearchObservation,
    normalize_candidates, project_profile, rank_candidates, utc_now,
)
from .repositories import DigestRunRecord


SEARCH_REFERENCE = "mcp:search:web_search"
MATERIALIZE_REFERENCE = "mcp:digest:materialize"
OBSERVE_REFERENCE = "mcp:digest:observe"
SEARCH_RESULT_LIMIT = 10


class DigestGenerationWorkflow:
    """One synchronous, offline vertical slice; no scheduler or delivery."""

    def __init__(self, repository, search_client, provider, workspace,
                 audit_directory, id_factory=None, clock=None):
        self.repository = repository
        self.search_client = search_client
        self.provider = provider
        self.workspace = os.path.realpath(workspace)
        self.audit_directory = os.path.realpath(audit_directory)
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.clock = clock or utc_now
        os.makedirs(self.workspace, exist_ok=True)
        os.makedirs(self.audit_directory, exist_ok=True)

    def _registry(self, artifact_client):
        return MCPRegistry(
            {"search": self.search_client, "digest": artifact_client},
            tool_policies={
                SEARCH_REFERENCE: POLICY_ALLOW,
                MATERIALIZE_REFERENCE: POLICY_ALLOW,
                OBSERVE_REFERENCE: POLICY_ALLOW,
            },
            tool_effects={
                SEARCH_REFERENCE: MCP_EFFECT_READ_ONLY,
                MATERIALIZE_REFERENCE: MCP_EFFECT_SIDE_EFFECTING,
                OBSERVE_REFERENCE: MCP_EFFECT_READ_ONLY,
            },
        )

    def _dispatch(self, registry, writer, reference, arguments):
        registry.resolve(reference)
        policy = registry.policy_for(reference)
        effect = registry.effect_for(reference)
        checkpoint = create_action_checkpoint(reference, arguments, effect)
        writer.append(
            "tool_requested", "harness", reference, "requested",
            references={"action_id": checkpoint["action_id"]},
        )
        writer.append(
            "policy_decision", "harness", reference, policy["action"],
            policy["reason"], references={"action_id": checkpoint["action_id"]},
        )

        def persist(current):
            writer.append(
                "action_state_changed", "harness", reference,
                current["state"], references={
                    "action_id": current["action_id"],
                    "checkpoint_id": current["action_id"],
                },
            )

        action = authorize_action(
            checkpoint=checkpoint, capability=reference, arguments=arguments,
            effect=effect, policy_decision=policy["action"],
            approval_granted=True, run_id=writer.run_id,
            workspace_root=self.workspace,
        )
        outcome = dispatch_authorized_action(
            action, checkpoint, persist_checkpoint=persist,
            executor=lambda normalized: execute_mcp_tool(
                registry, reference, normalized,
            ),
        )
        observation = outcome.raw_observation
        event = writer.append(
            "observation_recorded", "mcp", reference,
            "succeeded" if observation.get("exit_code") == 0 else "failed",
            references={
                "action_id": checkpoint["action_id"],
                "observation": safe_observation_summary(observation),
            },
        )
        return checkpoint, observation, event

    @staticmethod
    def _persist_evidence(store, writer, record, accepted=None):
        store.save(record)
        references = {
            "evidence_id": record["evidence_id"],
            "evidence_fingerprint": record["evidence_fingerprint"],
        }
        writer.append(
            "evidence_created", "harness", "evidence", "created",
            references=references,
        )
        if accepted is not None:
            writer.append(
                "evidence_accepted" if accepted else "evidence_rejected",
                "harness", "evidence",
                "accepted" if accepted else "rejected",
                references=references,
            )

    def _search_and_accept(self, registry, writer, evidence_store,
                           subscription):
        query = " ".join((subscription.topic, *subscription.focus_topics)).strip()
        maximum = min(
            SEARCH_RESULT_LIMIT, max(3, subscription.max_items),
        )
        action, raw, event = self._dispatch(
            registry, writer, SEARCH_REFERENCE,
            {"query": query, "max_results": maximum},
        )
        result = raw.get("result") if raw.get("exit_code") == 0 else None
        safe_result, search_error = None, None
        if result is not None:
            try:
                safe_result = validate_safe_search_result(
                    result, query, maximum,
                )
            except SearchAdapterError:
                search_error = "INVALID_RESPONSE"
        else:
            candidate = str(raw.get("error") or "").split(":", 1)[0]
            search_error = (
                candidate if candidate in SEARCH_ERROR_CODES
                else "NETWORK_ERROR"
            )
        schema_valid = safe_result is not None
        rows = safe_result["results"] if safe_result is not None else []
        provider = (
            safe_result["provider"] if safe_result is not None
            else getattr(self.search_client, "provider", "fake")
        )
        observation = SearchObservation(
            observation_id=event["event_id"], query=query,
            observed_at=self.clock(), results=tuple(copy.deepcopy(rows)),
            provider=provider,
            query_identity=(
                safe_result["query_identity"] if safe_result else None
            ),
            result_count=(safe_result["result_count"] if safe_result else 0),
            request_metadata=(
                safe_result["request_metadata"] if safe_result else
                {"result_limit": maximum}
            ),
            response_metadata=(
                safe_result["response_metadata"] if safe_result else
                {"http_status": None, "response_bytes": 0,
                 "retry_after_seconds": None}
            ),
            observation_identity=(
                safe_result["observation_identity"] if safe_result else None
            ),
        )
        observation_evidence = create_mcp_observation_evidence(
            writer.run_id,
            {"kind": "search_observation", "target": action["action_id"],
             "claim": "external_observation_recorded"},
            "search", SEARCH_REFERENCE, raw, event["event_id"],
            action_id=action["action_id"],
        )
        self._persist_evidence(evidence_store, writer, observation_evidence)
        accepted_id = self.id_factory()
        provisional = normalize_candidates(
            observation, accepted_id,
            (subscription.topic, *subscription.focus_topics),
        )
        candidate_set_identity = hashlib.sha256(canonical_json_bytes([
            {
                "candidate_id": item.candidate_id,
                "source_id": hashlib.sha256(
                    item.canonical_url.encode("utf-8"),
                ).hexdigest(),
                "content_identity": item.content_identity,
                "canonical_url": item.canonical_url,
            } for item in provisional
        ])).hexdigest()
        verification_action_id = self.id_factory()
        writer.append(
            "verification_requested", "harness", "search_candidate_set",
            "requested", references={
                "verification_action_id": verification_action_id,
                "source_action_id": action["action_id"],
                "candidate_evidence_id": observation_evidence["evidence_id"],
            },
        )
        accepted = (
            raw.get("exit_code") == 0 and schema_valid and bool(provisional)
        )
        verification = create_verification_evidence(
            writer.run_id,
            {"kind": "search_candidate_set", "target": candidate_set_identity,
             "claim": "normalized_candidates_accepted"},
            {"target_type": "search_candidate_set",
             "sha256": candidate_set_identity},
            verification_action_id, raw, event["event_id"], accepted,
            None if accepted else "search observation/schema validation failed",
            source_action_id=action["action_id"], references={
                "candidate_evidence_id": observation_evidence["evidence_id"],
                "candidate_set_sha256": candidate_set_identity,
            }, evidence_id=accepted_id,
        )
        self._persist_evidence(evidence_store, writer, verification, accepted)
        return observation, provisional, verification, search_error

    def _materialize_artifact(self, registry, writer, evidence_store,
                              artifact_store, payload, path, requirement,
                              search_evidence_id, digest_run_id,
                              profile_projection):
        encoded = canonical_json_bytes(payload) + b"\n"
        artifact_client = registry.clients["digest"]
        payload_identity = artifact_client.register(encoded)
        materialize, raw_write, write_event = self._dispatch(
            registry, writer, MATERIALIZE_REFERENCE,
            {"path": path, "payload_sha256": payload_identity},
        )
        write_observation = create_mcp_observation_evidence(
            writer.run_id,
            {"kind": "workspace_file", "target": path,
             "claim": "materialization_observed"},
            "digest", MATERIALIZE_REFERENCE, raw_write,
            write_event["event_id"], action_id=materialize["action_id"],
        )
        self._persist_evidence(evidence_store, writer, write_observation)
        observe, raw_read, read_event = self._dispatch(
            registry, writer, OBSERVE_REFERENCE, {"path": path},
        )
        read_observation = create_mcp_observation_evidence(
            writer.run_id,
            {"kind": "workspace_file", "target": path,
             "claim": "current_identity_observed"},
            "digest", OBSERVE_REFERENCE, raw_read,
            read_event["event_id"], action_id=observe["action_id"],
        )
        self._persist_evidence(evidence_store, writer, read_observation)
        identity = observe_workspace_file(path, self.workspace)
        verification = create_verification_evidence(
            writer.run_id,
            {"kind": "workspace_file", "target": path,
             "claim": "content_verified"},
            {"target_type": "file", "path": path},
            observe["action_id"], raw_read, read_event["event_id"],
            raw_read.get("exit_code") == 0, source_action_id=materialize["action_id"],
            references={"candidate_evidence_id": read_observation["evidence_id"]},
            artifact=artifact_ref(path, identity["sha256"], identity["size"]),
        )
        self._persist_evidence(evidence_store, writer, verification, True)
        producer = create_producer(
            writer.run_id, kind="tool_action",
            action_id=materialize["action_id"], capability=MATERIALIZE_REFERENCE,
            server="digest", tool="materialize",
        )
        artifact = create_artifact(
            writer.run_id, path, "accepted", identity, producer,
            evidence_ids=[search_evidence_id, verification["evidence_id"]],
            contract=requirement, references={
                "digest_run_id": digest_run_id,
                "digest_contract_satisfied": True,
                "profile_projection_id": profile_projection.projection_id,
                "profile_version": profile_projection.profile_version,
            },
        )
        artifact_store.save(artifact)
        writer.append(
            "artifact_accepted", "harness", "artifact", "accepted",
            references={
                "artifact_id": artifact["artifact_id"],
                "artifact_fingerprint": artifact["artifact_fingerprint"],
                "path": artifact["path"], "status": artifact["status"],
                "evidence_ids": artifact["evidence_ids"],
            },
        )
        return artifact, verification

    def _finalize(self, writer, evidence_store, artifact_store,
                  contract_store, result_store, requirement, answer,
                  artifact_ids, evidence_ids):
        provider = FinalCandidateProvider(
            answer, artifact_refs=artifact_ids, evidence_refs=evidence_ids,
        )
        previous = os.getcwd()
        os.chdir(self.workspace)
        try:
            return run_agent(
                "generate digest", provider, max_steps=1,
                audit_writer=writer, evidence_store=evidence_store,
                artifact_store=artifact_store,
                output_contract_store=contract_store,
                result_store=result_store,
                output_contract={"required_artifacts": [requirement]},
                return_result=True,
            )
        finally:
            os.chdir(previous)

    def run(self, subscription_id, period_key):
        subscription = self.repository.get_subscription(subscription_id)
        if subscription is None:
            raise DomainError("Subscription 不存在")
        if not subscription.enabled:
            raise DomainError("Subscription 已停用")
        profile = self.repository.get_profile(subscription.user_id)
        if profile is None:
            profile = InterestProfile.empty(
                subscription.user_id, subscription.updated_at,
            )
        profile_projection = project_profile(profile, subscription)
        digest_run_id, harness_run_id = self.id_factory(), self.id_factory()
        reserved = DigestRunRecord(
            digest_run_id, subscription_id, period_key, harness_run_id,
            "reserved", None, None, None, None,
            profile_version=profile_projection.profile_version,
            profile_projection_id=profile_projection.projection_id,
            profile_projection=profile_projection.as_dict(),
        )
        existing, created = self.repository.reserve_digest_run(reserved)
        if not created:
            return ApplicationResult(
                existing.digest_run_id, existing.harness_run_id,
                existing.status, existing.reason, existing.digest_id,
                existing.artifact_id, existing.harness_result or {}, True,
            )
        writer = AuditWriter(
            subscription.user_id, harness_run_id, self.audit_directory,
        )
        evidence_store = EvidenceStore(os.path.join(
            self.audit_directory, "evidence",
        ))
        artifact_store = ArtifactStore(os.path.join(
            self.audit_directory, "artifacts",
        ))
        contract_store = OutputContractStore(os.path.join(
            self.audit_directory, "output_contracts",
        ))
        result_store = ResultStore(os.path.join(
            self.audit_directory, "results",
        ))
        artifact_client = WorkspaceArtifactClient(self.workspace)
        registry = self._registry(artifact_client)
        (_observation, candidates,
         search_evidence, search_error) = self._search_and_accept(
             registry, writer, evidence_store, subscription,
         )
        self.repository.save_candidates(digest_run_id, candidates)
        selected = rank_candidates(
            candidates, subscription, self.clock(), profile_projection,
            self.repository.get_seen_content(subscription.user_id),
        )
        digest_id = self.id_factory()
        path = f"runs/{digest_run_id}/digest.json"
        requirement = {
            "name": "digest", "artifact_type": "workspace_file",
            "path": path,
            "requirements": [
                "exists", "non_empty", "content_identity", "verified",
            ],
        }
        payload, contract = None, None
        artifact, file_evidence = None, None
        if selected:
            payload = self.provider.synthesize(
                subscription, selected, period_key, digest_id,
                profile_projection,
            )
            contract = evaluate_digest_contract(
                payload, subscription,
                selected,
                {search_evidence["evidence_id"]},
                profile_projection,
            )
            if contract.satisfied:
                artifact, file_evidence = self._materialize_artifact(
                    registry, writer, evidence_store, artifact_store,
                    payload, path, requirement,
                    search_evidence["evidence_id"], digest_run_id,
                    profile_projection,
                )
        artifact_ids = [artifact["artifact_id"]] if artifact else []
        evidence_ids = [search_evidence["evidence_id"]]
        if file_evidence:
            evidence_ids.append(file_evidence["evidence_id"])
        answer = (
            payload["rendered_text"] if artifact else
            "没有可用搜索候选" if not selected else
            "Digest candidate 未通过 deterministic Output Contract"
        )
        harness_result = self._finalize(
            writer, evidence_store, artifact_store, contract_store,
            result_store, requirement, answer, artifact_ids, evidence_ids,
        )
        reason = harness_result["reason"]
        if contract is not None and not contract.satisfied:
            reason = ",".join(contract.violations)
        elif not selected:
            reason = (
                "no_results" if search_error == "EMPTY_RESULTS"
                else search_error or "no_results"
            )
        digest = None
        if harness_result["status"] == "completed" and artifact is not None:
            digest = Digest(
                digest_id=digest_id, digest_run_id=digest_run_id,
                harness_run_id=harness_run_id,
                artifact_id=artifact["artifact_id"],
                subscription_id=subscription_id,
                payload=copy.deepcopy(payload), created_at=self.clock(),
            )
        final_record = replace(
            reserved, status=harness_result["status"], reason=reason,
            digest_id=digest.digest_id if digest else None,
            artifact_id=artifact["artifact_id"] if artifact else None,
            harness_result=harness_result,
        )
        self.repository.finish_digest_run(final_record, digest)
        return ApplicationResult(
            digest_run_id, harness_run_id, harness_result["status"], reason,
            digest.digest_id if digest else None,
            artifact["artifact_id"] if artifact else None,
            harness_result, False,
        )

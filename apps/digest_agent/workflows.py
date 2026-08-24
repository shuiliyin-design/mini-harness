"""Digest generation: app orchestration over existing Harness authority/truth."""

from dataclasses import asdict, replace
import copy
import hashlib
import json
import os
import time
import uuid

from mini_harness_core.agent import run_agent
from mini_harness_core.artifacts import (
    ArtifactStore, OutputContractStore, create_artifact, create_producer,
    observe_workspace_file,
)
from mini_harness_core.audit import (
    AuditWriter, read_events, safe_observation_summary,
)
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
from mini_harness_core.result import ResultError, ResultStore

from .adapters.provider import (
    CANDIDATE_SCHEMA_FIELDS, CANDIDATE_SCHEMA_MISMATCH_RULES,
    ENVELOPE_EXTRACTION_ERRORS, GENERATION_DIAGNOSTIC_FIELDS,
    GENERATION_FAILURE_SUBTYPES, JSON_LEXICAL_SUBTYPES, SAFE_JSON_TYPES,
    STRUCTURED_CANDIDATE_SCHEMA_IDENTITY,
    STRUCTURED_RETRY_SUBTYPES,
    FinalCandidateProvider, ProviderAdapterError,
)
from .adapters.search import (
    SEARCH_ERROR_CODES, SearchAdapterError, validate_safe_search_result,
)
from .adapters.workspace import WorkspaceArtifactClient
from .contracts import evaluate_digest_contract
from .domain import (
    ApplicationResult, Digest, DomainError, ID_PATTERN, InterestProfile, SearchObservation,
    ProfileProjection, Subscription, TopicWeight,
    normalize_candidates, project_profile, rank_candidates, utc_now,
)
from .repositories import DigestRunRecord, GenerationAttemptRecord


PROVIDER_FAILURE_CODES = {
    "CONFIGURATION_ERROR": ("configuration", "generation_configuration_error"),
    "AUTH_FAILED": ("configuration", "generation_configuration_error"),
    "TIMEOUT": ("generation", "generation_timeout"),
    "RATE_LIMITED": ("generation", "generation_rate_limited"),
    "NETWORK_ERROR": ("generation", "generation_unavailable"),
    "INVALID_RESPONSE": ("generation", "generation_invalid_response"),
    "MODEL_REFUSAL": ("generation", "generation_refusal"),
    "EMPTY_OUTPUT": ("generation", "generation_empty_output"),
}
PROVIDER_SUBTYPE_CODES = {
    "NON_JSON": ("generation", "generation_non_json"),
    "JSON_PARSE": ("generation", "generation_json_parse"),
}
SEARCH_FAILURE_CODES = {
    "CONFIGURATION_ERROR": ("configuration", "search_configuration_error"),
    "AUTH_FAILED": ("configuration", "search_configuration_error"),
    "TIMEOUT": ("search", "search_timeout"),
    "RATE_LIMITED": ("search", "search_rate_limited"),
    "NETWORK_ERROR": ("search", "search_unavailable"),
    "INVALID_RESPONSE": ("search", "search_invalid_response"),
    "OVERSIZED_RESPONSE": ("search", "search_invalid_response"),
    "EMPTY_RESULTS": ("search", "search_empty_results"),
    "no_results": ("search", "search_empty_results"),
}


SEARCH_REFERENCE = "mcp:search:web_search"
MATERIALIZE_REFERENCE = "mcp:digest:materialize"
OBSERVE_REFERENCE = "mcp:digest:observe"
SEARCH_RESULT_LIMIT = 10


class DigestGenerationWorkflow:
    """One synchronous, offline vertical slice; no scheduler or delivery."""

    def __init__(self, repository, search_client, provider, workspace,
                 audit_directory, id_factory=None, clock=None,
                 generation_max_attempts=2, generation_deadline_seconds=125,
                 monotonic=None, fault_injector=None):
        if generation_max_attempts not in {1, 2}:
            raise ValueError("generation_max_attempts must be 1 or 2")
        if (not isinstance(generation_deadline_seconds, (int, float))
                or isinstance(generation_deadline_seconds, bool)
                or not 1 <= generation_deadline_seconds <= 180):
            raise ValueError("invalid generation deadline")
        self.repository = repository
        self.search_client = search_client
        self.provider = provider
        self.workspace = os.path.realpath(workspace)
        self.audit_directory = os.path.realpath(audit_directory)
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.clock = clock or utc_now
        self.generation_max_attempts = generation_max_attempts
        self.generation_deadline_seconds = float(generation_deadline_seconds)
        self.monotonic = monotonic or time.monotonic
        self.fault_injector = fault_injector
        os.makedirs(self.workspace, exist_ok=True)
        os.makedirs(self.audit_directory, exist_ok=True)

    def _fault(self, stage, value):
        if self.fault_injector is not None:
            self.fault_injector(stage, value)

    @staticmethod
    def _safe_attempt_metadata(value, allowed):
        if not isinstance(value, dict):
            return {}
        return {
            key: item for key, item in value.items()
            if key in allowed and (
                item is None or isinstance(item, (str, int, float, bool))
            )
            and (key != "json_lexical_subtype"
                 or item in JSON_LEXICAL_SUBTYPES)
            and (key != "schema_mismatch_rule"
                 or item in CANDIDATE_SCHEMA_MISMATCH_RULES)
            and (key != "schema_mismatch_field"
                 or item in CANDIDATE_SCHEMA_FIELDS)
            and (key != "envelope_error"
                 or item in ENVELOPE_EXTRACTION_ERRORS)
            and (key not in {
                "message_type", "content_type", "arguments_type",
                "payload_top_type", "payload_summary_type",
                "payload_items_type", "payload_selected_source_refs_type",
                "payload_items_nested_type",
            } or item in SAFE_JSON_TYPES)
            and (key != "payload_source" or item == "tool_arguments")
        }

    def _synthesize_with_attempts(self, reserved, subscription, selected,
                                  digest_id, profile_projection):
        started = self.monotonic()
        last_error = None
        for attempt_number in range(1, self.generation_max_attempts + 1):
            describe = getattr(self.provider, "describe_attempt", None)
            metadata = (
                describe(
                    subscription, selected, reserved.period_key,
                    profile_projection,
                ) if callable(describe) else {
                    "provider_identity": getattr(
                        self.provider, "provider_identity", "fake",
                    ),
                    "candidate_count": len(selected),
                    "schema_identity": STRUCTURED_CANDIDATE_SCHEMA_IDENTITY,
                    "structured_output_mechanism": "deterministic_fake",
                }
            )
            metadata = self._safe_attempt_metadata(metadata, {
                "provider_identity", "model_identity", "api_mode",
                "prompt_chars", "prompt_sha256", "request_sha256",
                "candidate_count", "schema_identity",
                "structured_output_mechanism", "timeout_seconds",
                "max_output_tokens", "temperature",
            })
            attempt_id = hashlib.sha256(
                f"{reserved.digest_run_id}:generation:{attempt_number}".encode(),
            ).hexdigest()[:32]
            attempt = self.repository.reserve_generation_attempt(
                GenerationAttemptRecord(
                    attempt_id, reserved.digest_run_id, attempt_number,
                    "started", metadata, None, None, self.clock(), None,
                ),
            )
            try:
                payload = self.provider.synthesize(
                    subscription, selected, reserved.period_key, digest_id,
                    profile_projection,
                )
            except ProviderAdapterError as error:
                last_error = error
                response = self._safe_attempt_metadata(
                    getattr(self.provider, "last_attempt", None), {
                        "http_status", "response_bytes", "response_sha256",
                        "response_chars", "content_sha256", "finish_reason",
                        "json_parse_succeeded", "schema_validation_succeeded",
                        "duration_ms", "max_output_tokens", "output_tokens",
                        "parse_error_line", "parse_error_column",
                        "starts_with_object", "ends_with_object",
                        "failure_subtype", "json_lexical_subtype",
                        "schema_mismatch_rule", "schema_mismatch_field",
                        "schema_mismatch_item_index", "schema_actual_chars",
                        "schema_expected_min_chars",
                        "schema_expected_max_chars",
                        "schema_control_char_count",
                        "schema_actual_item_count", "schema_actual_ref_count",
                        "choice_count", "message_type", "content_presence",
                        "content_type", "tool_calls_presence",
                        "tool_call_count", "tool_kind_match",
                        "function_name_match", "arguments_presence",
                        "arguments_type", "payload_source", "envelope_error",
                        "payload_top_type", "payload_summary_type",
                        "payload_items_type",
                        "payload_items_string_chars",
                        "payload_items_string_starts_array",
                        "payload_items_string_ends_array",
                        "payload_items_nested_json_parse",
                        "payload_items_nested_type",
                        "payload_selected_source_refs_type",
                    },
                )
                self.repository.finish_generation_attempt(replace(
                    attempt, status="failed", response_metadata=response,
                    failure_subtype=(
                        response.get("failure_subtype")
                        or error.subtype or error.code
                    ),
                    completed_at=self.clock(),
                ))
                retryable = (
                    error.code == "TIMEOUT"
                    or (error.code == "INVALID_RESPONSE"
                        and error.subtype in STRUCTURED_RETRY_SUBTYPES)
                )
                if (not retryable
                        or attempt_number == self.generation_max_attempts
                        or self.monotonic() - started
                        >= self.generation_deadline_seconds):
                    raise
                continue
            response = self._safe_attempt_metadata(
                getattr(self.provider, "last_attempt", None), {
                    "http_status", "response_bytes", "response_sha256",
                    "response_chars", "content_sha256", "finish_reason",
                    "json_parse_succeeded", "schema_validation_succeeded",
                    "duration_ms", "max_output_tokens", "output_tokens",
                    "parse_error_line", "parse_error_column",
                    "starts_with_object", "ends_with_object",
                    "failure_subtype", "json_lexical_subtype",
                    "schema_mismatch_rule", "schema_mismatch_field",
                    "schema_mismatch_item_index", "schema_actual_chars",
                    "schema_expected_min_chars", "schema_expected_max_chars",
                    "schema_control_char_count", "schema_actual_item_count",
                    "schema_actual_ref_count",
                    "choice_count", "message_type", "content_presence",
                    "content_type", "tool_calls_presence", "tool_call_count",
                    "tool_kind_match", "function_name_match",
                    "arguments_presence", "arguments_type", "payload_source",
                    "envelope_error", "payload_top_type",
                    "payload_summary_type", "payload_items_type",
                    "payload_items_string_chars",
                    "payload_items_string_starts_array",
                    "payload_items_string_ends_array",
                    "payload_items_nested_json_parse",
                    "payload_items_nested_type",
                    "payload_selected_source_refs_type",
                },
            )
            self.repository.finish_generation_attempt(replace(
                attempt, status="succeeded", response_metadata=response,
                completed_at=self.clock(),
            ))
            return payload
        raise last_error

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

    def reserve_first_briefing(self, outbox_id, reservation, definition,
                               subscription):
        """Materialize the Slice B application_run_id without external work."""
        if (reservation.subscription_id != subscription.subscription_id
                or reservation.definition_id != definition.definition_id
                or reservation.definition_version
                != definition.definition_version):
            raise DomainError("First Briefing durable refs 不一致")
        profile = self.repository.get_profile(subscription.user_id)
        if profile is None:
            profile = InterestProfile.empty(
                subscription.user_id, subscription.updated_at,
            )
        profile_projection = project_profile(profile, subscription)
        snapshot = asdict(subscription)
        snapshot["focus_topics"] = list(subscription.focus_topics)
        timestamp = self.clock()
        candidate = DigestRunRecord(
            reservation.application_run_id, subscription.subscription_id,
            "first-briefing", self.id_factory(), "reserved", None, None,
            None, None,
            profile_version=profile_projection.profile_version,
            profile_projection_id=profile_projection.projection_id,
            profile_projection=profile_projection.as_dict(),
            idempotency_key=(
                "first-briefing:" + reservation.application_run_id
            ),
            subscription_version=subscription.version,
            subscription_snapshot=snapshot, updated_at=timestamp,
            definition_id=definition.definition_id,
            definition_version=definition.definition_version,
        )
        return self.repository.reserve_first_briefing_run(
            outbox_id, candidate,
        )

    def run(self, subscription_id, period_key, idempotency_key=None):
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
        snapshot = asdict(subscription)
        snapshot["focus_topics"] = list(subscription.focus_topics)
        product_lookup = getattr(
            self.repository, "get_product_subscription", lambda _value: None,
        )
        product = product_lookup(subscription_id)
        timestamp = self.clock()
        reserved = DigestRunRecord(
            digest_run_id, subscription_id, period_key, harness_run_id,
            "reserved", None, None, None, None,
            profile_version=profile_projection.profile_version,
            profile_projection_id=profile_projection.projection_id,
            profile_projection=profile_projection.as_dict(),
            idempotency_key=idempotency_key or period_key,
            subscription_version=subscription.version,
            subscription_snapshot=snapshot, updated_at=timestamp,
            definition_id=(product.definition_id if product else None),
            definition_version=(product.definition_version if product else None),
        )
        existing, created = self.repository.reserve_digest_run(reserved)
        if not created:
            return ApplicationResult(
                existing.digest_run_id, existing.harness_run_id,
                existing.status, existing.reason, existing.digest_id,
                existing.artifact_id, existing.harness_result or {}, True,
            )
        return self.execute_reserved(existing)

    def execute_reserved(self, reserved):
        """Bind one durable application run, then perform external work."""
        if reserved.status == "reserved" and reserved.harness_bound_at is None:
            reserved = self.repository.bind_digest_run(
                reserved.digest_run_id, reserved.harness_run_id, self.clock(),
            )
        elif not (reserved.status in {"running", "running_recovery"}
                  and reserved.harness_bound_at):
            raise DomainError("Application Run 不能开始")
        self._fault("after_harness_binding", reserved)
        snapshot = dict(reserved.subscription_snapshot or {})
        if snapshot:
            snapshot["focus_topics"] = tuple(snapshot["focus_topics"])
            subscription = Subscription(**snapshot)
        else:
            subscription = self.repository.get_subscription(reserved.subscription_id)
        if subscription is None:
            raise DomainError("Subscription 不存在")
        projection = reserved.profile_projection
        if projection is None:
            profile = self.repository.get_profile(subscription.user_id)
            if profile is None:
                profile = InterestProfile.empty(
                    subscription.user_id, subscription.updated_at,
                )
            profile_projection = project_profile(profile, subscription)
        else:
            profile_projection = ProfileProjection(
                projection["profile_version"],
                projection["profile_rule_version"],
                tuple(TopicWeight(item["topic_key"], item["weight"])
                      for item in projection["topic_weights"]),
                projection["projection_id"],
            )
        writer = AuditWriter(
            subscription.user_id, reserved.harness_run_id, self.audit_directory,
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
        self._fault("after_search_evidence", reserved)
        self.repository.save_candidates(reserved.digest_run_id, candidates)
        selected = rank_candidates(
            candidates, subscription, self.clock(), profile_projection,
            self.repository.get_seen_content(subscription.user_id),
        )
        digest_id = self.id_factory()
        path = f"runs/{reserved.digest_run_id}/digest.json"
        requirement = {
            "name": "digest", "artifact_type": "workspace_file",
            "path": path,
            "requirements": [
                "exists", "non_empty", "content_identity", "verified",
            ],
        }
        payload, contract, provider_error = None, None, None
        provider_error_subtype = None
        provider_failure_subtype = None
        provider_failure_diagnostics = None
        artifact, file_evidence = None, None
        if selected:
            try:
                payload = self._synthesize_with_attempts(
                    reserved, subscription, selected, digest_id,
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
                        search_evidence["evidence_id"], reserved.digest_run_id,
                        profile_projection,
                    )
            except ProviderAdapterError as error:
                provider_error = error.code
                provider_error_subtype = error.subtype
                attempt = self._safe_attempt_metadata(
                    getattr(self.provider, "last_attempt", None), {
                        "schema_mismatch_rule", "schema_mismatch_field",
                        "payload_source", "payload_top_type",
                        "payload_items_type", "envelope_error",
                        "json_lexical_subtype",
                        "payload_items_string_chars",
                        "payload_items_string_starts_array",
                        "payload_items_string_ends_array",
                        "payload_items_nested_json_parse",
                        "payload_items_nested_type",
                    },
                )
                if error.subtype == "SCHEMA_MISMATCH":
                    candidate_subtype = attempt.get("schema_mismatch_rule")
                elif error.subtype == "ENVELOPE_EXTRACTION":
                    candidate_subtype = "ENVELOPE_EXTRACTION"
                elif error.subtype == "JSON_PARSE":
                    candidate_subtype = attempt.get("json_lexical_subtype")
                else:
                    candidate_subtype = None
                if candidate_subtype in GENERATION_FAILURE_SUBTYPES:
                    provider_failure_subtype = candidate_subtype
                    provider_failure_diagnostics = {
                        key: value for key, value in attempt.items()
                        if key in GENERATION_DIAGNOSTIC_FIELDS
                    } or None
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
        failure_stage, failure_code = None, None
        failure_subtype, failure_diagnostics = None, None
        if provider_error is not None:
            reason = provider_error
            failure_stage, failure_code = (
                PROVIDER_SUBTYPE_CODES.get(provider_error_subtype)
                or PROVIDER_FAILURE_CODES.get(
                    provider_error,
                    ("generation", "generation_incomplete"),
                )
            )
            failure_subtype = provider_failure_subtype
            failure_diagnostics = provider_failure_diagnostics
        elif contract is not None and not contract.satisfied:
            reason = ",".join(contract.violations)
            failure_stage, failure_code = "contract", "output_contract_failed"
            failure_subtype = contract.failure_subtype
            failure_diagnostics = copy.deepcopy(contract.diagnostics)
        elif not selected:
            reason = (
                "no_results" if search_error == "EMPTY_RESULTS"
                else search_error or "no_results"
            )
            failure_stage, failure_code = SEARCH_FAILURE_CODES.get(
                search_error or "no_results",
                ("search", "search_unavailable"),
            )
        elif harness_result["status"] != "completed":
            failure_stage, failure_code = "generation", "generation_incomplete"
        digest = None
        if harness_result["status"] == "completed" and artifact is not None:
            digest = Digest(
                digest_id=digest_id, digest_run_id=reserved.digest_run_id,
                harness_run_id=reserved.harness_run_id,
                artifact_id=artifact["artifact_id"],
                subscription_id=reserved.subscription_id,
                payload=copy.deepcopy(payload), created_at=self.clock(),
            )
        final_record = replace(
            reserved, status=harness_result["status"], reason=reason,
            digest_id=digest.digest_id if digest else None,
            artifact_id=artifact["artifact_id"] if artifact else None,
            harness_result=harness_result, updated_at=self.clock(),
            failure_stage=failure_stage, failure_code=failure_code,
            failure_subtype=failure_subtype,
            failure_diagnostics=failure_diagnostics,
        )
        self.repository.finish_digest_run(final_record, digest)
        self._fault("after_digest_commit", final_record)
        return ApplicationResult(
            reserved.digest_run_id, reserved.harness_run_id,
            harness_result["status"], reason,
            digest.digest_id if digest else None,
            artifact["artifact_id"] if artifact else None,
            harness_result, False,
            failure_stage, failure_code,
            failure_subtype, failure_diagnostics,
        )

    def recover_projection(self, record):
        """Project an immutable terminal Harness Result without external work."""
        result = ResultStore(os.path.join(
            self.audit_directory, "results",
        )).load(record.harness_run_id)
        digest = None
        artifact_id = None
        if result["status"] == "completed":
            if len(result["artifact_ids"]) != 1:
                raise ResultError("completed Digest Result artifact 无效")
            artifact_id = result["artifact_ids"][0]
            artifact = ArtifactStore(os.path.join(
                self.audit_directory, "artifacts",
            )).load(artifact_id)
            if artifact["run_id"] != record.harness_run_id:
                raise ResultError("Artifact/Harness Run mismatch")
            current = observe_workspace_file(artifact["path"], self.workspace)
            if current != artifact["content_identity"]:
                raise ResultError("Artifact current identity mismatch")
            with open(os.path.join(self.workspace, artifact["path"]),
                      encoding="utf-8") as stream:
                payload = json.load(stream)
            if (payload.get("subscription_id") != record.subscription_id
                    or payload.get("subscription_version")
                    != record.subscription_version
                    or not ID_PATTERN.fullmatch(str(payload.get("digest_id", "")))):
                raise ResultError("Digest projection binding mismatch")
            digest = Digest(
                digest_id=payload["digest_id"],
                digest_run_id=record.digest_run_id,
                harness_run_id=record.harness_run_id,
                artifact_id=artifact_id,
                subscription_id=record.subscription_id,
                payload=copy.deepcopy(payload), created_at=self.clock(),
            )
        final = replace(
            record, status=result["status"], reason=result["reason"],
            digest_id=digest.digest_id if digest else None,
            artifact_id=artifact_id, harness_result=result,
            updated_at=self.clock(), failure_stage=None, failure_code=None,
            failure_subtype=None, failure_diagnostics=None,
        )
        self.repository.finish_digest_run(final, digest)
        return ApplicationResult(
            final.digest_run_id, final.harness_run_id, final.status,
            final.reason, final.digest_id, final.artifact_id, result, True,
        )

    def recover_application_run(self, record):
        """Defer recovery to durable Harness truth at the integration seam."""
        try:
            return self.recover_projection(record)
        except ResultError:
            events = read_events(
                record.harness_run_id, self.audit_directory, missing_ok=True,
            )
            if not events:
                claimed = self.repository.claim_bound_digest_run_recovery(
                    record.digest_run_id, self.clock(),
                )
                return self.execute_reserved(claimed)
            marked = self.repository.mark_digest_run_recovery_required(
                record.digest_run_id, "recovery_required", self.clock(),
            )
            return ApplicationResult(
                marked.digest_run_id, marked.harness_run_id, marked.status,
                marked.reason, None, None, {}, True,
            )

    def inspect_recovery_facts(self, record):
        """Return bounded durable facts, never raw Harness records."""
        events = read_events(
            record.harness_run_id, self.audit_directory, missing_ok=True,
        )
        result_path = os.path.join(
            self.audit_directory, "results", record.harness_run_id + ".json",
        )
        result = None
        try:
            result = ResultStore(os.path.dirname(result_path)).load(
                record.harness_run_id,
            )
        except ResultError:
            pass
        binding = "bound" if record.harness_bound_at else "unbound"
        if result is not None:
            projected = (
                record.status == result["status"]
                and (result["status"] != "completed"
                     or record.digest_id is not None)
            )
            return {
                "binding_status": binding,
                "harness_run_status": result["status"],
                "terminal_result_available": True,
                "effect_certainty": "authoritative_terminal",
                "safe_recovery_actions": (() if projected else
                                          ("repair_projection",)),
                "blocking_reason": None if not projected else "already_projected",
            }
        if os.path.exists(result_path):
            return {
                "binding_status": binding,
                "harness_run_status": "invalid_terminal_record",
                "terminal_result_available": False,
                "effect_certainty": "unknown",
                "safe_recovery_actions": (),
                "blocking_reason": "NO_SAFE_AUTOMATIC_RECOVERY",
            }
        if not record.harness_bound_at and not events:
            return {
                "binding_status": "unbound",
                "harness_run_status": "not_started",
                "terminal_result_available": False,
                "effect_certainty": "not_started",
                "safe_recovery_actions": ("resume_original_run",),
                "blocking_reason": None,
            }
        if record.harness_bound_at and not events:
            return {
                "binding_status": "bound",
                "harness_run_status": "bound_not_started",
                "terminal_result_available": False,
                "effect_certainty": "not_started",
                "safe_recovery_actions": ("resume_bound_run",),
                "blocking_reason": None,
            }
        return {
            "binding_status": binding,
            "harness_run_status": "started_nonterminal",
            "terminal_result_available": False,
            "effect_certainty": "unknown",
            "safe_recovery_actions": (),
            "blocking_reason": "NO_SAFE_AUTOMATIC_RECOVERY",
        }

    def resume_bound_run(self, record):
        facts = self.inspect_recovery_facts(record)
        if facts["safe_recovery_actions"] != ("resume_bound_run",):
            raise ValueError("bound run is not safely resumable")
        claimed = self.repository.claim_bound_digest_run_recovery(
            record.digest_run_id, self.clock(),
        )
        return self.execute_reserved(claimed)

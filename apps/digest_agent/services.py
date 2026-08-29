"""Ordinary application services; CRUD/delivery never enter an Agent Run."""

from dataclasses import replace
import hashlib
import os
import re
import uuid

from mini_harness_core.evidence import (
    EvidenceError, EvidenceStore, create_evidence,
)

from .domain import (
    DeliveryOutcome, DeliveryRecord, DeliveryRequest, DomainError, Feedback,
    Subscription, delivery_attempt_identity, delivery_identity,
    distribution_notification_identity, normalize_topic,
    safe_condition_update_preview, safe_digest_preview, utc_now,
)


def _subscription_owned(repository, subscription, user_id):
    if subscription is None:
        return False
    checker = getattr(repository, "subscription_belongs_to_user", None)
    return (checker(subscription.subscription_id, user_id)
            if checker is not None else subscription.user_id == user_id)


class SubscriptionService:
    def __init__(self, repository, id_factory=None, clock=None):
        self.repository = repository
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.clock = clock or utc_now

    @staticmethod
    def parse_request(request):
        if not isinstance(request, str) or not request.strip():
            raise ValueError("natural-language subscription 不能为空")
        text = request.strip()
        topic_match = re.search(
            r"订阅\s*(.+?)(?=[，,。；;]|每天|每日|\d+\s*字|重点关注|$)", text,
        )
        topic = topic_match.group(1).strip() if topic_match else text[:120].strip()
        chars = re.search(r"(\d+)\s*字以内", text)
        items = re.search(r"(?:最多|不超过)\s*(\d+)\s*(?:条|项|篇)", text)
        focus_match = re.search(r"重点关注\s*(.+?)(?:[。；;]|$)", text)
        focus = []
        if focus_match:
            focus = [
                item.strip() for item in re.split(
                    r"[、,，]|和|及", focus_match.group(1),
                ) if item.strip()
            ]
        return {
            "topic": topic, "cadence": "daily",
            "language": "zh-CN",
            "max_chars": int(chars.group(1)) if chars else 600,
            "max_items": int(items.group(1)) if items else 5,
            "focus_topics": tuple(dict.fromkeys(focus)),
            "delivery_channel": "none", "enabled": True,
        }

    def create_from_natural_language(self, user_id, request):
        candidate = self.parse_request(request)
        timestamp = self.clock()
        subscription = Subscription(
            subscription_id=self.id_factory(), user_id=user_id,
            natural_language_request=request, version=1,
            created_at=timestamp, updated_at=timestamp, **candidate,
        )
        self.repository.save_subscription(subscription)
        return subscription

    def get(self, subscription_id):
        return self.repository.get_subscription(subscription_id)

    def _owned(self, subscription, user_id):
        return _subscription_owned(self.repository, subscription, user_id)

    def list(self):
        return self.repository.list_subscriptions()

    def update(self, user_id, subscription_id, expected_version, **changes):
        current = self.repository.get_subscription(subscription_id)
        if not self._owned(current, user_id):
            raise DomainError("Subscription 不存在")
        if current.version != expected_version:
            raise DomainError("Subscription version conflict")
        product_lookup = getattr(
            self.repository, "get_product_subscription", lambda _value: None,
        )
        if product_lookup(subscription_id) is not None:
            raise DomainError("Product Subscription definition update requires new version")
        allowed = {
            "topic", "natural_language_request", "cadence", "language",
            "max_chars", "max_items", "focus_topics", "delivery_channel",
        }
        if not changes or set(changes) - allowed:
            raise DomainError("Subscription update fields 无效")
        if "focus_topics" in changes:
            changes["focus_topics"] = tuple(changes["focus_topics"])
        timestamp = self.clock()
        updated = replace(
            current, **changes, version=current.version + 1,
            updated_at=timestamp,
        )
        if not self.repository.update_subscription(updated, current.version):
            raise DomainError("Subscription version conflict")
        return updated

    def set_enabled(self, user_id, subscription_id, enabled, expected_version):
        if not isinstance(enabled, bool):
            raise DomainError("enabled 必须是 boolean")
        current = self.repository.get_subscription(subscription_id)
        if not self._owned(current, user_id):
            raise DomainError("Subscription 不存在")
        if current.version != expected_version:
            raise DomainError("Subscription version conflict")
        if current.enabled == enabled:
            return current
        timestamp = self.clock()
        updated = replace(
            current, enabled=enabled, version=current.version + 1,
            updated_at=timestamp,
        )
        if not self.repository.update_subscription(updated, current.version):
            raise DomainError("Subscription version conflict")
        return updated


class FeedbackService:
    """Validate feedback ownership, then delegate one atomic application commit."""

    def __init__(self, repository, clock=None):
        self.repository = repository
        self.clock = clock or utc_now

    def record(self, user_id, digest_id, feedback_type, event_key, item_id=None):
        feedback = Feedback(
            user_id=user_id, digest_id=digest_id, item_id=item_id,
            feedback_type=feedback_type, event_key=event_key,
        )
        digest = self.repository.get_digest(digest_id)
        if digest is None:
            raise DomainError("Digest 不存在")
        subscription = self.repository.get_subscription(digest.subscription_id)
        if not _subscription_owned(self.repository, subscription, user_id):
            raise DomainError("Digest 不属于当前 user")
        items = digest.payload.get("items", [])
        if item_id is None:
            if feedback_type != "opened":
                raise DomainError("item feedback 必须绑定 item")
            topic_keys = {
                normalize_topic(topic)
                for item in items for topic in item.get("topic_tags", [])
            }
        else:
            selected = next(
                (item for item in items if item.get("item_id") == item_id), None,
            )
            if selected is None:
                raise DomainError("Digest item 不存在")
            topic_keys = {
                normalize_topic(topic) for topic in selected.get("topic_tags", [])
            }
        if not topic_keys:
            raise DomainError("feedback item 缺少可更新 topic")
        return self.repository.apply_feedback(
            feedback, tuple(sorted(topic_keys)), self.clock(),
        )


class DeliveryPersistenceError(RuntimeError):
    """Dispatch may have happened, but terminal application state was not saved."""


class DeliveryService:
    """Durable application-side delivery with conservative effect certainty."""

    def __init__(self, repository, adapters, clock=None, evidence_store=None):
        self.repository = repository
        self.adapters = {
            adapter.channel: adapter for adapter in adapters
        }
        self.clock = clock or utc_now
        self.evidence_store = evidence_store

    def _adapter(self, channel):
        try:
            return self.adapters[channel]
        except KeyError as error:
            raise DomainError("delivery channel 没有 adapter") from error

    def _request(self, record):
        if record.digest_id is not None:
            digest = self.repository.get_digest(record.digest_id)
            if digest is None:
                raise DomainError("Delivery 对应 Digest 不存在")
            return DeliveryRequest(
                record.delivery_id, record.attempt_id, digest.digest_id,
                record.channel, "AI Digest", safe_digest_preview(digest),
            )
        distribution = self.repository.get_update_distribution(
            record.distribution_id,
        )
        update = (
            self.repository.get_tracking_update(distribution.update_id)
            if distribution is not None else None
        )
        if distribution is None or update is None:
            raise DomainError("Delivery 对应 Distribution/Update 不存在")
        return DeliveryRequest(
            record.delivery_id, record.attempt_id, None, record.channel,
            update.payload["title"], safe_condition_update_preview(update),
            distribution.distribution_id,
        )

    def _notification_evidence(self, record, outcome, timestamp):
        if self.evidence_store is None:
            raise EvidenceError("notification Evidence store unavailable")
        evidence_id = hashlib.sha256(
            f"notification-evidence\n{record.attempt_id}".encode("utf-8"),
        ).hexdigest()[:32]
        evidence = create_evidence(
            record.delivery_id, "termux_observation",
            {
                "kind": "capability", "target": "termux:notification",
                "claim": "notification_request_accepted",
            },
            source={
                "capability": "termux:notification",
                "logical_notification_id": record.delivery_id,
                "attempt_id": record.attempt_id,
            },
            verification={
                "accepted": True, "read_only": False,
                "claim_scope": "request_submission",
                "effect_certainty": outcome.effect_certainty,
            },
            freshness={
                "scope": "run", "observed_at": timestamp,
                "run_id": record.delivery_id,
            },
            content_identity={
                "safe_observation": outcome.safe_observation,
            },
            references={
                "distribution_id": record.distribution_id,
                "delivery_id": record.delivery_id,
            },
            evidence_id=evidence_id, created_at=timestamp,
        )
        self.evidence_store.save(evidence)
        return evidence_id

    def _dispatch(self, record):
        try:
            started = self.repository.mark_delivery_dispatch_started(
                record.delivery_id, record.attempt_id,
            )
        except ValueError:
            current = self.repository.get_delivery(record.delivery_id)
            if current is None:
                raise
            return current
        try:
            outcome = self._adapter(record.channel).dispatch(
                self._request(started),
            )
            if not isinstance(outcome, DeliveryOutcome):
                raise TypeError("delivery adapter returned invalid outcome")
        except Exception:
            outcome = DeliveryOutcome(
                "unknown", "unknown", error_code="ADAPTER_EXCEPTION",
            )
        completed_at = self.clock()
        evidence_id = None
        if outcome.status == "accepted" and started.distribution_id is not None:
            try:
                evidence_id = self._notification_evidence(
                    started, outcome, completed_at,
                )
            except (EvidenceError, OSError):
                terminal = replace(
                    started, status="unknown", completed_at=completed_at,
                    error_code="EVIDENCE_PERSIST_FAILED",
                    effect_certainty="unknown",
                )
                try:
                    self.repository.finish_delivery(terminal)
                except Exception:
                    pass
                raise DeliveryPersistenceError(
                    "notification accepted but Evidence persistence failed"
                )
        terminal = replace(
            started, status=outcome.status,
            provider_message_id=outcome.provider_message_id,
            completed_at=completed_at, error_code=outcome.error_code,
            effect_certainty=outcome.effect_certainty,
            evidence_id=evidence_id,
        )
        try:
            return self.repository.finish_delivery(terminal)
        except Exception as error:
            raise DeliveryPersistenceError(
                "delivery terminal persistence failed; effect remains unknown"
            ) from error

    def deliver_digest(self, user_id, digest_id, channel):
        adapter = self._adapter(channel)
        digest = self.repository.get_digest(digest_id)
        if digest is None:
            raise DomainError("Digest 不存在")
        subscription = self.repository.get_subscription(digest.subscription_id)
        run = self.repository.get_digest_run(digest.digest_run_id)
        if (not _subscription_owned(self.repository, subscription, user_id)
                or run is None or run.status != "completed"
                or run.digest_id != digest.digest_id):
            raise DomainError("Digest 未 completed 或不属于当前 user")
        delivery_id = delivery_identity(digest_id, channel)
        requested_at = self.clock()
        pending = DeliveryRecord(
            delivery_id=delivery_id,
            attempt_id=delivery_attempt_identity(delivery_id, 1),
            digest_id=digest_id, user_id=user_id, channel=adapter.channel,
            status="pending", attempt_number=1,
            provider_message_id=None, requested_at=requested_at,
            completed_at=None, error_code=None,
            effect_certainty="not_started",
        )
        existing, created = self.repository.reserve_delivery(pending)
        if not created and existing.status != "pending":
            return existing
        return self._dispatch(existing)

    def deliver_distribution(self, user_id, distribution_id):
        distribution = self.repository.get_update_distribution(distribution_id)
        update = (
            self.repository.get_tracking_update(distribution.update_id)
            if distribution is not None else None
        )
        relation = (
            self.repository.get_user_subscription_for_subscription(
                update.subscription_id,
            ) if update is not None else None
        )
        if (distribution is None or update is None or relation is None
                or distribution.status != "AVAILABLE"
                or distribution.user_subscription_id
                != relation.user_subscription_id
                or relation.user_id != user_id):
            raise DomainError("Distribution notification binding 无效")
        channel = "termux_notification"
        self._adapter(channel)
        delivery_id = distribution_notification_identity(
            distribution_id, channel,
        )
        requested_at = self.clock()
        pending = DeliveryRecord(
            delivery_id=delivery_id,
            attempt_id=delivery_attempt_identity(delivery_id, 1),
            digest_id=None, user_id=user_id, channel=channel,
            status="pending", attempt_number=1,
            provider_message_id=None, requested_at=requested_at,
            completed_at=None, error_code=None,
            effect_certainty="not_started",
            distribution_id=distribution_id,
        )
        existing, created = self.repository.reserve_delivery(pending)
        if not created and existing.status != "pending":
            return existing
        return self._dispatch(existing)

    def retry_delivery(self, delivery_id):
        previous = self.repository.get_delivery(delivery_id)
        if previous is None:
            raise DomainError("Delivery 不存在")
        if not (previous.status == "failed"
                and previous.effect_certainty == "not_started"):
            raise DomainError("只有 failed/not_started delivery 可显式重试")
        if (previous.distribution_id is not None
                and previous.attempt_number >= 2):
            raise DomainError("Distribution notification retry 已达上限")
        attempt_number = previous.attempt_number + 1
        pending = DeliveryRecord(
            delivery_id=previous.delivery_id,
            attempt_id=delivery_attempt_identity(
                previous.delivery_id, attempt_number,
            ),
            digest_id=previous.digest_id, user_id=previous.user_id,
            channel=previous.channel, status="pending",
            attempt_number=attempt_number, provider_message_id=None,
            requested_at=self.clock(), completed_at=None, error_code=None,
            effect_certainty="not_started",
            distribution_id=previous.distribution_id,
        )
        reserved = self.repository.reserve_delivery_retry(previous, pending)
        return self._dispatch(reserved)

    def get_delivery(self, delivery_id):
        return self.repository.get_delivery(delivery_id)


def build_delivery_service(repository, adapters, audit_path, **kwargs):
    """Keep Evidence-store composition outside bootstrap/transport layers."""
    return DeliveryService(
        repository, adapters,
        evidence_store=EvidenceStore(os.path.join(
            audit_path, "notification-evidence",
        )),
        **kwargs,
    )

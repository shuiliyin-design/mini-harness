"""Ordinary application services; CRUD/delivery never enter an Agent Run."""

from dataclasses import replace
import re
import uuid

from .domain import (
    DeliveryOutcome, DeliveryRecord, DeliveryRequest, DomainError, Feedback,
    Subscription, delivery_attempt_identity, delivery_identity,
    normalize_topic, safe_digest_preview, utc_now,
)


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

    def list(self):
        return self.repository.list_subscriptions()


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
        if subscription is None or subscription.user_id != user_id:
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

    def __init__(self, repository, adapters, clock=None):
        self.repository = repository
        self.adapters = {
            adapter.channel: adapter for adapter in adapters
        }
        self.clock = clock or utc_now

    def _adapter(self, channel):
        try:
            return self.adapters[channel]
        except KeyError as error:
            raise DomainError("delivery channel 没有 adapter") from error

    @staticmethod
    def _request(record, digest):
        return DeliveryRequest(
            record.delivery_id, record.attempt_id, digest.digest_id,
            record.channel, "AI Digest", safe_digest_preview(digest),
        )

    def _dispatch(self, record, digest):
        started = self.repository.mark_delivery_dispatch_started(
            record.delivery_id, record.attempt_id,
        )
        try:
            outcome = self._adapter(record.channel).dispatch(
                self._request(started, digest),
            )
            if not isinstance(outcome, DeliveryOutcome):
                raise TypeError("delivery adapter returned invalid outcome")
        except Exception:
            outcome = DeliveryOutcome(
                "unknown", "unknown", error_code="ADAPTER_EXCEPTION",
            )
        terminal = replace(
            started, status=outcome.status,
            provider_message_id=outcome.provider_message_id,
            completed_at=self.clock(), error_code=outcome.error_code,
            effect_certainty=outcome.effect_certainty,
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
        if (subscription is None or subscription.user_id != user_id
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
        if not created:
            return existing
        return self._dispatch(existing, digest)

    def retry_delivery(self, delivery_id):
        previous = self.repository.get_delivery(delivery_id)
        if previous is None:
            raise DomainError("Delivery 不存在")
        if not (previous.status == "failed"
                and previous.effect_certainty == "not_started"):
            raise DomainError("只有 failed/not_started delivery 可显式重试")
        digest = self.repository.get_digest(previous.digest_id)
        if digest is None:
            raise DomainError("Delivery 对应 Digest 不存在")
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
        )
        reserved = self.repository.reserve_delivery_retry(previous, pending)
        return self._dispatch(reserved, digest)

    def get_delivery(self, delivery_id):
        return self.repository.get_delivery(delivery_id)

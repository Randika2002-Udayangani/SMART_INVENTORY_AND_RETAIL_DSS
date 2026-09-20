"""Database-backed, multi-worker-safe quota protection for chatbot requests."""

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from orders.models import ChatbotRateLimit, Customer


def _actor_key(user):
    prefix = "customer" if isinstance(user, Customer) else "staff"
    return f"{prefix}:{user.pk}"


def consume_chatbot_request(user):
    """Consume one request if within the configured fixed time window.

    Returns (allowed, retry_after_seconds). Row locking means this remains
    correct when multiple Django workers receive requests for the same user.
    """
    limit = max(1, int(settings.CHATBOT_RATE_LIMIT))
    window_seconds = max(1, int(settings.CHATBOT_RATE_WINDOW_SECONDS))
    now = timezone.now()
    actor_key = _actor_key(user)

    with transaction.atomic():
        try:
            counter = ChatbotRateLimit.objects.select_for_update().get(actor_key=actor_key)
        except ChatbotRateLimit.DoesNotExist:
            try:
                with transaction.atomic():
                    counter = ChatbotRateLimit.objects.create(
                        actor_key=actor_key, window_started_at=now, request_count=0
                    )
            except IntegrityError:
                # Another worker created the counter first; lock that row.
                counter = ChatbotRateLimit.objects.select_for_update().get(actor_key=actor_key)

        elapsed = now - counter.window_started_at
        if elapsed >= timedelta(seconds=window_seconds):
            counter.window_started_at = now
            counter.request_count = 0

        if counter.request_count >= limit:
            retry_after = max(
                1, window_seconds - int((now - counter.window_started_at).total_seconds())
            )
            return False, retry_after

        counter.request_count += 1
        counter.save(update_fields=["window_started_at", "request_count"])
        return True, 0


import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone

from .models import PasswordResetToken

TOKEN_LIFETIME_MINUTES = 30
MAX_REQUESTS_PER_HOUR = 3


def hash_token(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def create_reset_token(customer):
    """Return a raw token, or None if the customer asked too many times this hour."""
    now = timezone.now()
    recent = PasswordResetToken.objects.filter(
        customer=customer, created_at__gte=now - timedelta(hours=1)
    ).count()
    if recent >= MAX_REQUESTS_PER_HOUR:
        return None
    # newest request cancels older unused tokens
    PasswordResetToken.objects.filter(
        customer=customer, used_at__isnull=True, expires_at__gt=now
    ).update(used_at=now)
    raw = secrets.token_urlsafe(32)
    PasswordResetToken.objects.create(
        customer=customer,
        token_hash=hash_token(raw),
        expires_at=now + timedelta(minutes=TOKEN_LIFETIME_MINUTES),
    )
    return raw


def find_valid_token(raw):
    if not raw:
        return None
    return (
        PasswordResetToken.objects.select_related('customer')
        .filter(
            token_hash=hash_token(raw),
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
            customer__is_active=True,
        )
        .first()
    )

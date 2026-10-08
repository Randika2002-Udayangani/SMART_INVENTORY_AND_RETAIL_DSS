from django.conf import settings
from django.db import models


# ─────────────────────────────────────────────────────────────────
# Role, AppUser, UserSession — REMOVED.
#
# These were the original custom auth models from early in the
# project. Confirmed dead: nothing in the real login flow
# (TokenObtainPairView / SimpleJWT against settings.AUTH_USER_MODEL)
# ever read or wrote to them. Every FK that used to point at AppUser
# (inventory/models.py x7, orders/models.py x2) has been repointed
# to settings.AUTH_USER_MODEL. See inventory/migrations/0005_*.py,
# orders/migrations/0003_*.py, and this app's 0004_*.py for the
# actual schema changes (applied in that order — the FKs move off
# AppUser before AppUser itself is dropped).
# ─────────────────────────────────────────────────────────────────


class AuditLog(models.Model):
    # Points to the real auth table (where staff actually log in from),
    # not the orphaned AppUser table.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='user_id'
    )
    action = models.CharField(max_length=100)
    table_name = models.CharField(max_length=100)
    record_id = models.IntegerField(null=True, blank=True)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    ip_address = models.CharField(max_length=45, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'audit_log'


class SystemConfig(models.Model):
    key = models.CharField(max_length=100, unique=True)
    value = models.CharField(max_length=255)
    description = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_config'

    def __str__(self):
        return f"{self.key} = {self.value}"

# ─────────────────────────────────────────────────────────────────
# F15 — Account lockout (API Design Doc v3.1 §4.1)
#
# Built on settings.AUTH_USER_MODEL — the real auth user that
# /api/auth/login/ actually checks.
#
# get_or_create() is used everywhere this is read, not a signal —
# so existing accounts (including the superuser) get a row lazily on
# their next login attempt, with safe defaults. No migration-time
# risk to any existing account.
# ─────────────────────────────────────────────────────────────────
class UserLoginSecurity(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='login_security'
    )
    failed_login_count = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'user_login_security'

    def __str__(self):
        return f"{self.user.username} security"
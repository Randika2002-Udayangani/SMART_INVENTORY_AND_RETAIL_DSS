from django.db import models

# Create your models here.


class Role(models.Model):
    ROLE_NAMES = [
        ('ADMIN', 'Admin'),
        ('MANAGER', 'Manager'),
        ('STAFF', 'Staff'),
    ]
    role_name = models.CharField(max_length=20, choices=ROLE_NAMES, unique=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'role'

    def __str__(self):
        return self.role_name


class AppUser(models.Model):
    username = models.CharField(max_length=50, unique=True)
    password_hash = models.TextField()
    role = models.ForeignKey(
        Role, on_delete=models.PROTECT, db_column='role_id'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    failed_login_count = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'app_user'

    def __str__(self):
        return self.username


class UserSession(models.Model):
    user = models.ForeignKey(
        AppUser, on_delete=models.CASCADE, db_column='user_id'
    )
    session_token = models.CharField(max_length=255, unique=True)
    login_time = models.DateTimeField(auto_now_add=True)
    logout_time = models.DateTimeField(null=True, blank=True)
    last_activity = models.DateTimeField(auto_now=True)
    ip_address = models.CharField(max_length=45, blank=True)
    is_active = models.BooleanField(default=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'user_session'


class AuditLog(models.Model):
    user = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
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
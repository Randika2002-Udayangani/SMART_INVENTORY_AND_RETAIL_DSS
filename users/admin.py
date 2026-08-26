from django.contrib import admin
from .models import AuditLog, SystemConfig

admin.site.register(AuditLog)
admin.site.register(SystemConfig)
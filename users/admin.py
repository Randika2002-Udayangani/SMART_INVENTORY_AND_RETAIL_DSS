from django.contrib import admin
from .models import Role, AppUser, UserSession, AuditLog, SystemConfig

admin.site.register(Role)
admin.site.register(AppUser)
admin.site.register(UserSession)
admin.site.register(AuditLog)
admin.site.register(SystemConfig)
# ============================================================
# REPLACE users/urls.py with this — adds 2 new lines for Audit Log.
# ============================================================

from django.urls import path
from . import views


urlpatterns = [
    path('users/', views.RegisterView.as_view(), name='user-register'),
    path('users/<int:pk>/', views.UserDetailView.as_view(), name='user-detail'),
    path('auth/me/', views.MeView.as_view(), name='auth-me'),
    path('auth/logout/', views.LogoutView.as_view(), name='auth-logout'),
    path('auth/change-password/', views.ChangePasswordView.as_view(), name='auth-change-password'),
    path('config/', views.SystemConfigListView.as_view(), name='config-list'),
    path('config/<str:key>/', views.SystemConfigDetailView.as_view(), name='config-detail'),
    path('audit-log/', views.AuditLogListView.as_view(), name='audit-log-list'),
    path('audit-log/<int:pk>/', views.AuditLogDetailView.as_view(), name='audit-log-detail'),
    path('users/<int:pk>/unlock/', views.UnlockUserView.as_view(), name='user-unlock'),
]
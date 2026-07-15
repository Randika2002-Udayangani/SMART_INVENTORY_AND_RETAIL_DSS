# ============================================================
# REPLACE users/urls.py with this — adds 4 new paths to the
# existing 'users/' registration path.
# ============================================================

from django.urls import path
from . import views

urlpatterns = [
    path('users/', views.RegisterView.as_view(), name='user-register'),
    path('users/<int:pk>/', views.UserDetailView.as_view(), name='user-detail'),
    path('auth/me/', views.MeView.as_view(), name='auth-me'),
    path('auth/logout/', views.LogoutView.as_view(), name='auth-logout'),
    path('auth/change-password/', views.ChangePasswordView.as_view(), name='auth-change-password'),
]
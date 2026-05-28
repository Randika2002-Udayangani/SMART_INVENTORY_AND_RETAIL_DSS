from django.urls import path
from .views import upload_daily_bill

urlpatterns = [
    path(
        'upload/daily-bill/',
        upload_daily_bill
    ),
]
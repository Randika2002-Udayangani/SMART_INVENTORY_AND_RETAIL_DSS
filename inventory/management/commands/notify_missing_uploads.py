from datetime import timedelta, datetime, time
from zoneinfo import ZoneInfo
from django.core.management.base import BaseCommand
from core.utils import get_latest_sync_uploads
from orders.models import Notification
from inventory.services.notifications import create_notification

LOCAL_TZ = ZoneInfo("Asia/Colombo")


class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        today = datetime.now(LOCAL_TZ).date()
        if today.weekday() == 6:
            return
        start_of_day = datetime.combine(today, time.min, tzinfo=LOCAL_TZ)
        end_of_day = start_of_day + timedelta(days=1)

        uploads, _ = get_latest_sync_uploads()
        for upload_type, row in uploads:
            if row is None or row.upload_date != today:
                title = f'{upload_type} not uploaded today'
                already_notified_today = Notification.objects.filter(
                    type='MISSING_UPLOAD',
                    title=title,
                    created_at__gte=start_of_day,
                    created_at__lt=end_of_day,
                ).exists()
                if already_notified_today:
                    continue
                create_notification(
                    type='MISSING_UPLOAD', priority='HIGH',
                    title=title,
                    message=f'No {upload_type} upload found for {today}.',
                    reference_table='upload_log',
                )
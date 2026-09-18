from datetime import date
from django.core.management.base import BaseCommand
from core.utils import get_latest_sync_uploads
from inventory.services.notifications import create_notification


class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        if date.today().weekday() == 6:  # Sunday — adjust to your real schedule
            return
        uploads, _ = get_latest_sync_uploads()
        for upload_type, row in uploads:
            if row is None or row.upload_date != date.today():
                create_notification(
                    type='MISSING_UPLOAD', priority='HIGH',
                    title=f'{upload_type} not uploaded today',
                    message=f'No {upload_type} upload found for {date.today()}.',
                    reference_table='upload_log',
                )
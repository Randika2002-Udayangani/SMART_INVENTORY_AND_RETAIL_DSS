from datetime import timedelta, datetime, time
from zoneinfo import ZoneInfo
from django.core.management.base import BaseCommand
from purchases.models import PurchaseBatch
from orders.models import Notification
from inventory.services.notifications import create_notification

LOCAL_TZ = ZoneInfo("Asia/Colombo")


class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        today = datetime.now(LOCAL_TZ).date()
        start_of_day = datetime.combine(today, time.min, tzinfo=LOCAL_TZ)
        end_of_day = start_of_day + timedelta(days=1)
        soon = today + timedelta(days=7)

        batches = PurchaseBatch.objects.filter(
            status__in=['ACTIVE', 'PENDING_EXPIRY'],
            remaining_quantity__gt=0,
            expiry_date__lte=soon,
            expiry_date__gte=today,
        )

        for batch in batches:
            already_notified_today = Notification.objects.filter(
                reference_table='purchase_batch',
                reference_id=batch.id,
                created_at__gte=start_of_day,
                created_at__lt=end_of_day,
            ).exists()
            if already_notified_today:
                continue
            create_notification(
                type='EXPIRING_BATCH', priority='MEDIUM',
                title='Batch expiring soon',
                message=f'{batch.product.product_name} — {batch.remaining_quantity} units expire {batch.expiry_date}.',
                reference_table='purchase_batch', reference_id=batch.id,
            )
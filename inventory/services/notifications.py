from orders.models import Notification

def create_notification(*, user=None, customer=None, type, priority, title, message,
                          reference_table='', reference_id=None, expires_at=None):
    return Notification.objects.create(
        user=user, customer=customer, type=type, priority=priority,
        title=title, message=message,
        reference_table=reference_table, reference_id=reference_id,
        expires_at=expires_at,
    )
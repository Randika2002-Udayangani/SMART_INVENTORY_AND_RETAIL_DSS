from .models import AuditLog


def log_action(user, action, table_name, record_id=None,
                old_value=None, new_value=None, request=None):
    """
    Writes one AuditLog row. Call this from any view after a
    create/update/delete on data that matters (financial, inventory, supplier).

    user        — request.user (Django auth User); stored as None if anonymous
    action      — short string, e.g. 'CREATE', 'UPDATE', 'DELETE', 'STOCK_ADJUSTMENT'
    table_name  — the logical table/model affected, e.g. 'supplier', 'purchase_batch'
    record_id   — the affected row's primary key
    old_value   — dict of the pre-change state (None for CREATE)
    new_value   — dict of the post-change state (None for DELETE)
    request     — pass the view's request object to capture IP address
    """
    ip = request.META.get('REMOTE_ADDR', '') if request is not None else ''

    AuditLog.objects.create(
        user=user if (user and user.is_authenticated) else None,
        action=action,
        table_name=table_name,
        record_id=record_id,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip,
    )
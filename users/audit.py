import ipaddress

from django.conf import settings

from .models import AuditLog


def get_client_ip(request):
    """
    Best-effort real client IP for the audit log.

    Behind a hosting proxy (Render, Cloudflare ...) REMOTE_ADDR is the proxy's
    address, so every audit row would show the same IP. The proxy adds the
    real client address to the X-Forwarded-For header, but a client can also
    send a fake X-Forwarded-For, so the header is only trusted when
    settings.AUDIT_PROXY_COUNT says how many trusted proxies sit in front of
    Django (0 = trust nothing, use REMOTE_ADDR; this is the safe default for
    local development). With N trusted proxies the real client is the N-th
    entry counted from the RIGHT, because each trusted proxy appends the
    address it received the request from; entries further left may be forged.
    """
    meta = request.META
    remote = meta.get('REMOTE_ADDR', '') or ''
    try:
        proxies = int(getattr(settings, 'AUDIT_PROXY_COUNT', 0))
    except (TypeError, ValueError):
        proxies = 0

    if proxies > 0:
        forwarded = [
            part.strip()
            for part in meta.get('HTTP_X_FORWARDED_FOR', '').split(',')
            if part.strip()
        ]
        if len(forwarded) >= proxies:
            candidate = forwarded[-proxies]
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    return remote


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
    ip = get_client_ip(request) if request is not None else ''

    AuditLog.objects.create(
        user=user if (user and user.is_authenticated) else None,
        action=action,
        table_name=table_name,
        record_id=record_id,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip,
    )

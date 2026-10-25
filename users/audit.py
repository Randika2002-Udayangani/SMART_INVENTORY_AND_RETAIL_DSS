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


def resolve_actors(logs):
    """
    Work out WHO did each audit action, for display.

    Staff actions have a user. Customer actions (placing or cancelling an
    online order) and system or not-signed-in events do not, so the
    audit page would show "Unknown". This turns each row into
    {log.id: (actor_type, actor_label)} where actor_type is one of
    STAFF, CUSTOMER, ANONYMOUS, SYSTEM.

    Only the customer's ID is kept in the audit row; the name is looked up
    here at display time (two small queries for the whole page, not one per
    row), so the log does not copy personal details.
    """
    from orders.models import Customer, OnlineOrder

    logs = list(logs)

    def details(log, key):
        for blob in (log.new_value, log.old_value):
            if isinstance(blob, dict) and blob.get(key) is not None:
                return blob[key]
        return None

    # Customer actions: no staff user and the row is about an online order.
    customer_rows = [l for l in logs if l.user_id is None and l.table_name == 'online_order']

    # Rows that do not carry customer_id (e.g. a cancellation) are resolved
    # through the order they point at, in ONE query.
    order_ids = {l.record_id for l in customer_rows
                 if details(l, 'customer_id') is None and l.record_id is not None}
    customer_by_order = dict(
        OnlineOrder.objects.filter(id__in=order_ids).values_list('id', 'customer_id')
    ) if order_ids else {}

    customer_id_by_log = {}
    for l in customer_rows:
        cid = details(l, 'customer_id')
        if cid is None:
            cid = customer_by_order.get(l.record_id)
        customer_id_by_log[l.id] = cid

    wanted = {cid for cid in customer_id_by_log.values() if cid is not None}
    names = dict(
        Customer.objects.filter(id__in=wanted).values_list('id', 'name')
    ) if wanted else {}

    result = {}
    for l in logs:
        if l.user_id is not None:
            username = l.user.username if l.user else f'User #{l.user_id}'
            result[l.id] = ('STAFF', username)
        elif l.id in customer_id_by_log:
            cid = customer_id_by_log[l.id]
            if cid is None:
                result[l.id] = ('CUSTOMER', 'Customer (details unavailable)')
            elif cid in names:
                result[l.id] = ('CUSTOMER', f'Customer #{cid} \u2013 {names[cid]}')
            else:
                result[l.id] = ('CUSTOMER', f'Deleted customer #{cid}')
        elif l.action == 'LOGIN_FAILED':
            tried = (l.new_value or {}).get('username', '') if isinstance(l.new_value, dict) else ''
            result[l.id] = ('ANONYMOUS', f'Not signed in (tried: {tried})' if tried else 'Not signed in')
        else:
            result[l.id] = ('SYSTEM', 'System')
    return result

"""
Shared helper functions used across multiple apps.
"""

def get_last_sync_date():
    """
    Return the latest incoming data upload date/time, as an ISO date
    string, or 'Not synced yet' if nothing has been uploaded.

    Counts an upload as "synced" if its final status is SUCCESS or
    PARTIAL - a PARTIAL upload still means real data was inserted
    (some individual rows were just skipped, which is routine on real
    PDF/Excel data), so it should still count as a sync for the
    purposes of this "last synced" indicator. Only FAILED uploads
    (nothing inserted at all) are excluded.

    This is the single source of truth for "last sync date" - do NOT
    duplicate this function elsewhere. Previously this logic existed
    in two separate places (inventory/views.py reading UploadLog with
    a SUCCESS-only filter, and sales/views.py reading a SystemConfig
    key that got updated unconditionally regardless of final upload
    status) and the two disagreed with each other for the same
    upload. Both are now replaced by this one function.
    """
    from sales.models import UploadLog

    last_upload = (
        UploadLog.objects
        .filter(
            upload_type__in=['DAILY_BILLS', 'SUPPLIER_INVOICE', 'ITEM_SALES'],
            status__in=['SUCCESS', 'PARTIAL'],
        )
        .order_by('-upload_date', '-id')
        .first()
    )
    return last_upload.upload_date.isoformat() if last_upload else 'Not synced yet'
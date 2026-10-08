"""
Item Ledger ↔ online-order pickup reconciliation.

Shared by sales/views.py (ItemLedgerPDFUploadView). Purpose: the Item
Ledger aggregates ALL sales for a product/date (online pickups rung up
at the counter + ordinary POS sales), but picked-up online orders were
ALREADY stock-deducted at order creation via deduct_stock_fefo
(source='ONLINE_ORDER', reference_id=order.id). Without reconciliation
the upload would deduct the same units a second time.

Deliberately NOT a naive product/date match against order data: the
only trusted signal that an online order's units are inside a day's
Item Ledger total is an explicit physical pickup (OnlineOrder.picked_up_at
IS NOT NULL). Orders without it (PENDING/CONFIRMED/READY, cancelled,
never collected) are excluded — otherwise ordinary POS sales would be
misclassified as online pickups.

Date semantics: Asia/Colombo LOCAL day of picked_up_at, matching the
detect_double_deduction command convention. settings.TIME_ZONE is UTC,
so __date lookups would use the wrong day boundary.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Sum

from inventory.models import StockLedger
from orders.models import OnlineOrder

LOCAL_TZ = ZoneInfo("Asia/Colombo")

# Explicit safety net, NOT redundant with picked_up_at__isnull=False:
# CANCELLED/EXPIRED orders release their ONLINE_ORDER stock deduction on
# cancellation and must never contribute to a day's confirmed online
# quantity — even if dirty data ever gives one a picked_up_at timestamp.
# Uses the model's own status constants rather than a hardcoded string.
_EXCLUDED_STATUSES = tuple(OnlineOrder.NEVER_PICKED_STATUSES)


def confirmed_online_quantity(product_id, sale_date):
    """
    Total units of `product_id` already stock-deducted at order creation
    (StockLedger source='ONLINE_ORDER') for online orders physically
    picked up on `sale_date` (Asia/Colombo local day).

    Returns:
        {
            'confirmed': int,        # abs sum of deduction quantities
            'order_ids': [int, ...]  # reference_ids of the deductions
        }
    """
    start_of_day = datetime.combine(sale_date, time.min, tzinfo=LOCAL_TZ)
    end_of_day = start_of_day + timedelta(days=1)

    picked_up_order_ids = set(
        OnlineOrder.objects.filter(
            picked_up_at__isnull=False,
            picked_up_at__gte=start_of_day,
            picked_up_at__lt=end_of_day,
        ).exclude(
            status__in=_EXCLUDED_STATUSES,
        ).values_list('id', flat=True)
    )
    if not picked_up_order_ids:
        return {'confirmed': 0, 'order_ids': []}

    deductions = StockLedger.objects.filter(
        product_id=product_id,
        source='ONLINE_ORDER',
        quantity_change__lt=0,
        reference_id__in=picked_up_order_ids,
    )
    totals = deductions.aggregate(total=Sum('quantity_change'))
    confirmed = abs(totals['total'] or 0)
    order_ids = sorted(set(
        deductions.values_list('reference_id', flat=True)
    ))
    return {'confirmed': confirmed, 'order_ids': order_ids}


def reconcile_item_ledger_deduction(product_id, sale_date, ledger_quantity):
    """
    Work out how much of an Item Ledger product/day total still needs a
    FEFO deduction, accounting for already-deducted confirmed online
    pickups.

    Returns:
        {
            'confirmed_online_quantity': int,
            'order_ids': [int, ...],
            'remaining_to_deduct': int,
            'risk': bool,  # True = DOUBLE_DEDUCTION_RISK (confirmed
                           # online deductions exceed the ledger total)
        }
    Caller must NOT deduct when risk is True and must NOT guess a
    quantity — flag the upload instead.
    """
    confirmed = confirmed_online_quantity(product_id, sale_date)
    remaining = ledger_quantity - confirmed['confirmed']
    return {
        'confirmed_online_quantity': confirmed['confirmed'],
        'order_ids': confirmed['order_ids'],
        'remaining_to_deduct': remaining,
        'risk': remaining < 0,
    }
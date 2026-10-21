"""
Single source of truth for "current available stock" at the product level.

FIX (stock audit, 2026-09): this previously summed StockLedger.quantity_change
directly. That's only mathematically equivalent to PurchaseBatch.remaining_quantity
if every single stock-affecting event has ALWAYS written a matching StockLedger
entry -- confirmed NOT reliably true: purchases/serializers.py wraps its
StockLedger insert in a try/except that logs and swallows failures rather
than failing the purchase, so a batch can exist with a nonzero
remaining_quantity but no corresponding PURCHASE ledger entry. In that case
the old ledger-sum would silently UNDER-count real stock, while
PurchaseBatch.remaining_quantity stays correct regardless (it's set
directly during purchase creation, before the ledger write is even
attempted).

PurchaseBatch.remaining_quantity is the more robust source -- it can't
silently drift the way a theoretically-complete-but-not-enforced ledger
can. StockLedger remains valuable as an audit trail / stock movement
history (see the Product Details modal's Stock Movement History), but is
no longer used to COMPUTE the stock total.

Sellable stock is limited to non-empty ACTIVE/PENDING_EXPIRY batches with
a verified future expiry date. This is the same definition used by customer
availability, so expired or undated stock cannot inflate available counts.
"""

from django.db.models import Q, Sum
from django.utils import timezone
from purchases.models import PurchaseBatch


def get_sellable_batches(product_id=None):
    """Return batches eligible for sale and customer availability counts.

    Non-perishable items may legitimately have no expiry date, so they must not
    be treated as out of stock just because their expiry field is null.
    """
    batches = PurchaseBatch.objects.filter(
        status__in=['ACTIVE', 'PENDING_EXPIRY'],
        remaining_quantity__gt=0,
    ).filter(
        Q(expiry_date__isnull=True) | Q(expiry_date__gt=timezone.now().date())
    )
    if product_id is not None:
        batches = batches.filter(product_id=product_id)
    return batches


def get_available_stock(product_id):
    """Return total unexpired, non-empty stock that can be sold now."""
    total = get_sellable_batches(product_id).aggregate(
        total=Sum('remaining_quantity')
    )['total']
    return total or 0
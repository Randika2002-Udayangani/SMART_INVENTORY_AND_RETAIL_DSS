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

Delegates to reorder_logic.get_current_stock() so there is exactly ONE
implementation of "current stock" in the codebase -- this file previously
duplicated that logic with a different (and less reliable) method. Also
fixes a second, independent bug: the old version had no status filter at
all, meaning EXPIRED/DISPOSED batches' original ledger contributions were
still counted unless a later negative entry happened to net them out --
fragile. reorder_logic.get_current_stock() correctly filters to
ACTIVE + PENDING_EXPIRY, the same real-sellable-stock definition used by
every other stock view in the project.
"""

from inventory.services.reorder_logic import get_current_stock as _get_current_stock


def get_available_stock(product_id):
    """
    Returns total sellable stock for a product. Single implementation,
    delegated -- see module docstring for why this changed.
    """
    return _get_current_stock(product_id)
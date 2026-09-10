"""
Reorder & Supplier Insights — Feature F11 (labelled F10 in pseudocode doc)

DOCUMENT SOURCES (all cross-referenced before writing):
  [LR]  Business Logic Report       — Feature 10, p.20  — formulas & thresholds
  [PC]  Pseudocode Document v3      — F10-A, p.26       — function structure
  [API] API Design Document v3.0    — Section 15        — endpoint contract
  [WK]  Week 4-5 Task Plan          — Day 1 spec        — urgency labels & test

⚠  CONFLICT RESOLVED — urgency classification:
  • [LR] and [PC] use RELATIVE urgency (tied to lead_time_days): CRITICAL/HIGH/NORMAL
  • [WK] uses ABSOLUTE day ranges: 0-3=CRITICAL, 4-7=HIGH, 8-14=MEDIUM, 15+=LOW
  Resolution: get_urgency() uses [WK] absolute ranges because
    (a) the task explicitly specifies them,
    (b) test case get_urgency(2) → CRITICAL confirms absolute not relative logic,
    (c) the function has no lead_time parameter.
  check_reorder_needs() still uses the [LR]/[PC] lead_time×2 threshold to decide
  WHETHER a recommendation is needed at all — both rules are honoured.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum

# ── Adjust these imports to match YOUR project's app names ──────────────────
from products.models import Product          # product_id, sku_code, avg_cost_price
#from inventory.models import PurchaseBatch  # remaining_quantity, status, purchase→supplier
from purchases.models import PurchaseBatch    #fix by R
from sales.models import ItemSalesRecord   # quantity_sold, sale_date


# ── Constants  [LR p.20] ────────────────────────────────────────────────────
SALES_LOOKBACK_DAYS   = 30    # "SUM(quantity_sold last 30 days) ÷ 30"
TARGET_STOCK_DAYS     = 30    # "(avg_daily_sales × 30)" in suggested qty formula
REORDER_CUTOFF_FACTOR = 2     # "days_stock > lead × 2 → no recommendation needed"
DEFAULT_LEAD_TIME     = 7     # fallback when no supplier record found


# ════════════════════════════════════════════════════════════════════════════
#  FUNCTION 1 — get_urgency
# ════════════════════════════════════════════════════════════════════════════

def get_urgency(days_of_stock: float) -> str:
    """
    Map days_of_stock to an urgency label using absolute day ranges.

    Source: [WK] Week 4-5 Task Plan — Day 1 spec
      0–3  days  →  CRITICAL
      4–7  days  →  HIGH
      8–14 days  →  MEDIUM
      15+  days  →  LOW

    Note on [LR]/[PC] conflict:
      The Logic Report and Pseudocode use relative urgency (≤lead → CRITICAL,
      ≤lead×1.5 → HIGH, ≤lead×2 → NORMAL). This function uses absolute
      ranges per the task plan. The lead_time comparison is handled separately
      inside check_reorder_needs() to decide whether to generate a recommendation.

    Args:
        days_of_stock (float): Remaining stock expressed as days of cover.
                               Use 0.0 if stock is completely depleted.

    Returns:
        str: One of 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'

    Tests that MUST pass  [WK]:
        >>> get_urgency(0)   == 'CRITICAL'
        >>> get_urgency(2)   == 'CRITICAL'   ← explicitly required by task plan
        >>> get_urgency(3)   == 'CRITICAL'
        >>> get_urgency(4)   == 'HIGH'
        >>> get_urgency(7)   == 'HIGH'
        >>> get_urgency(8)   == 'MEDIUM'
        >>> get_urgency(14)  == 'MEDIUM'
        >>> get_urgency(15)  == 'LOW'
        >>> get_urgency(100) == 'LOW'
    """
    if days_of_stock <= 3:
        return 'CRITICAL'
    elif days_of_stock <= 7:
        return 'HIGH'
    elif days_of_stock <= 14:
        return 'MEDIUM'
    else:
        return 'LOW'


# ════════════════════════════════════════════════════════════════════════════
#  FUNCTION 2 — calc_suggested_qty
# ════════════════════════════════════════════════════════════════════════════

def calc_suggested_qty(
    avg_daily_sales: Decimal,
    current_stock: int,
    lead_time_days: int,
) -> dict:
    """
    Calculate the suggested reorder quantity and safety stock.

    Source: [LR] Logic Report p.20 — Rule 2 "Suggested order quantity"
      safety_stock       = avg_daily_sales × lead_time_days
      suggested_quantity = (avg_daily_sales × 30) + safety_stock − current_stock
      suggested_quantity = MAX(0, suggested_quantity)

    Source: [PC] Pseudocode F10-A p.26
      safety   = avg_daily * lead
      suggested = MAX(0, (avg_daily*30) + safety - stock)

    Both sources agree exactly — no conflict.

    Args:
        avg_daily_sales (Decimal): Average units sold per day (last 30 days).
        current_stock   (int):     Total remaining_quantity across ACTIVE batches.
        lead_time_days  (int):     Supplier delivery time in days.

    Returns:
        dict:
            safety_stock       (int)  — buffer stock to cover the lead time period
            suggested_quantity (int)  — units to order (0 if no reorder needed)

    Examples:
        avg_daily=10, stock=50, lead=7
          safety      = 10 × 7   = 70
          raw         = (10×30) + 70 − 50 = 320
          suggested   = 320

        avg_daily=5, stock=200, lead=7
          safety      = 5 × 7    = 35
          raw         = (5×30) + 35 − 200 = −15
          suggested   = MAX(0, −15) = 0   ← already overstocked
    """
    safety_stock   = int(avg_daily_sales * lead_time_days)
    raw_suggested  = (avg_daily_sales * TARGET_STOCK_DAYS) + safety_stock - current_stock
    suggested_qty  = max(0, int(raw_suggested))

    return {
        'safety_stock':       safety_stock,
        'suggested_quantity': suggested_qty,
    }


# ════════════════════════════════════════════════════════════════════════════
#  FUNCTION 3 — check_reorder_needs
# ════════════════════════════════════════════════════════════════════════════

def check_reorder_needs(as_of: date = None) -> list:
    """
    Evaluate every active product and return those that need reordering.

    Structure follows [PC] Pseudocode F10-A p.26 exactly:
      FOR EACH active product:
        1. avg_daily_sales = SUM(qty last 30 days) / 30
        2. IF avg_daily_sales == 0 → CONTINUE  (SLOW_MOVING, handled by F06)
        3. current_stock   = SUM(remaining_quantity) from ACTIVE batches
        4. days_of_stock   = current_stock / avg_daily_sales
        5. lead_time_days  = Supplier.lead_time_days  (via PurchaseBatch chain)
        6. IF days_of_stock > lead × 2 → CONTINUE  (no action needed)  [LR/PC]
        7. urgency         = get_urgency(days_of_stock)                 [WK]
        8. qty_data        = calc_suggested_qty(...)                    [LR/PC]
        9. estimated_cost  = suggested_quantity × avg_cost_price
       10. Append result dict

    Output is sorted: CRITICAL first, then HIGH, MEDIUM, LOW.

    Returns:
        list[dict]:
            product_id         (int)
            product_name       (str)
            sku_code           (str)
            current_stock      (int)
            avg_daily_sales    (float)
            days_of_stock      (float)
            lead_time_days     (int)
            safety_stock       (int)
            suggested_quantity (int)
            estimated_cost     (float)   — suggested_qty × avg_cost_price
            urgency            (str)     — CRITICAL | HIGH | MEDIUM | LOW
            supplier_id        (int|None)
    """
    today   = as_of or date.today()
    since   = today - timedelta(days=SALES_LOOKBACK_DAYS)
    results = []

    active_products = Product.objects.filter(is_active=True).select_related()

    for product in active_products:

        # ── 1. avg_daily_sales ────────────────────────────────────────────────
        # [LR] "avg_daily_sales = SUM(quantity_sold last 30 days) ÷ 30"
        sales_agg = ItemSalesRecord.objects.filter(
            product=product,
            sale_date__gte=since,
            sale_date__lte=today,
        ).aggregate(total_sold=Sum('quantity_sold'))

        total_sold      = sales_agg['total_sold'] or 0
        avg_daily_sales = Decimal(str(total_sold)) / Decimal(str(SALES_LOOKBACK_DAYS))

        # ── 2. Skip SLOW_MOVING ───────────────────────────────────────────────
        # [PC] "IF avg_daily == 0: CONTINUE  // flagged SLOW_MOVING in F06"
        if avg_daily_sales == 0:
            continue

        # ── 3. Current stock ──────────────────────────────────────────────────
        # [LR] "current_stock = SUM(remaining_quantity) from ACTIVE batches"
        stock_agg = PurchaseBatch.objects.filter(
            product=product,
            status__in=['ACTIVE', 'PENDING_EXPIRY'],   # ← was status='ACTIVE'
            remaining_quantity__gt=0,
        ).aggregate(total_stock=Sum('remaining_quantity'))

        current_stock = stock_agg['total_stock'] or 0

        # ── 4. days_of_stock ──────────────────────────────────────────────────
        # [LR] "days_of_stock = current_stock ÷ avg_daily_sales"
        days_of_stock = float(current_stock) / float(avg_daily_sales)

        # ── 5. Lead time ──────────────────────────────────────────────────────
        supplier_id, lead_time_days = _get_supplier_lead_time(product)

        # ── 6. Skip if no reorder needed ──────────────────────────────────────
        # [LR] "IF days_of_stock > lead_time_days × 2 → no recommendation needed"
        # [PC] "ELSE: CONTINUE"
        if days_of_stock > lead_time_days * REORDER_CUTOFF_FACTOR:
            continue

        # ── 7. Urgency  (absolute ranges from [WK]) ───────────────────────────
        urgency = get_urgency(days_of_stock)

        # ── 8. Suggested quantity  [LR/PC] ────────────────────────────────────
        qty_data = calc_suggested_qty(
            avg_daily_sales=avg_daily_sales,
            current_stock=current_stock,
            lead_time_days=lead_time_days,
        )

        # ── 9. Estimated cost  [PC] "suggested * product.avg_cost_price" ──────
        avg_cost  = product.avg_cost_price or product.cost_price or Decimal('0')
        est_cost  = qty_data['suggested_quantity'] * float(avg_cost)

        # ── 10. Append ────────────────────────────────────────────────────────
        results.append({
            'product_id':         product.id,
            'product_name':       product.product_name,
            'sku_code':           product.sku_code or '',
            'current_stock':      current_stock,
            'avg_daily_sales':    round(float(avg_daily_sales), 2),
            'days_of_stock':      round(days_of_stock, 1),
            'lead_time_days':     lead_time_days,
            'safety_stock':       qty_data['safety_stock'],
            'suggested_quantity': qty_data['suggested_quantity'],
            'estimated_cost':     round(est_cost, 2),
            'urgency':            urgency,
            'supplier_id':        supplier_id,
        })

        # [PC] "IF urgency==CRITICAL: NOTIFY(manager, ...)"
        # Notification is created in the API view layer, not here,
        # to keep this service pure (no HTTP/request context needed).

    # Sort: CRITICAL → HIGH → MEDIUM → LOW
    _PRIORITY = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
    results.sort(key=lambda r: _PRIORITY[r['urgency']])

    return results


# ════════════════════════════════════════════════════════════════════════════
#  HELPERS  (used internally + exposed for health_score.py / lifecycle.py)
# ════════════════════════════════════════════════════════════════════════════

def get_current_stock(product_id: int) -> int:
    agg = PurchaseBatch.objects.filter(
        product_id=product_id,
        status__in=['ACTIVE', 'PENDING_EXPIRY'],   # ← was status='ACTIVE'
        remaining_quantity__gt=0,
    ).aggregate(total=Sum('remaining_quantity'))

    return agg['total'] or 0


def _get_supplier_lead_time(product: Product):
    """
    Find the most recent supplier for this product and return their lead time.

    Traversal: PurchaseBatch → Purchase → Supplier → lead_time_days
    Falls back to DEFAULT_LEAD_TIME (7 days) if no supplier found.

    Returns:
        tuple: (supplier_id: int|None, lead_time_days: int)
    """
    latest_batch = (
        PurchaseBatch.objects
        .filter(product=product, status='ACTIVE')
        .select_related('purchase__supplier')
        .order_by('-id')
        .first()
    )

    if latest_batch and latest_batch.purchase and latest_batch.purchase.supplier:
        supplier       = latest_batch.purchase.supplier
        lead_time      = supplier.lead_time_days or DEFAULT_LEAD_TIME
        return supplier.id, lead_time
    return None, DEFAULT_LEAD_TIME
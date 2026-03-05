
#
# Query optimization summary:
#   v1: N+1 problem          — 1 Product query per product inside loop
#   v2: N aggregate queries  — 1 aggregation query per product inside loop
#   v3 (this): 3 total queries for entire function:
#       Query 1 — grouped annotate: all product totals in one shot
#       Query 2 — products_map: all product details in one shot
#       Query 3 — bill_agg: store revenue and discount totals

from decimal import Decimal
from django.db.models import Sum, F, ExpressionWrapper, DecimalField
from sales.models import ItemSalesRecord, DailyBillSummary
from products.models import Product


# ═══════════════════════════════════════════════════════════════════════════════
# F05-A: Revenue & Profit Calculation (WAC method)
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_sales_and_profit(start_date, end_date):
    """
    Calculates per-product profit using WAC method for a given date range.

    WAC chosen over FIFO because customers at Samanala Super Mart physically
    pick items from the back of shelves, breaking FIFO order in practice.
    (Documented in project report — client site observation.)

    WAC dependency: avg_cost_price must be recalculated after every new
    PurchaseBatch save. See purchases/services.py → recalculate_wac().
    If WAC is stale, profit figures here will be incorrect.

    Database queries in this function: 3 total (regardless of product count)
        Query 1 — grouped annotate on ItemSalesRecord
        Query 2 — Product fetch with brand/category
        Query 3 — DailyBillSummary aggregation

    Returns:
        results        — list of per-product profit dictionaries
        store_revenue  — total store revenue from DailyBillSummary
        total_discount — total discounts given in the period
        consistency    — mismatch report between two revenue sources
    """

    # ── Query 1: ONE grouped query — all product totals at once ───────────────
    # .values('product_id')         → group by product_id
    # .annotate(...)                → calculate totals per group
    # Result: one row per product with total_qty and total_revenue already summed
    # This replaces the entire loop of N aggregate queries with a single DB call
    aggregated = (
        ItemSalesRecord.objects
        .filter(sale_date__range=(start_date, end_date))
        .values('product_id')
        .annotate(
            total_qty=Sum('quantity_sold'),
            total_revenue=Sum('total_amount'),
        )
    )
    # aggregated is now like:
    # [
    #   {'product_id': 1, 'total_qty': 120, 'total_revenue': 48000.00},
    #   {'product_id': 2, 'total_qty': 45,  'total_revenue': 22500.00},
    #   ...
    # ]

    # Extract product IDs from the aggregated result
    product_ids = [row['product_id'] for row in aggregated]

    # ── Query 2: ONE query — fetch all needed products with brand & category ───
    products_map = {
        p.id: p for p in Product.objects.filter(
            id__in=product_ids
        ).select_related('brand', 'category')
    }

    # ── Build results — pure Python from here, zero DB queries ────────────────
    results = []

    for row in aggregated:
        product_id    = row['product_id']
        total_qty     = row['total_qty']     or 0
        total_revenue = row['total_revenue'] or Decimal('0.00')

        # Dictionary lookup — no DB hit
        product  = products_map[product_id]

        # WAC dependency: this value must be kept current by purchases module
        avg_cost = product.avg_cost_price or Decimal('0.00')

        # Profit = revenue - (qty * avg_cost)
        # WAC profit formula: profit per unit = unit_price - avg_cost_price
        # total_profit = SUM(qty * unit_price) - SUM(qty * avg_cost)
        #              = total_revenue - (total_qty * avg_cost)
        total_profit = total_revenue - (total_qty * avg_cost)

        # Guard: avoid ZeroDivisionError if revenue is zero
        margin_pct = (
            (total_profit / total_revenue * 100)
            if total_revenue
            else Decimal('0')
        )

        # Business insight flags
        flags = []
        if margin_pct > 25 and total_qty < 50:
            flags.append('HIGH_MARGIN_LOW_VOLUME')
        if margin_pct < 10 and total_qty > 200:
            flags.append('LOW_MARGIN_HIGH_VOLUME')

        results.append({
            'product_id'    : product.id,
            'product_name'  : product.product_name,
            'brand_id'      : product.brand_id,
            'brand_name'    : product.brand.brand_name if product.brand else None,
            'category_id'   : product.category_id,
            'category_name' : product.category.category_name if product.category else None,
            'total_qty'     : total_qty,
            'total_revenue' : float(total_revenue),
            'total_profit'  : float(total_profit),
            'margin_pct'    : round(float(margin_pct), 2),
            'flags'         : flags,
        })

    # ── Query 3: Store-level totals from DailyBillSummary ─────────────────────
    bill_agg = DailyBillSummary.objects.filter(
        sale_date__range=(start_date, end_date)
    ).aggregate(
        total_revenue=Sum('final_amount'),
        total_discount=Sum('discount')
    )
    store_revenue  = float(bill_agg['total_revenue']  or 0)
    total_discount = float(bill_agg['total_discount'] or 0)

    # ── Revenue mismatch detection ─────────────────────────────────────────────
    # ItemSalesRecord and DailyBillSummary come from two separate pipelines.
    # They will not always match due to discounts, returns, rounding,
    # and flagged internal transfers (e.g. BAKERY bills).
    # Surface mismatch to manager — do not hide it.
    item_sales_total = sum(r['total_revenue'] for r in results)
    mismatch_amount  = round(abs(store_revenue - item_sales_total), 2)

    consistency = {
        'item_sales_total' : round(item_sales_total, 2),
        'bill_sales_total' : round(store_revenue, 2),
        'mismatch_amount'  : mismatch_amount,
        'mismatch_flag'    : mismatch_amount > 0,
    }

    return results, store_revenue, total_discount, consistency


# ═══════════════════════════════════════════════════════════════════════════════
# F05-B: Brand & Category Aggregation
# ═══════════════════════════════════════════════════════════════════════════════

def aggregate_by_brand_and_category(start_date, end_date, product_results=None):
    """
    Groups profit totals by brand and category, sorted descending by profit.

    Accepts pre-computed product_results to avoid recalculating.
    If Randika's view already called calculate_sales_and_profit(), she passes
    the results here — no repeated heavy queries.

    Called by: GET /api/analytics/brand-comparison/
    """
    if product_results is None:
        product_results, _, _, _ = calculate_sales_and_profit(start_date, end_date)

    brand_map    = {}
    category_map = {}

    for result in product_results:

        # Brand aggregation
        b_id = result['brand_id']
        if b_id not in brand_map:
            brand_map[b_id] = {
                'brand_name'    : result['brand_name'],
                'total_profit'  : 0.0,
                'total_revenue' : 0.0,
            }
        brand_map[b_id]['total_profit']  += result['total_profit']
        brand_map[b_id]['total_revenue'] += result['total_revenue']

        # Category aggregation
        c_id = result['category_id']
        if c_id not in category_map:
            category_map[c_id] = {
                'category_name' : result['category_name'],
                'total_profit'  : 0.0,
                'total_revenue' : 0.0,
            }
        category_map[c_id]['total_profit']  += result['total_profit']
        category_map[c_id]['total_revenue'] += result['total_revenue']

    sorted_brands = sorted(
        brand_map.values(), key=lambda x: x['total_profit'], reverse=True
    )
    sorted_categories = sorted(
        category_map.values(), key=lambda x: x['total_profit'], reverse=True
    )

    return sorted_brands, sorted_categories


# ═══════════════════════════════════════════════════════════════════════════════
# F05-C: Top N Products
# ═══════════════════════════════════════════════════════════════════════════════

def get_top_products(start_date, end_date, rank_by='profit', limit=5, product_results=None):
    """
    Returns top N products ranked by profit or quantity sold.

    Accepts pre-computed product_results to avoid recalculating.

    rank_by = 'profit' (default) or 'qty'
    limit   = number of products to return (default 5)

    Called by: GET /api/analytics/top-products/?rank_by=profit
    """
    if product_results is None:
        product_results, _, _, _ = calculate_sales_and_profit(start_date, end_date)

    if rank_by == 'qty':
        sorted_results = sorted(
            product_results, key=lambda x: x['total_qty'], reverse=True
        )
    else:
        sorted_results = sorted(
            product_results, key=lambda x: x['total_profit'], reverse=True
        )

    return sorted_results[:limit]
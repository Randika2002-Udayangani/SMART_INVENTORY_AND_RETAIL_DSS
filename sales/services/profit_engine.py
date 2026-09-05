#
# Query optimization summary:
#   v1: N+1 problem          — 1 Product query per product inside loop
#   v2: N aggregate queries  — 1 aggregation query per product inside loop
#   v3 (this): 3 total queries for entire function:
#       Query 1 — grouped annotate: all product totals in one shot
#       Query 2 — products_map: all product details in one shot
#       Query 3 — bill_agg: store revenue and discount totals
#       Exception: if no sales in range → Query 2 and 3 skipped entirely


#   Fix 7  — mismatch_flag uses 2% threshold not > 0
#   Fix 8  — products_map.get() with None guard (KeyError on deleted products)
#   Fix 9  — early return when no sales (avoids useless Product query)
#   Fix 10 — LOSS_PRODUCT flag added for negative profit products
#   Fix 11 — Decimal kept in results dict, float() only at API output layer
#            removes the Decimal → float → Decimal(str()) round-trip

from decimal import Decimal
from datetime import date, timedelta
from django.db.models import Sum, Count
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
    PurchaseBatch save. See purchases/serializers.py → Fix 7 (remaining_quantity).
    If WAC is stale, profit figures here will be incorrect.

    Database queries: 3 total (regardless of product count)
        Query 1 — grouped annotate on ItemSalesRecord
        Query 2 — Product fetch with brand/category
        Query 3 — DailyBillSummary aggregation
        Exception: if no sales in range → Query 2 and 3 skipped entirely

    NOTE on results[] data types:
        total_revenue, total_profit, margin_pct are kept as Decimal
        throughout this function for full numerical precision.
        The caller (Randika's view/serializer) is responsible for
        converting to float/str for JSON serialization.
        This avoids the Decimal → float → Decimal(str()) round-trip
        that would occur if we converted here.

    Returns:
        results        — list of per-product profit dicts (Decimal values)
        store_revenue  — total store revenue from DailyBillSummary (float)
        total_discount — total discounts given in the period (float)
        consistency    — mismatch report between two revenue sources
    """

    # ── Query 1: ONE grouped query — all product totals at once ───────────────
    aggregated = (
        ItemSalesRecord.objects
        .filter(sale_date__range=(start_date, end_date))
        .values('product_id')
        .annotate(
            total_qty     = Sum('quantity_sold'),
            total_revenue = Sum('total_amount'),
        )
    )

    product_ids = [row['product_id'] for row in aggregated]

    # ── Fix 9: Early return when no sales ─────────────────────────
    # product_ids = [] → Product.objects.filter(id__in=[]) is a useless query
    # DailyBillSummary query would also return zeros
    # Return immediately — avoids 2 unnecessary DB queries
    if not product_ids:
        return [], 0, 0, {
            'item_sales_total' : 0,
            'bill_sales_total' : 0,
            'mismatch_amount'  : 0,
            'mismatch_pct'     : 0,
            'mismatch_flag'    : False,
        }

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

        # Fix 8: KeyError guard — skip if product deleted after sales recorded
        product = products_map.get(product_id)
        if not product:
            continue

        # WAC: avg_cost_price maintained by purchases/serializers.py Fix 7
        avg_cost     = product.avg_cost_price or Decimal('0.00')
        total_profit = total_revenue - (total_qty * avg_cost)

        # Margin — Decimal throughout, quantized for consistent precision
        margin_pct = (
            (total_profit / total_revenue * 100)
            if total_revenue
            else Decimal('0')
        ).quantize(Decimal('0.01'))

        # Fix 10: Business insight flags
        # LOSS_PRODUCT checked first — highest priority signal
        flags = []
        if total_profit < 0:
            flags.append('LOSS_PRODUCT')
        if margin_pct > 25 and total_qty < 50:
            flags.append('HIGH_MARGIN_LOW_VOLUME')
        if margin_pct < 10 and total_qty > 200:
            flags.append('LOW_MARGIN_HIGH_VOLUME')

        # ── Fix 11: Decimal kept in results — NO float() conversion here ──
        # total_revenue, total_profit, margin_pct stay as Decimal objects
        # The view/serializer that calls this function converts to float/str
        # for JSON output. This keeps full precision for any downstream
        # analytics that reuse results[] (F05-B, F05-C, mismatch calc below)
        results.append({
            'product_id'    : product.id,
            'product_name'  : product.product_name,
            'brand_id'      : product.brand_id    or 0,
            'brand_name'    : product.brand.brand_name       if product.brand     else 'UNBRANDED',
            'category_id'   : product.category_id or 0,
            'category_name' : product.category.category_name if product.category  else 'UNCATEGORISED',
            'total_qty'     : total_qty,
            'total_revenue' : total_revenue,   # Decimal — caller converts
            'total_profit'  : total_profit,    # Decimal — caller converts
            'margin_pct'    : margin_pct,      # Decimal — caller converts
            'flags'         : flags,
        })

    # ── Query 3: Store-level totals from DailyBillSummary ─────────────────────
    bill_agg = DailyBillSummary.objects.filter(
        sale_date__range=(start_date, end_date)
    ).aggregate(
        total_revenue  = Sum('final_amount'),
        total_discount = Sum('discount')
    )
    store_revenue  = float(bill_agg['total_revenue']  or 0)
    total_discount = float(bill_agg['total_discount'] or 0)

    # ── Revenue mismatch detection ─────────────────────────────────────────────
    # ItemSalesRecord and DailyBillSummary come from two separate pipelines.
    # They will never match exactly due to discounts, returns, rounding,
    # and flagged internal transfers (e.g. BAKERY bills).
    # Fix 11 benefit: results[] now holds Decimal values so this sum is
    # clean Decimal arithmetic — no Decimal(str(float)) workaround needed
    item_sales_total = float(sum(r['total_revenue'] for r in results))
    mismatch_amount  = round(abs(store_revenue - item_sales_total), 2)

    mismatch_pct = (
        round((mismatch_amount / store_revenue) * 100, 2)
        if store_revenue > 0
        else 0.0
    )

    # Fix 7: 2% threshold — agreed with client, documented in project report
    consistency = {
        'item_sales_total' : round(item_sales_total, 2),
        'bill_sales_total' : round(store_revenue, 2),
        'mismatch_amount'  : mismatch_amount,
        'mismatch_pct'     : mismatch_pct,
        'mismatch_flag'    : mismatch_amount > store_revenue * 0.02,
    }

    return results, store_revenue, total_discount, consistency


# ═══════════════════════════════════════════════════════════════════════════════
# F05-B: Brand & Category Aggregation
# ═══════════════════════════════════════════════════════════════════════════════

def aggregate_by_brand_and_category(start_date, end_date, product_results=None):
    """
    Groups profit totals by brand and category, sorted descending by profit.

    Accepts pre-computed product_results to avoid recalculating.
    results[] values are Decimal — converted to float here for output.

    Called by: GET /api/analytics/brand-comparison/
    """
    if product_results is None:
        product_results, _, _, _ = calculate_sales_and_profit(start_date, end_date)

    brand_map    = {}
    category_map = {}

    for result in product_results:

        b_id = result['brand_id']
        if b_id not in brand_map:
            brand_map[b_id] = {
                'brand_name'    : result['brand_name'],
                'total_profit'  : Decimal('0.00'),
                'total_revenue' : Decimal('0.00'),
            }
        brand_map[b_id]['total_profit']  += result['total_profit']
        brand_map[b_id]['total_revenue'] += result['total_revenue']

        c_id = result['category_id']
        if c_id not in category_map:
            category_map[c_id] = {
                'category_name' : result['category_name'],
                'total_profit'  : Decimal('0.00'),
                'total_revenue' : Decimal('0.00'),
            }
        category_map[c_id]['total_profit']  += result['total_profit']
        category_map[c_id]['total_revenue'] += result['total_revenue']

    # Convert to float at output — safe here, no further arithmetic
    sorted_brands = sorted(
        [
            {
                'brand_name'    : v['brand_name'],
                'total_profit'  : float(v['total_profit']),
                'total_revenue' : float(v['total_revenue']),
            }
            for v in brand_map.values()
        ],
        key=lambda x: x['total_profit'], reverse=True
    )
    sorted_categories = sorted(
        [
            {
                'category_name' : v['category_name'],
                'total_profit'  : float(v['total_profit']),
                'total_revenue' : float(v['total_revenue']),
            }
            for v in category_map.values()
        ],
        key=lambda x: x['total_profit'], reverse=True
    )

    return sorted_brands, sorted_categories


# ═══════════════════════════════════════════════════════════════════════════════
# F05-C: Top N Products
# ═══════════════════════════════════════════════════════════════════════════════

def get_top_products(start_date, end_date, rank_by='profit', limit=5, product_results=None):
    """
    Returns top N products ranked by profit or quantity sold.

    Accepts pre-computed product_results to avoid recalculating.
    results[] values are Decimal — sorting on Decimal is valid and precise.

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


# ═══════════════════════════════════════════════════════════════════════════════
# F05-D: Slow-Moving / Decision Support
# ═══════════════════════════════════════════════════════════════════════════════

def slow_moving(start_date, end_date, product_results=None):
    """
    Flags products whose margin and volume diverge from their own
    category's average — per API Design Doc §10:
    "Products where margin < category_avg AND qty > category_avg
    (high volume, low margin) or vice versa."

    Two flag types returned (both directions of the divergence,
    since the design doc's "or vice versa" covers both):
        HIGH_VOLUME_LOW_MARGIN — selling a lot, but thin margin
                                  relative to peers in the same
                                  category. Candidate for review —
                                  competing on price at the expense
                                  of profit.
        LOW_VOLUME_HIGH_MARGIN — barely selling, but fat margin.
                                  Candidate for promotion/zone
                                  placement rather than discount —
                                  the margin is fine, visibility isn't.

    Category averages are computed from the SAME product_results
    passed in (or freshly calculated) — not a separate DB query —
    so this stays consistent with whatever period the caller is
    already looking at, and costs zero extra queries beyond
    calculate_sales_and_profit()'s existing 3.

    Products with no category (category_id == 0 / UNCATEGORISED)
    are skipped — there's no peer group to compare them against,
    and lumping every uncategorised product into one fake "category"
    would produce a meaningless average.

    Returns:
        list of dicts, one per flagged product:
            product_id, product_name, category_name,
            margin_pct, total_qty  (the product's own figures)
            category_avg_margin_pct, category_avg_qty  (its peer average)
            flag  — 'HIGH_VOLUME_LOW_MARGIN' or 'LOW_VOLUME_HIGH_MARGIN'

    Called by: GET /api/analytics/slow-moving/
    """
    if product_results is None:
        product_results, _, _, _ = calculate_sales_and_profit(start_date, end_date)

    # ── Build category averages first — needs every product's numbers
    #    before any single product can be compared against its peers ──────────
    category_totals = {}
    for result in product_results:
        c_id = result['category_id']
        if c_id == 0:
            continue  # UNCATEGORISED — no peer group, skip from averaging too

        if c_id not in category_totals:
            category_totals[c_id] = {
                'category_name'  : result['category_name'],
                'margin_sum'     : Decimal('0.00'),
                'qty_sum'        : 0,
                'product_count'  : 0,
            }
        category_totals[c_id]['margin_sum']    += result['margin_pct']
        category_totals[c_id]['qty_sum']       += result['total_qty']
        category_totals[c_id]['product_count'] += 1

    category_averages = {
        c_id: {
            'category_name'          : totals['category_name'],
            'category_avg_margin_pct': (totals['margin_sum'] / totals['product_count']).quantize(Decimal('0.01')),
            'category_avg_qty'       : totals['qty_sum'] / totals['product_count'],
        }
        for c_id, totals in category_totals.items()
    }

    # ── Compare each product against its own category's average ──────────────
    flagged = []
    for result in product_results:
        c_id = result['category_id']
        if c_id == 0 or c_id not in category_averages:
            continue

        avg = category_averages[c_id]
        margin_pct = result['margin_pct']
        total_qty  = result['total_qty']

        flag = None
        if margin_pct < avg['category_avg_margin_pct'] and total_qty > avg['category_avg_qty']:
            flag = 'HIGH_VOLUME_LOW_MARGIN'
        elif margin_pct > avg['category_avg_margin_pct'] and total_qty < avg['category_avg_qty']:
            flag = 'LOW_VOLUME_HIGH_MARGIN'

        if flag:
            flagged.append({
                'product_id'              : result['product_id'],
                'product_name'            : result['product_name'],
                'category_name'           : result['category_name'],
                'margin_pct'              : float(margin_pct),
                'total_qty'               : total_qty,
                'category_avg_margin_pct' : float(avg['category_avg_margin_pct']),
                'category_avg_qty'        : float(avg['category_avg_qty']),
                'flag'                    : flag,
            })

    return flagged


# ═══════════════════════════════════════════════════════════════════════════════
# F05-E: Sales Trend (Chart Data)
# ═══════════════════════════════════════════════════════════════════════════════

def sales_trend(period='daily', months=6, start_date=None, end_date=None):
    """
    Buckets sales into daily / weekly / monthly totals for chart
    rendering — per API Design Doc §10:
    "Daily/weekly/monthly sales trend data for chart.
    Pass ?period=daily/weekly/monthly and ?months=6"

    Fix (analytics overview rebuild): originally this only ever computed
    its own date range from `months` back from today() — meaning the
    trend chart could never share the same date range as every other
    analytics endpoint on the page, which all take explicit
    date_from/date_to. That's the "inconsistent date filter" bug.

    start_date/end_date, when both given, now take priority over
    months and are used as-is. months stays as the fallback for any
    caller (e.g. a manager-dashboard trend widget) that just wants
    "last N months from today" without picking exact dates.

    Uses Django's Trunc* DB functions to do the bucketing inside
    the database (single aggregate query) rather than pulling every
    row and bucketing in Python — same "push the work to the DB"
    approach as calculate_sales_and_profit()'s grouped annotate.

    period='daily'   → TruncDate,  one point per calendar day
    period='weekly'  → TruncWeek,  one point per ISO week start
    period='monthly' → TruncMonth, one point per calendar month
    Anything else falls back to 'daily' rather than raising —
    matches the defensive-default pattern already used for
    SystemConfig lookups elsewhere in this file's sibling module
    (discount_engine.py's _get_config_value).

    Returns:
        list of dicts, oldest → newest:
            period_label   str  — ISO date of the bucket start
            total_qty      int
            total_revenue  float

    Called by: GET /api/analytics/sales-trend/?period=daily&months=6
    """
    from django.db.models.functions import TruncDate, TruncWeek, TruncMonth

    trunc_map = {
        'daily':   TruncDate,
        'weekly':  TruncWeek,
        'monthly': TruncMonth,
    }
    trunc_fn = trunc_map.get(period, TruncDate)

    if start_date is None or end_date is None:
        end_date   = date.today()
        start_date = end_date - timedelta(days=months * 30)

    bucketed = (
        ItemSalesRecord.objects
        .filter(sale_date__range=(start_date, end_date))
        .annotate(bucket=trunc_fn('sale_date'))
        .values('bucket')
        .annotate(
            total_qty     = Sum('quantity_sold'),
            total_revenue = Sum('total_amount'),
        )
        .order_by('bucket')
    )

    return [
        {
            'period_label'  : str(row['bucket']),
            'total_qty'     : row['total_qty']     or 0,
            'total_revenue' : float(row['total_revenue'] or 0),
        }
        for row in bucketed
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# F05-F: Category Performance
# ═══════════════════════════════════════════════════════════════════════════════

def category_performance(start_date, end_date, product_results=None):
    """
    Sales and profit grouped by category, with a margin ranking —
    per API Design Doc §10: "Sales and profit grouped by category.
    Includes margin ranking."

    Builds on the same category_map grouping used in
    aggregate_by_brand_and_category() (F05-B) rather than
    re-querying — but adds margin_pct per category and a 1-indexed
    rank field, which brand-comparison's output doesn't need.

    Accepts pre-computed product_results to avoid recalculating,
    same convention as F05-B and F05-C.

    Rank is by margin_pct descending (highest-margin category = rank 1),
    matching "margin ranking" in the spec — not by total_profit, since
    a high-revenue category can have thin margins and a small category
    can be very profitable per rupee sold; margin is what "ranking"
    refers to here.

    Returns:
        list of dicts, sorted by rank ascending:
            category_name, total_revenue, total_profit, margin_pct, rank

    Called by: GET /api/analytics/category-performance/
    """
    if product_results is None:
        product_results, _, _, _ = calculate_sales_and_profit(start_date, end_date)

    category_map = {}
    for result in product_results:
        c_id = result['category_id']
        if c_id not in category_map:
            category_map[c_id] = {
                'category_name' : result['category_name'],
                'total_profit'  : Decimal('0.00'),
                'total_revenue' : Decimal('0.00'),
            }
        category_map[c_id]['total_profit']  += result['total_profit']
        category_map[c_id]['total_revenue'] += result['total_revenue']

    rows = []
    for v in category_map.values():
        margin_pct = (
            (v['total_profit'] / v['total_revenue'] * 100)
            if v['total_revenue']
            else Decimal('0')
        ).quantize(Decimal('0.01'))

        rows.append({
            'category_name' : v['category_name'],
            'total_revenue' : float(v['total_revenue']),
            'total_profit'  : float(v['total_profit']),
            'margin_pct'    : float(margin_pct),
        })

    # ── Rank by margin_pct descending — highest margin category first ─────────
    rows.sort(key=lambda x: x['margin_pct'], reverse=True)
    for i, row in enumerate(rows, start=1):
        row['rank'] = i

    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# F05-G: Store Revenue KPIs
# ═══════════════════════════════════════════════════════════════════════════════

def store_revenue(start_date, end_date):
    """
    Store-level KPIs sourced from DailyBillSummary — per API Design
    Doc §10: "Store-level KPIs from Daily_Bill_Summary: total revenue,
    total discount, net revenue, payment type breakdown."

    Deliberately reads DailyBillSummary, NOT ItemSalesRecord —
    these are the two separate easyAcc pipelines (§9 Data Ingestion).
    Store-level KPIs belong to the bill-level pipeline; product-level
    profit belongs to the item-ledger pipeline. Mixing them here
    would reintroduce the same item-sales-vs-bill-sales mismatch
    that calculate_sales_and_profit()'s consistency check already
    exists to detect — so this function stays on its own pipeline.

    total_revenue = sum(gross_amount)   — before discount
    total_discount = sum(discount)
    net_revenue = sum(final_amount)     — what the store actually took in

    payment_type breakdown skips bills with payment_type == '' —
    per the model, payment_type is blank=True, so older or
    incompletely-parsed bills may not have it set. Counting those
    under a fake "UNKNOWN" bucket would silently hide a data-quality
    issue; leaving them out of the breakdown (while still counting
    them in the totals above) makes the gap visible instead — the
    breakdown total will be lower than total_revenue if any bills
    are missing payment_type, which is the intended signal.

    Returns:
        dict:
            total_revenue    float
            total_discount   float
            net_revenue      float
            payment_breakdown  list of {payment_type, total_amount, bill_count}
            period           {date_from, date_to}

    Called by: GET /api/analytics/store-revenue/
    """
    bills = DailyBillSummary.objects.filter(
        sale_date__range=(start_date, end_date)
    )

    totals = bills.aggregate(
        total_revenue  = Sum('gross_amount'),
        total_discount = Sum('discount'),
        net_revenue    = Sum('final_amount'),
    )

    payment_rows = (
        bills.exclude(payment_type='')
        .values('payment_type')
        .annotate(
            total_amount = Sum('final_amount'),
            bill_count   = Count('id'),
        )
        .order_by('-total_amount')
    )

    return {
        'total_revenue'  : float(totals['total_revenue']  or 0),
        'total_discount' : float(totals['total_discount'] or 0),
        'net_revenue'    : float(totals['net_revenue']    or 0),
        'payment_breakdown': [
            {
                'payment_type' : row['payment_type'],
                'total_amount' : float(row['total_amount'] or 0),
                'bill_count'   : row['bill_count'],
            }
            for row in payment_rows
        ],
        'period': {
            'date_from': str(start_date),
            'date_to':   str(end_date),
        }
    }
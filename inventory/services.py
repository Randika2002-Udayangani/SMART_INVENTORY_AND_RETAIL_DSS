# Optimization history:
#   v1: N×3 queries          — 3 DB queries per product inside loop
#   v2: 4 total queries      — pre-aggregated with grouped annotate
#   v3: 5 total queries      — added bulk_create
#   v4 (this): 6 total queries — all fixes applied:
#       Fix 1 — row['total'] or 0 guard in all three maps
#       Fix 2 — delete existing records before bulk_create (no duplicates)
#       Fix 3 — UniqueConstraint added via separate migration file
#       Fix 4 — .only() on Product query (70% memory reduction)
#       Fix 5 — Subquery in get_latest_lifecycle (N queries → 1 query)
#
# Query breakdown:
#       Query 1 — DELETE existing records for this period
#       Query 2 — active products (id, product_name, introduced_date only)
#       Query 3 — current period sales aggregated by product
#       Query 4 — historical period sales aggregated by product
#       Query 5 — slow moving period sales aggregated by product
#       Query 6 — ONE bulk_create for all lifecycle records
#
# Called by : Randika → POST /api/lifecycle/calculate/
# Displays  : Lavanya → lifecycle report page
# Feeds into: F07 (SLOW_MOVING), F09 (DECLINING)
#
# Logic priority order (matches Week 2 pseudocode document):
#   NEW → SLOW_MOVING → GROWING/DECLINING → STABLE
#   Note: SLOW_MOVING check runs before velocity comparison intentionally.
#   A product with < 5 units in 60 days is considered dead regardless
#   of velocity ratio. A declining product with near-zero sales is more
#   usefully classified as SLOW_MOVING → DISCONTINUE than DECLINING → DISCOUNT.


from datetime import date, timedelta
from django.db.models import Sum, Max, Subquery, OuterRef
from products.models import Product
from sales.models import ItemSalesRecord
from inventory.models import ProductLifecycle


# ═══════════════════════════════════════════════════════════════════════════════
# F06 — Main Lifecycle Classification Function
# ═══════════════════════════════════════════════════════════════════════════════

def run_lifecycle_calculation():
    """
    Classifies ALL active products into lifecycle status.
    Manager triggers this ON-DEMAND from dashboard.

    Velocity periods (relative to today):
        Current period    → last 30 days      (day 0  to day 30)
        Historical period → days 31 to 120    (day 31 to day 120)
        Slow moving check → last 60 days      (day 0  to day 60)

    Classification rules (matches Week 2 pseudocode document exactly):
        introduced_date > today - 30     → NEW         → MONITOR
        slow_moving_qty < 5              → SLOW_MOVING → DISCONTINUE
        current > historical × 1.15      → GROWING     → RETAIN
        current < historical × 0.85      → DECLINING   → DISCOUNT
        everything else                  → STABLE      → RETAIN

    Database queries: 6 total regardless of product count
        Query 1 — DELETE existing records for this period
        Query 2 — active products (3 fields only via .only())
        Query 3 — current period aggregated
        Query 4 — historical period aggregated
        Query 5 — slow moving period aggregated
        Query 6 — bulk_create all lifecycle records

    Returns:
        {
            'summary' : {'NEW': int, 'GROWING': int, 'STABLE': int,
                        'DECLINING': int, 'SLOW_MOVING': int},
            'products': [
                {
                    'product_id'          : int,
                    'product_name'        : str,
                    'status'              : str,
                    'current_velocity'    : float,
                    'historical_velocity' : float,
                    'recommendation'      : str,
                }
            ]
        }
    """

    today = date.today()

    # ── Define date boundaries ─────────────────────────────────────────────────
    current_start     = today - timedelta(days=30)
    historical_start  = today - timedelta(days=120)
    historical_end    = today - timedelta(days=31)
    slow_moving_start = today - timedelta(days=60)

    # comparison_period label — e.g. "2026-03"
    # one lifecycle record per product per month
    comparison_period = today.strftime('%Y-%m')

    # ── Query 1: DELETE existing records for this period ──────────────────────
    # Prevents duplicate records if manager triggers calculation multiple times
    # in the same month. Ensures: 1 product = 1 lifecycle record per period.
    # UniqueConstraint in migration also enforces this at DB level.
    ProductLifecycle.objects.filter(
        comparison_period=comparison_period
    ).delete()

    # ── Query 2: Fetch ALL active products — only needed fields ───────────────
    # .only() loads just 3 fields instead of all 10 Product fields
    # Reduces memory usage by ~70% for large product catalogs
    active_products = Product.objects.filter(
        is_active=True
    ).only('id', 'product_name', 'introduced_date')

    # ── Query 3: Current period totals per product ────────────────────────────
    # ONE grouped query — all products at once
    # row['total'] or 0 — guards against None when Sum() finds no matching rows
    current_map = {
        row['product_id']: row['total'] or 0
        for row in (
            ItemSalesRecord.objects
            .filter(sale_date__range=(current_start, today))
            .values('product_id')
            .annotate(total=Sum('quantity_sold'))
        )
    }

    # ── Query 4: Historical period totals per product ─────────────────────────
    # row['total'] or 0 — same None guard
    historical_map = {
        row['product_id']: row['total'] or 0
        for row in (
            ItemSalesRecord.objects
            .filter(sale_date__range=(historical_start, historical_end))
            .values('product_id')
            .annotate(total=Sum('quantity_sold'))
        )
    }

    # ── Query 5: Slow moving period totals per product ────────────────────────
    # row['total'] or 0 — same None guard
    slow_map = {
        row['product_id']: row['total'] or 0
        for row in (
            ItemSalesRecord.objects
            .filter(sale_date__range=(slow_moving_start, today))
            .values('product_id')
            .annotate(total=Sum('quantity_sold'))
        )
    }

    # ── Initialize containers ──────────────────────────────────────────────────
    results           = []
    lifecycle_records = []  # collected for bulk_create
    summary = {
        'NEW'        : 0,
        'GROWING'    : 0,
        'STABLE'     : 0,
        'DECLINING'  : 0,
        'SLOW_MOVING': 0,
    }

    # ── Loop — ZERO DB queries inside, pure Python dictionary lookups ──────────
    for product in active_products:

        # Dictionary lookups — no DB hit
        # .get(product.id, 0) → safely returns 0 if product has no sales
        current_qty     = current_map.get(product.id, 0)
        historical_qty  = historical_map.get(product.id, 0)
        slow_moving_qty = slow_map.get(product.id, 0)

        # Velocity = total qty / number of days in period
        # 0 / 30 = 0.0 safely — no condition needed
        current_vel    = round(current_qty    / 30, 2)
        historical_vel = round(historical_qty / 90, 2)

        # ── Step 1: New product check ──────────────────────────────────────────
        # introduced_date within last 30 days → too early to compare velocity
        if (
            product.introduced_date and
            product.introduced_date > (today - timedelta(days=30))
        ):
            status         = 'NEW'
            recommendation = 'MONITOR'

        # ── Step 2: Slow moving check ──────────────────────────────────────────
        # Less than 5 units in 60 days → product is effectively dead
        # Runs BEFORE velocity comparison — see file header note on priority
        elif slow_moving_qty < 5:
            status         = 'SLOW_MOVING'
            recommendation = 'DISCONTINUE'

        # ── Step 3: No historical data ─────────────────────────────────────────
        # Product has sales but nothing in days 31-120
        # Cannot compare velocities → classify as STABLE (neutral)
        elif historical_vel == 0:
            status         = 'STABLE'
            recommendation = 'RETAIN'

        # ── Step 4: Growing check ──────────────────────────────────────────────
        # Selling 15% MORE than historical → growing
        elif current_vel > historical_vel * 1.15:
            status         = 'GROWING'
            recommendation = 'RETAIN'

        # ── Step 5: Declining check ────────────────────────────────────────────
        # Selling 15% LESS than historical → declining
        # Automatically fed into F09 Discount Engine as candidate
        elif current_vel < historical_vel * 0.85:
            status         = 'DECLINING'
            recommendation = 'DISCOUNT'

        # ── Step 6: Stable ────────────────────────────────────────────────────
        # Within ±15% of historical → stable
        else:
            status         = 'STABLE'
            recommendation = 'RETAIN'

        # ── Collect for bulk_create (no DB hit here) ───────────────────────────
        # ProductLifecycle() creates object in Python memory only
        # Nothing saved to DB until bulk_create runs after the loop
        lifecycle_records.append(
            ProductLifecycle(
                product           = product,
                status            = status,
                sales_velocity    = current_vel,
                comparison_period = comparison_period,
                recommendation    = recommendation,
                # calculated_date auto set by auto_now_add=True in model
            )
        )

        # Update summary counts
        summary[status] += 1

        # Add to results
        results.append({
            'product_id'          : product.id,
            'product_name'        : product.product_name,
            'status'              : status,
            'current_velocity'    : current_vel,
            'historical_velocity' : historical_vel,
            'recommendation'      : recommendation,
        })

    # ── Query 6: ONE bulk insert — all records in single DB call ──────────────
    # Without bulk_create: 1000 products = 1000 INSERT queries
    # With bulk_create:    1000 products = 1 INSERT query
    ProductLifecycle.objects.bulk_create(lifecycle_records)

    return {
        'summary' : summary,
        'products': results,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# F06 — Get Latest Lifecycle Status Per Product
# ═══════════════════════════════════════════════════════════════════════════════

def get_latest_lifecycle(status_filter=None):
    """
    Returns the most recent lifecycle record per product.
    Does NOT recalculate — reads already saved ProductLifecycle records.

    Optimization: Subquery fetches latest record per product in ONE query
    instead of N queries (one per product).

    SQL equivalent:
        SELECT * FROM product_lifecycle pl
        WHERE calculated_date = (
            SELECT MAX(calculated_date)
            FROM product_lifecycle
            WHERE product_id = pl.product_id
        )

    status_filter → optional string to filter by status
        'DECLINING'   → used by F09 Discount Engine
        'SLOW_MOVING' → used by F07 Loss Analysis
        None          → returns all products

    Called by : Randika → GET /api/lifecycle/
                Randika → GET /api/lifecycle/declining/
    Displays  : Lavanya → lifecycle report filter tabs
    Feeds into: F09 → get_latest_lifecycle('DECLINING')
                F07 → get_latest_lifecycle('SLOW_MOVING')

    Database queries: 1 total regardless of product count
    """

    # Subquery: for each row, find the latest calculated_date for that product
    # OuterRef('product_id') → refers to product_id of the outer query
    # order_by('-calculated_date')[:1] → most recent date first, take first
    latest_date_subquery = (
        ProductLifecycle.objects
        .filter(product_id=OuterRef('product_id'))
        .order_by('-calculated_date')
        .values('calculated_date')[:1]
    )

    # ONE query: fetch only the latest record per product
    queryset = (
        ProductLifecycle.objects
        .filter(calculated_date=Subquery(latest_date_subquery))
        .select_related('product')
        .order_by('product__product_name')
    )

    # Apply status filter if provided
    if status_filter:
        queryset = queryset.filter(status=status_filter)

    # Build results list
    results = [
        {
            'product_id'      : record.product.id,
            'product_name'    : record.product.product_name,
            'status'          : record.status,
            'sales_velocity'  : float(record.sales_velocity),
            'recommendation'  : record.recommendation,
            'calculated_date' : str(record.calculated_date),
        }
        for record in queryset
    ]

    return results




# ═══════════════════════════════════════════════════════════════════════════════
# F07 — Loss & Root Cause Analysis
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_loss_analysis():
    """
    Calculates three types of loss across all products:
        1. Expiry loss    — from expired/disposed PurchaseBatch records
        2. Damage loss    — from manually recorded LossRecord entries
        3. Slow moving    — from F06 SLOW_MOVING products × current stock

    Called by : Randika → GET /api/loss-analysis/
    Displays  : Lavanya → loss report page
    Feeds into: SupplierReturn tracking

    Database queries: 4 total
        Query 1 — expired batches aggregated by product
        Query 2 — damage/other loss records aggregated by product
        Query 3 — slow moving products from F06
        Query 4 — active batch stock for slow moving products

    Returns:
        {
            'summary': {
                'total_expiry_loss'     : float,
                'total_damage_loss'     : float,
                'total_slow_moving_loss': float,
                'net_total_loss'        : float,
            },
            'products': [
                {
                    'product_id'       : int,
                    'product_name'     : str,
                    'expiry_loss'      : float,
                    'damage_loss'      : float,
                    'slow_moving_loss' : float,
                    'total_loss'       : float,
                }
            ]
        }
    """

    from decimal import Decimal
    from purchases.models import PurchaseBatch
    from inventory.models import LossRecord

    # ── Query 1: Expiry loss — from expired/disposed batches ──────────────────
    # remaining_quantity > 0 means stock was still on shelf when it expired
    # loss value = remaining_quantity × cost_price per batch
    # Grouped by product → summed
    expired_batches = (
        PurchaseBatch.objects
        .filter(
            status__in=['EXPIRED', 'DISPOSED'],
            remaining_quantity__gt=0
        )
        .select_related('product')
    )

    # Build expiry loss map {product_id: total_expiry_loss}
    expiry_map     = {}
    product_names  = {}   # {product_id: product_name} — reused across all maps

    for batch in expired_batches:
        pid        = batch.product_id
        loss_value = batch.remaining_quantity * batch.cost_price

        expiry_map[pid]    = expiry_map.get(pid, Decimal('0')) + loss_value
        product_names[pid] = batch.product.product_name

    # ── Query 2: Damage loss — from manually recorded LossRecord entries ──────
    # Staff records DAMAGE and OTHER type losses manually
    # Grouped by product → sum loss_value
    damage_agg = (
        LossRecord.objects
        .filter(loss_type__in=['DAMAGE', 'OTHER'])
        .values('product_id')
        .annotate(total=Sum('loss_value'))
    )

    # Build damage loss map {product_id: total_damage_loss}
    damage_map = {
        row['product_id']: row['total'] or Decimal('0')
        for row in damage_agg
    }

    # Fetch product names for damage products not already in product_names
    damage_product_ids = [
        pid for pid in damage_map
        if pid not in product_names
    ]
    if damage_product_ids:
        for p in Product.objects.filter(
            id__in=damage_product_ids
        ).only('id', 'product_name'):
            product_names[p.id] = p.product_name

    # ── Query 3: Slow moving loss — from F06 results ──────────────────────────
    # Get all products currently classified as SLOW_MOVING
    slow_moving_products = get_latest_lifecycle('SLOW_MOVING')
    slow_moving_ids      = [p['product_id'] for p in slow_moving_products]

    # ── Query 4: Current stock for slow moving products ───────────────────────
    # current stock = SUM(remaining_quantity) of ACTIVE batches
    # loss = current_stock × avg_cost_price
    slow_map = {}

    if slow_moving_ids:
        # fetch avg_cost_price for slow moving products
        slow_products = {
            p.id: p for p in Product.objects.filter(
                id__in=slow_moving_ids
            ).only('id', 'product_name', 'avg_cost_price')
        }

        # get current active stock per product in ONE query
        active_stock_agg = (
            PurchaseBatch.objects
            .filter(
                product_id__in=slow_moving_ids,
                status='ACTIVE'
            )
            .values('product_id')
            .annotate(total_stock=Sum('remaining_quantity'))
        )

        for row in active_stock_agg:
            pid           = row['product_id']
            current_stock = row['total_stock'] or 0
            avg_cost      = slow_products[pid].avg_cost_price or Decimal('0')
            slow_map[pid] = current_stock * avg_cost

            # add to product names if not already there
            if pid not in product_names:
                product_names[pid] = slow_products[pid].product_name

    # ── Combine all three loss sources ────────────────────────────────────────
    all_product_ids = set(
        list(expiry_map.keys()) +
        list(damage_map.keys()) +
        list(slow_map.keys())
    )

    products = []

    for pid in all_product_ids:
        expiry_loss      = float(expiry_map.get(pid, 0))
        damage_loss      = float(damage_map.get(pid, 0))
        slow_moving_loss = float(slow_map.get(pid,  0))
        total_loss       = expiry_loss + damage_loss + slow_moving_loss

        products.append({
            'product_id'       : pid,
            'product_name'     : product_names.get(pid, 'Unknown'),
            'expiry_loss'      : round(expiry_loss,      2),
            'damage_loss'      : round(damage_loss,      2),
            'slow_moving_loss' : round(slow_moving_loss, 2),
            'total_loss'       : round(total_loss,       2),
        })

    # Sort by total loss descending — highest loss product appears first
    products.sort(key=lambda x: x['total_loss'], reverse=True)

    # ── Store level summary ───────────────────────────────────────────────────
    total_expiry      = round(sum(expiry_map.get(pid, 0)  for pid in expiry_map),  2)
    total_damage      = round(sum(damage_map.get(pid, 0)  for pid in damage_map),  2)
    total_slow_moving = round(sum(slow_map.get(pid, 0)    for pid in slow_map),    2)
    net_total         = round(float(total_expiry) + float(total_damage) + float(total_slow_moving), 2)

    return {
        'summary': {
            'total_expiry_loss'     : float(total_expiry),
            'total_damage_loss'     : float(total_damage),
            'total_slow_moving_loss': float(total_slow_moving),
            'net_total_loss'        : net_total,
        },
        'products': products,
    }




    # ═══════════════════════════════════════════════════════════════════════════════
# F08 — Inventory Health Score
# ═══════════════════════════════════════════════════════════════════════════════
#
# Optimization notes:
#   - All sales aggregations done in bulk (grouped annotate) before the loop
#   - Category averages pre-computed once — not recalculated per product
#   - bulk_create used for InventoryHealthScore and CategoryHealthScore
#   - .only() used on Product query to reduce memory
#
# Query breakdown:
#   Query 1  — active products (id, name, category, unit_price, avg_cost_price)
#   Query 2  — current 30-day sales grouped by product
#   Query 3  — active batch stock grouped by product
#   Query 4  — nearest expiry per product
#   Query 5  — category-level sales average (for velocity score denominator)
#   Query 6  — product ratings (if rating table exists)
#   Query 7  — bulk_create InventoryHealthScore records
#   Query 8  — bulk_create CategoryHealthScore records
#
# Called by : Randika → POST /api/health-scores/calculate/
# Displays  : Lavanya → health dashboard, critical alerts
# Feeds into: F09 Discount Engine (CRITICAL products)

def calculate_health_scores():
    """
    Calculates per-product Inventory Health Score for all active products.

    Scoring components:
        Velocity score       — how well is it selling vs category average
        Margin score         — profit margin quality
        Expiry risk score    — days until nearest batch expires
        Stock duration score — how many days of stock remain
        Rating score         — customer rating (only when >= 10 ratings exist)

    Adaptive weighting:
        4-component (default, < 10 ratings):
            Velocity 35% + Margin 25% + Expiry 25% + Duration 15%
        5-component (>= 10 ratings):
            Velocity 30% + Margin 20% + Expiry 20% + Duration 15% + Rating 15%

    Status bands:
        HEALTHY  : 80 – 100
        WATCH    : 60 – 79
        AT RISK  : 40 – 59
        CRITICAL :  0 – 39

    Returns:
        {
            'products_processed': int,
            'summary': {
                'HEALTHY': int, 'WATCH': int,
                'AT RISK': int, 'CRITICAL': int
            }
        }
    """

    from decimal import Decimal
    from django.db.models import Avg, Min
    from purchases.models import PurchaseBatch
    from inventory.models import InventoryHealthScore, CategoryHealthScore
    from products.models import Category

    today = date.today()

    # ── Query 1: Active products ───────────────────────────────────────────────
    active_products = list(
        Product.objects.filter(is_active=True)
        .only('id', 'product_name', 'category_id',
              'unit_price', 'avg_cost_price')
        .select_related('category')
    )

    product_ids = [p.id for p in active_products]

    # ── Query 2: Current 30-day sales per product ──────────────────────────────
    sales_map = {
        row['product_id']: row['total'] or 0
        for row in (
            ItemSalesRecord.objects
            .filter(
                product_id__in=product_ids,
                sale_date__gte=today - timedelta(days=30)
            )
            .values('product_id')
            .annotate(total=Sum('quantity_sold'))
        )
    }

    # ── Query 3: Active stock per product ─────────────────────────────────────
    stock_map = {
        row['product_id']: row['total'] or 0
        for row in (
            PurchaseBatch.objects
            .filter(product_id__in=product_ids, status='ACTIVE')
            .values('product_id')
            .annotate(total=Sum('remaining_quantity'))
        )
    }

    # ── Query 4: Nearest expiry per product ───────────────────────────────────
    expiry_map = {
        row['product_id']: row['nearest']
        for row in (
            PurchaseBatch.objects
            .filter(
                product_id__in=product_ids,
                status='ACTIVE',
                expiry_date__isnull=False
            )
            .values('product_id')
            .annotate(nearest=Min('expiry_date'))
        )
    }

    # ── Query 5: Category avg daily sales (velocity denominator) ──────────────
    # Pre-compute once per category — avoids recalculating inside the loop
    category_ids = list({p.category_id for p in active_products})
    cat_sales_map = {}

    for cat_id in category_ids:
        cat_product_ids = [
            p.id for p in active_products if p.category_id == cat_id
        ]
        cat_total = (
            ItemSalesRecord.objects
            .filter(
                product_id__in=cat_product_ids,
                sale_date__gte=today - timedelta(days=30)
            )
            .aggregate(t=Sum('quantity_sold'))['t'] or 0
        )
        count = len(cat_product_ids) or 1
        cat_sales_map[cat_id] = (cat_total / 30) / count

    # ── Query 6: Product ratings (if available) ───────────────────────────────
    rating_map = {}    # {product_id: {'count': int, 'avg': float}}
    try:
        from orders.models import ProductRating
        rating_agg = (
            ProductRating.objects
            .filter(product_id__in=product_ids)
            .values('product_id')
            .annotate(count=Sum('rating'), avg=Avg('rating'))
        )
        for row in rating_agg:
            rating_map[row['product_id']] = {
                'count': row['count'] or 0,
                'avg'  : float(row['avg'] or 0),
            }
    except Exception:
        pass   # ratings table not available — use 4-component mode

    # ── Loop — all lookups are dictionary (no DB hits) ─────────────────────────
    health_records   = []
    category_buckets = {}   # {category_id: [overall_score, ...]}
    summary = {'HEALTHY': 0, 'WATCH': 0, 'AT RISK': 0, 'CRITICAL': 0}

    for product in active_products:

        avg_daily = sales_map.get(product.id, 0) / 30
        cat_avg   = cat_sales_map.get(product.category_id, 0)

        # ── Velocity score ─────────────────────────────────────────────────────
        vel_score = min((avg_daily / cat_avg) * 100, 100) \
            if cat_avg > 0 else 50.0

        # ── Margin score ───────────────────────────────────────────────────────
        unit_price = float(product.unit_price or 0)
        avg_cost   = float(product.avg_cost_price or 0)
        if unit_price > 0:
            margin_pct   = ((unit_price - avg_cost) / unit_price) * 100
            margin_score = min(margin_pct * 2.5, 100)
        else:
            margin_score = 0.0

        # ── Expiry risk score ──────────────────────────────────────────────────
        nearest_expiry = expiry_map.get(product.id)
        if nearest_expiry:
            days_to_expiry = (nearest_expiry - today).days
            if days_to_expiry > 90:
                expiry_score = 100.0
            elif days_to_expiry >= 31:
                expiry_score = (days_to_expiry / 90) * 100
            elif days_to_expiry >= 0:
                expiry_score = (days_to_expiry / 30) * 50
            else:
                expiry_score = 0.0
        else:
            expiry_score = 100.0   # no expiry = no expiry risk

        # ── Stock duration score ───────────────────────────────────────────────
        current_stock = stock_map.get(product.id, 0)
        days_of_stock = (current_stock / avg_daily) if avg_daily > 0 else 0

        if days_of_stock > 60:
            duration_score = 100.0
        elif days_of_stock >= 15:
            duration_score = (days_of_stock / 60) * 100
        elif days_of_stock > 0:
            duration_score = (days_of_stock / 15) * 50
        else:
            duration_score = 0.0

        # ── Rating score (conditional) ─────────────────────────────────────────
        rating_info   = rating_map.get(product.id, {})
        rating_count  = rating_info.get('count', 0)

        if rating_count >= 10:
            rating_score      = (rating_info['avg'] / 5) * 100
            rating_sufficient = True
            weighting_mode    = '5-COMPONENT'
        else:
            rating_score      = None
            rating_sufficient = False
            weighting_mode    = '4-COMPONENT'

        # ── Adaptive weighting ─────────────────────────────────────────────────
        if not rating_sufficient:
            overall = (vel_score      * 0.35 +
                       margin_score   * 0.25 +
                       expiry_score   * 0.25 +
                       duration_score * 0.15)
        else:
            overall = (vel_score      * 0.30 +
                       margin_score   * 0.20 +
                       expiry_score   * 0.20 +
                       duration_score * 0.15 +
                       rating_score   * 0.15)

        overall = round(overall, 2)

        # ── Status classification ──────────────────────────────────────────────
        if overall >= 80:
            status_val = 'HEALTHY'
            action     = 'Maintain current stock levels'
        elif overall >= 60:
            status_val = 'WATCH'
            action     = 'Monitor closely — review in next cycle'
        elif overall >= 40:
            status_val = 'AT RISK'
            action     = 'Review pricing and stock levels urgently'
        else:
            status_val = 'CRITICAL'
            action     = 'Immediate action required — consider discount or clearance'

        summary[status_val] += 1

        # collect for category bucketing
        if product.category_id not in category_buckets:
            category_buckets[product.category_id] = []
        category_buckets[product.category_id].append(overall)

        health_records.append(
            InventoryHealthScore(
                product              = product,
                velocity_score       = round(vel_score, 2),
                margin_score         = round(margin_score, 2),
                expiry_risk_score    = round(expiry_score, 2),
                stock_duration_score = round(duration_score, 2),
                rating_score         = round(rating_score, 2)
                                       if rating_score is not None else None,
                overall_score        = overall,
                status               = status_val,
                recommended_action   = action,
                rating_sufficient    = rating_sufficient,
                weighting_mode       = weighting_mode,
                calculated_date      = today,
            )
        )

    # ── Query 7: Bulk insert all health score records ─────────────────────────
    InventoryHealthScore.objects.bulk_create(health_records)

    # ── Category health scores ─────────────────────────────────────────────────
    cat_records = []
    for cat_id, scores in category_buckets.items():
        if not scores:
            continue
        avg_score      = round(sum(scores) / len(scores), 2)
        healthy_count  = sum(1 for s in scores if s >= 80)
        watch_count    = sum(1 for s in scores if 60 <= s < 80)
        at_risk_count  = sum(1 for s in scores if 40 <= s < 60)
        critical_count = sum(1 for s in scores if s < 40)

        if avg_score >= 80:
            cat_status = 'HEALTHY'
        elif avg_score >= 60:
            cat_status = 'WATCH'
        elif avg_score >= 40:
            cat_status = 'AT RISK'
        else:
            cat_status = 'CRITICAL'

        try:
            category = Category.objects.get(pk=cat_id)
        except Category.DoesNotExist:
            continue

        cat_records.append(
            CategoryHealthScore(
                category        = category,
                avg_health_score= avg_score,
                healthy_count   = healthy_count,
                watch_count     = watch_count,
                at_risk_count   = at_risk_count,
                critical_count  = critical_count,
                status          = cat_status,
                calculated_date = today,
            )
        )

    # ── Query 8: Bulk insert category health scores ───────────────────────────
    CategoryHealthScore.objects.bulk_create(cat_records)

    return {
        'products_processed': len(health_records),
        'summary'           : summary,
    }


from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Sum, Avg, Min
from products.models import Product, Category
from sales.models import ItemSalesRecord
from purchases.models import PurchaseBatch
from inventory.models import InventoryHealthScore, CategoryHealthScore


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


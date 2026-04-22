from decimal import Decimal
from django.db.models import Sum
from products.models import Product
from purchases.models import PurchaseBatch
from inventory.models import LossRecord
from inventory.services.lifecycle import get_latest_lifecycle


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
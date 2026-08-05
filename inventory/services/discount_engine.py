"""
F09 — Dynamic Pricing & Discount Engine

Recommends discounts for near-expiry products. RECOMMENDATION ONLY —
this never modifies easyAcc prices directly. Manager reviews and
manually applies approved discounts in easyAcc POS.

Tier values used (API Design Document v3.0, Section 14.1):
    >90 days   = 0%  (no discount — not stored as a DiscountRule row,
                       this is simply the default when no tier matches)
    60-90 days = 5%
    30-60 days = 15%
    14-30 days = 25%
    7-14 days  = 40%
    <7 days    = 50%

THREE SCENARIOS COMPARED PER BATCH (per design doc Section 14):
    1. Sell at Discount — apply the tier's discount %, sell remaining stock
    2. Return to Supplier — recover value via SupplierReturn (if the
       supplier's return_policy allows it)
    3. Discard — total loss, remaining_quantity × cost_price

PROFIT FLOOR (prevents loss-making discount recommendations):
    profit_floor = avg_cost_price × (1 + min_margin_pct / 100)
    If a discounted price would fall below this floor, the discount
    is capped at the floor price instead of the full tier percentage.
    min_margin_pct comes from SystemConfig (seeded as 10 by default).

WHY RULE-BASED TIERS, NOT A FORMULA:
    The 6-tier table is explicitly manager-configurable per the design
    doc — managers can edit thresholds via the existing DiscountRule
    CRUD endpoints (already built by Randika) without touching code.
    A continuous formula would be more "elegant" but would remove the
    manager's ability to tune business rules without a code deploy —
    rule-based lookup matches how the rest of this system handles
    config (System Config, discount rules, reorder thresholds).

WHY return_policy AFFECTS recovery_return:
    Per API Design Document Section 6: Supplier.return_policy is used
    "for discount engine" — different suppliers accept returns
    differently. A supplier with return_policy='NO_RETURNS' offers
    zero recovery via the return path, so that scenario should not
    be presented as viable even if the math would otherwise favour it.
"""

from decimal import Decimal
from datetime import date

from purchases.models import PurchaseBatch
from inventory.models import DiscountRule, DiscountRecommendation
from users.models import SystemConfig


def _get_config_value(key, default, cast=str):
    """
    Reads one SystemConfig value, falling back to a default if the
    key doesn't exist. Mirrors the get_last_sync_date() helper pattern
    already used in inventory/views.py — same defensive try/except
    approach, so behaviour is consistent across the codebase.
    """
    try:
        config = SystemConfig.objects.get(key=key)
        return cast(config.value)
    except SystemConfig.DoesNotExist:
        return default
    except (ValueError, TypeError):
        # Stored value couldn't be cast (e.g. corrupted config row) —
        # fall back rather than crashing the whole engine over one bad row
        return default


def _find_matching_tier(days_until_expiry):
    """
    Returns the DiscountRule matching the given days_until_expiry,
    or None if no tier applies (>90 days — no discount needed).

    Queries DiscountRule.objects.filter(is_active=True) fresh each
    call rather than caching, since managers can edit tiers at any
    time via the CRUD endpoints — caching would risk using stale
    thresholds after an edit.
    """
    return DiscountRule.objects.filter(
        is_active=True,
        days_from_expiry_min__lte=days_until_expiry,
        days_from_expiry_max__gt=days_until_expiry,
    ).first()


def _calculate_recovery_sell(batch, tier, min_margin_pct):
    """
    Scenario 1: Sell at Discount.

    Applies the tier's discount %, but never below the profit floor.
    Returns (recommended_discount_pct, recommended_price, recovery_value).
    """
    cost_price = batch.cost_price or Decimal('0')
    current_price = batch.product.unit_price or Decimal('0')

    profit_floor = cost_price * (Decimal('1') + Decimal(str(min_margin_pct)) / Decimal('100'))

    tier_discount_pct = Decimal(str(tier.discount_percentage))
    tier_price = current_price * (Decimal('1') - tier_discount_pct / Decimal('100'))

    # Cap the discount at the profit floor — never recommend a price
    # that would sell below cost + minimum margin
    if tier_price < profit_floor:
        final_price = profit_floor
        # Recompute the EFFECTIVE discount % once capped, so the
        # response is honest about what's actually being recommended
        # rather than showing the tier's nominal % when it was overridden
        if current_price > 0:
            effective_discount_pct = (
                (current_price - final_price) / current_price * Decimal('100')
            ).quantize(Decimal('0.01'))
        else:
            effective_discount_pct = Decimal('0')
    else:
        final_price = tier_price
        effective_discount_pct = tier_discount_pct

    recovery_value = final_price * batch.remaining_quantity

    return effective_discount_pct, final_price.quantize(Decimal('0.01')), recovery_value.quantize(Decimal('0.01'))


def _calculate_recovery_return(batch):
    """
    Scenario 2: Return to Supplier.

    Recovery value = remaining_quantity x cost_price (full cost recovered),
    but ONLY if the supplier's return_policy allows returns. If the
    policy is NO_RETURNS or similar, recovery is 0 — this scenario
    genuinely isn't available, not just unfavourable.
    """
    supplier = batch.purchase.supplier if batch.purchase else None

    if supplier is None:
        return Decimal('0')

    policy = (supplier.return_policy or '').upper()
    if 'NO' in policy and 'RETURN' in policy:
        # Matches policies like "NO_RETURNS", "NO RETURNS", "NONE"
        return Decimal('0')

    cost_price = batch.cost_price or Decimal('0')
    return (cost_price * batch.remaining_quantity).quantize(Decimal('0.01'))


def _calculate_recovery_discard(batch):
    """
    Scenario 3: Discard.

    Always 0 recovery — total loss of remaining stock value.
    Included explicitly (rather than just implied as "the worst case")
    so the manager sees all three numbers side by side and the
    best_action logic has a real baseline to compare against.
    """
    return Decimal('0.00')


def calculate_discounts():
    """
    Runs the discount engine across all ACTIVE batches with stock
    remaining and an expiry date within the configured alert window.

    For each qualifying batch:
        1. Finds the matching DiscountRule tier (skips if none — i.e.
           batch has more than 90 days until expiry, no action needed)
        2. Calculates all 3 recovery scenarios
        3. Picks best_action = whichever scenario has the highest
           recovery value
        4. Saves a DiscountRecommendation row (or updates if one
           already exists for this batch with status=PENDING — avoids
           creating duplicate recommendations on repeated calculate
           calls before a manager has reviewed the existing one)

    Returns:
        {
            'batches_evaluated':   int,
            'recommendations_created': int,
            'recommendations_updated': int,
            'skipped_no_tier':     int,  — batches >90 days, no action needed
        }

    Called by: POST /api/discounts/calculate/ (view not yet built —
               see analytics/urls.py / inventory/urls.py for wiring,
               same pattern as HealthScoreCalculateView /
               LifecycleCalculateView)
    """
    today = date.today()
    expiry_alert_days = _get_config_value('expiry_alert_days', 30, cast=int)
    min_margin_pct     = _get_config_value('min_margin_pct', 10, cast=int)

    cutoff_date = today + __import__('datetime').timedelta(days=expiry_alert_days)

    # ── Get all ACTIVE batches within the alert window ────────────────────────
    qualifying_batches = PurchaseBatch.objects.filter(
        status='ACTIVE',
        remaining_quantity__gt=0,
        expiry_date__isnull=False,
        expiry_date__lte=cutoff_date,
    ).select_related('product', 'purchase__supplier')

    created   = 0
    updated   = 0
    skipped   = 0

    for batch in qualifying_batches:
        days_until_expiry = (batch.expiry_date - today).days
        if days_until_expiry < 0:
            days_until_expiry = 0  # already expired but still ACTIVE somehow — treat as most urgent tier

        tier = _find_matching_tier(days_until_expiry)
        if tier is None:
            # >90 days or no matching rule — no discount needed yet
            skipped += 1
            continue

        # ── Calculate all 3 scenarios ───────────────────────────────────────
        discount_pct, recommended_price, recovery_sell = _calculate_recovery_sell(
            batch, tier, min_margin_pct
        )
        recovery_return  = _calculate_recovery_return(batch)
        recovery_discard = _calculate_recovery_discard(batch)

        # ── Pick the best action — highest recovery value wins ───────────────
        scenarios = {
            'SELL_DISCOUNT':     recovery_sell,
            'RETURN': recovery_return,
            'DISCARD':           recovery_discard,
        }
        best_action = max(scenarios, key=scenarios.get)

        # profit_protected: True if the recommended sell price is at or
        # above the profit floor (i.e. the discount wasn't capped due to
        # being unable to hit minimum margin)
        cost_price = batch.cost_price or Decimal('0')
        profit_floor = cost_price * (Decimal('1') + Decimal(str(min_margin_pct)) / Decimal('100'))
        profit_protected = recommended_price >= profit_floor

        # ── Save or update — avoid duplicate PENDING recommendations ────────
        existing = DiscountRecommendation.objects.filter(
            batch=batch, status='PENDING'
        ).first()

        defaults = {
            'product':                  batch.product,
            'days_until_expiry':        days_until_expiry,
            'current_price':            batch.product.unit_price or Decimal('0'),
            'recommended_discount_pct': discount_pct,
            'recommended_price':        recommended_price,
            'profit_protected':         profit_protected,
            'recovery_sell':            recovery_sell,
            'recovery_return':          recovery_return,
            'recovery_discard':         recovery_discard,
            'best_action':              best_action,
            'calculated_date':          today,
        }

        if existing:
            for field, value in defaults.items():
                setattr(existing, field, value)
            existing.save()
            updated += 1
        else:
            DiscountRecommendation.objects.create(
                batch=batch,
                status='PENDING',
                **defaults
            )
            created += 1

    return {
        'batches_evaluated':       qualifying_batches.count(),
        'recommendations_created': created,
        'recommendations_updated': updated,
        'skipped_no_tier':         skipped,
    }
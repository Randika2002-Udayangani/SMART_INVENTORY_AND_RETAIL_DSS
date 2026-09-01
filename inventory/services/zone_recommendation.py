# ═══════════════════════════════════════════════════════════════════════════════
# F01 (Section 5.3) — Store Zone Recommendations
# ═══════════════════════════════════════════════════════════════════════════════
# CATEGORY_ZONE_GROUPS below is that decision, made explicit as a plain
# dict so it's easy to read, review, and edit. It's a first-pass grouping
# based on your actual 22 categories and common supermarket layout —
# treat it as a draft for whoever owns physical store layout (you, or
# Randika/Lavanya) to sanity-check and adjust, not a fixed answer.
#
# Called by : POST /api/zones/recommendations/calculate/
# Reads     : InventoryHealthScore (latest per product), Category, StoreZone
# Writes    : StoreZone (group zones, get_or_create — idempotent),
#             Category.default_zone, ZoneRecommendation

from products.models import Product, Category, StoreZone, ZoneRecommendation
from inventory.models import InventoryHealthScore

HIGH_VELOCITY_THRESHOLD = 70
HIGH_MARGIN_THRESHOLD = 70
LOW_VELOCITY_THRESHOLD = 30       # below this = slow-moving
NEAR_EXPIRY_THRESHOLD = 30        # expiry_risk_score below this = near expiry

# Draft grouping — review before treating as final. Categories not listed
# here are reported back as 'categories_unmapped' rather than silently
# skipped, so a new category added later doesn't quietly fall through.
CATEGORY_ZONE_GROUPS = {
    "Personal Care": ["Skin Care", "Soaps", "Oral Care", "Hair Care", "Deodorant"],
    "Household & Cleaning": ["Laundry", "Dishwashing", "Shopping Bags"],
    "Bakery & Sweets": ["Cakes & Bakery", "Chocolate", "Biscuits", "Jams & Spreads", "Jellies"],
    "Pantry & Dry Goods": ["Dry Groceries", "Spices", "Condiments", "Baking", "Cooking Oils", "Papadams"],
    "Beverages": ["Beverages"],
    "Snacks": ["Snacks"],
    "Baby & Family Care": ["Baby Products"],
}


def assign_zones_from_groups():
    """
    Creates each group zone in CATEGORY_ZONE_GROUPS if it doesn't exist
    (idempotent), then sets Category.default_zone for every category
    listed — overwrites any existing value, since this mapping is now
    the authoritative source, not a guess to defer to something else.

    Returns:
        {
            'zones_created': [zone_name, ...],
            'categories_assigned': [{'category': ..., 'zone': ...}, ...],
            'categories_unmapped': [category_name, ...],  # exist in DB,
                                                            # not in the mapping
        }
    """
    zones_created = []
    categories_assigned = []
    mapped_names = set()

    for zone_name, category_names in CATEGORY_ZONE_GROUPS.items():
        zone, was_created = StoreZone.objects.get_or_create(
            zone_name=zone_name,
            defaults={
                "description": f"Groups: {', '.join(category_names)}",
                "traffic_level": "Medium",
                "zone_type": "GENERAL",
            },
        )
        if was_created:
            zones_created.append(zone_name)

        for cat_name in category_names:
            mapped_names.add(cat_name)
            try:
                category = Category.objects.get(category_name=cat_name)
            except Category.DoesNotExist:
                continue
            if category.default_zone_id != zone.id:
                category.default_zone = zone
                category.save(update_fields=["default_zone"])
            categories_assigned.append({"category": cat_name, "zone": zone_name})

    all_names = set(Category.objects.values_list("category_name", flat=True))
    categories_unmapped = sorted(all_names - mapped_names)

    return {
        "zones_created": zones_created,
        "categories_assigned": categories_assigned,
        "categories_unmapped": categories_unmapped,
    }


def calculate_zone_recommendations():
    """
    Calculates a suggested store zone for every active product with a
    health score on record. Runs assign_zones_from_groups() first so a
    manager pressing "calculate" gets both steps in one action.

    Only writes a ZoneRecommendation row when the suggested zone differs
    from the product's current (group) zone.

    Returns:
        {
            'zone_assignment': {...},   # output of assign_zones_from_groups()
            'products_evaluated': int,
            'recommendations_created': int,
            'skipped_no_health_score': int,
            'skipped_no_current_zone': int,
        }
    """
    zone_assignment = assign_zones_from_groups()

    high_traffic_zone = StoreZone.objects.filter(zone_type="HIGH_TRAFFIC").first()
    promo_zone = StoreZone.objects.filter(zone_type="PROMOTIONAL").first()
    discount_zone = StoreZone.objects.filter(zone_type="DISCOUNT_BIN").first()

    latest_scores = {}
    for score in InventoryHealthScore.objects.select_related("product").order_by(
        "product_id", "-calculated_date", "-calculated_at"
    ):
        if score.product_id not in latest_scores:
            latest_scores[score.product_id] = score

    active_products = Product.objects.filter(is_active=True).select_related(
        "category", "category__default_zone"
    )

    created = 0
    skipped_no_score = 0
    skipped_no_current_zone = 0
    evaluated = 0
    new_recommendations = []

    for product in active_products:
        score = latest_scores.get(product.id)
        if score is None:
            skipped_no_score += 1
            continue

        evaluated += 1

        current_zone = (
            product.category.default_zone
            if product.category and product.category.default_zone
            else None
        )
        if current_zone is None:
            skipped_no_current_zone += 1
            continue

        vel = float(score.velocity_score)
        margin = float(score.margin_score)
        expiry = float(score.expiry_risk_score)

        suggested_zone = None
        reason = ""

        if expiry < NEAR_EXPIRY_THRESHOLD and promo_zone:
            suggested_zone = promo_zone
            reason = (
                f"Near-expiry risk (expiry_risk_score={expiry:.1f}) — "
                f"recommend promotional end-of-aisle placement"
            )
        elif vel < LOW_VELOCITY_THRESHOLD and discount_zone:
            suggested_zone = discount_zone
            reason = (
                f"Slow-moving (velocity_score={vel:.1f}) — "
                f"recommend discount bin placement"
            )
        elif (
            vel >= HIGH_VELOCITY_THRESHOLD
            and margin >= HIGH_MARGIN_THRESHOLD
            and high_traffic_zone
        ):
            suggested_zone = high_traffic_zone
            reason = (
                f"High velocity ({vel:.1f}) + high margin ({margin:.1f}) — "
                f"recommend high-traffic placement"
            )

        if suggested_zone is None or suggested_zone.id == current_zone.id:
            continue

        new_recommendations.append(
            ZoneRecommendation(
                product=product,
                current_zone=current_zone,
                suggested_zone=suggested_zone,
                reason=reason[:255],
                performance_score=score.overall_score,
            )
        )

    if new_recommendations:
        ZoneRecommendation.objects.bulk_create(new_recommendations)
        created = len(new_recommendations)

    return {
        "zone_assignment": zone_assignment,
        "products_evaluated": evaluated,
        "recommendations_created": created,
        "skipped_no_health_score": skipped_no_score,
        "skipped_no_current_zone": skipped_no_current_zone,
    }
"""Controlled LLM tool-calling agent for the chatbot.

The functions in this module deliberately return small JSON-safe payloads.  The
model never receives a database connection or a queryset and cannot invoke a
tool which is not in ``TOOL_HANDLERS``.
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Min, OuterRef, Q, Subquery, Sum

from inventory.services.reorder_logic import check_reorder_needs
from inventory.services.stock import get_available_stock
from inventory.services.lifecycle import get_latest_lifecycle
from inventory.services.discount_engine import get_customer_visible_discounts
from products.models import Category, Product
from sales.models import ItemSalesRecord
from .gemini import GeminiQuotaExceeded, GeminiUnavailable
from .matching import (
    _QueryResolution,
    classify_product_by_name,
    normalize_text,
    product_type_matches,
    resolve_query_type,
    score_tokens,
    set_brand_prefixes,
    tokens_cover,
    tokenize,
)
from .provider import get_provider

LOCAL_TZ = ZoneInfo("Asia/Colombo")


logger = logging.getLogger(__name__)

# Register known brand names so prefix-based brand detection works even when
# the database brand field is NULL (1230 products in the current catalogue
# have brand=None).  This is a best-effort, read-only lookup at import time;
# if the table is not yet ready (e.g. during migrations), detection simply
# stays disabled until the next import.
try:
    from products.models import Brand as _BrandModel

    _known_brands = [b.brand_name for b in _BrandModel.objects.all()]
    set_brand_prefixes(_known_brands)
except Exception:  # pragma: no cover - defensive; table may not exist yet
    pass


SYSTEM_INSTRUCTIONS = """You are the SMART INVENTORY & RETAIL DSS assistant.
For any current product, inventory, sales, lifecycle, health, reorder, expiry,
or margin fact, call a tool before answering. Database tool output is the only
source of truth for those facts: never make up a price, stock amount, sales
figure, health score, expiry date, or business-rule result. Use the fewest
tools needed.
If a tool returns no data, say so plainly. Ask a concise clarification only
when the product or requested comparison cannot be identified. Customer users
can receive public product information and availability only. Manager-only
analytics are enforced by Django; do not claim access to unavailable data.
Answer naturally and concisely, and explain the returned business-rule data
when the user asks why.

Formatting is mandatory for product-search results: give at most one short
introductory sentence, then put every result on its own line exactly as
"- **Product name** — LKR price". Never combine products into a paragraph,
never replace a list with "and more", and do not escape bullet or bold markers
with backslashes. Do not include unrelated matches merely because their names
contain a search word.

Tool selection: price or product lookup → search_products/get_product_details/
find_cheapest_product/compare_products. Current stock or availability →
get_current_stock. Whether a requested quantity can be bought →
check_purchase_quantity (it decides can_fulfill; never compute stock yourself).
Expiring products → get_expiring_products. Which of two products sells more →
compare_product_sales. Best-selling or most-sold products →
get_best_selling_products; pass a "category" argument to rank only products in
that category (e.g. "best selling cooking oils"). When several facts are
needed, call every relevant
tool before answering. Never say you lack access to sales, stock, price, or
expiry information before calling the matching tool; only say information is
unavailable after a tool returns no data or the product cannot be found, and
then say exactly what was not found. Always quote the tool's numbers as given;
never calculate, estimate, or invent figures yourself.

Questions about a whole product type or category (e.g. "what milk powders are
available?", "list the cooking oils") are searches, not single-product
lookups: call search_products with the type words as the query, and call
get_product_details only once the user asks about one specific product. When
the user corrects an earlier answer (e.g. "not chocolate, milk powder"),
treat the correction as the new search terms and stop using the previously
discussed product; earlier turns only resolve references such as "it", "that
one", or "the cheaper one". Product search tolerates plurals, extra spaces,
word order, and minor misspellings, so pass the user's key words rather than
guessing an exact catalogue name. If several similar products match, list
them and ask which one the user means instead of picking one yourself; a tool
result carrying "candidates" already lists the tied options to offer.

Discounts, deals, sale items, discounted products, today's deals, special
offers, promotions, or any question about what is on discount or on sale →
get_active_discounts — for anonymous visitors as well as logged-in customers.
Never pass the word "discount", "deal", "sale", "offer", or "promotion" to
search_products as a search term; those words are intents, not product names.
Format each returned deal on its own line exactly as
"- **Product name** — LKR original → LKR discounted (X% OFF)". If
get_active_discounts returns no deals, say there are no active discounts
available right now — never say nothing was found for the word "discount".

You cannot place orders, add products to carts, or process checkouts. Include
the following order notice only when the customer directly asks whether they
can order, buy, check out, or place an order. Never include it in product
search, price, stock, comparison, recommendation, greeting, or other replies:
"**As an inventory and retail information assistant, I cannot process online
orders or checkouts through this chat.**"
For a direct order question, then tell them to add a matching available product
to their cart from the product page to continue."""

MANAGER_TOOLS = {
    "get_low_stock_products", "get_reorder_recommendations",
    "get_product_lifecycle", "get_health_score", "get_sales_summary",
    "get_slow_moving_products", "get_profit_margin",
}

# Explicit allowlist of tools that are safe for anonymous (not logged-in)
# website visitors. Deny-by-default: any tool added in the future that is
# NOT listed here will automatically be inaccessible to anonymous users,
# even though the chatbot endpoint itself is public. Customer-specific
# tools belong in neither set — they are then blocked for anonymous users
# but still available to logged-in customers (and managers).
PUBLIC_TOOLS = frozenset({
    "search_products", "get_product_details", "find_cheapest_product",
    "compare_products", "get_current_stock", "get_expiring_products",
    "get_active_discounts",
    "compare_product_sales", "get_best_selling_products", "check_purchase_quantity",
})


def is_anonymous(user):
    """True when the caller has no authenticated identity at all.

    Covers DRF's AnonymousUser and any None user passed programmatically.
    """
    return not getattr(user, "is_authenticated", False)


def is_manager(user):
    """Use the same group policy as IsManagerOrAdmin without an HTTP request."""
    return bool(
        getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False)
             or (getattr(user, "groups", None) and user.groups.filter(name__in=["ADMIN", "MANAGER"]).exists()))
    )


def _number(value):
    return float(value) if isinstance(value, Decimal) else value


def _public_product(product, include_stock=False):
    stock = get_available_stock(product.id)
    data = {
        "id": product.id,
        "product_name": product.product_name,
        "category": product.category.category_name if product.category else None,
        "brand": product.brand.brand_name if product.brand else None,
        "selling_price": float(product.unit_price),
        "is_available": stock > 0,
    }
    if include_stock:
        data["current_stock"] = stock
    return data


def _product_match_tokens(product):
    """Normalised (name) and brand/category tokens for one product."""
    name_tokens = tokenize(product.product_name)
    aux_tokens = tokenize(product.brand.brand_name if product.brand else "")
    aux_tokens += tokenize(product.category.category_name if product.category else "")
    return name_tokens, aux_tokens


def _product_type_matches(product, product_type):
    """True when *product* is classified as *product_type* by name/category."""
    if not product_type:
        return True
    return product_type_matches(
        product.product_name,
        product.category.category_name if product.category else None,
        product_type,
    )


def _search_ranked(query="", category="", max_price=None, limit=None, product_type=None):
    """Active products matching *query* and/or *category*, best matches first.

    Matching uses the shared normalised token strategy (orders.services.
    matching) instead of one ``LIKE`` substring, so plurals, word order,
    extra spacing, accents and minor misspellings behave consistently across
    every tool.  Every query word must be covered by the product's name,
    brand or category words; exact name matches rank first and category-only
    matches last, so "milk powder" can never return a chocolate malt drink
    that merely sits near powders in the catalogue.  Relevance ties are
    broken deterministically by price then name — never by database order.
    When *product_type* is given, only products whose name/category evidence
    supports that type are returned — this keeps coconut milk, chocolate milk
    and liquid milk from leaking into a "milk powder" search.  The catalogue
    is small enough to score in Python; the SQL side still applies the
    active/price constraints.
    """
    query_tokens = tokenize(query)
    category_tokens = tokenize(category)
    products = Product.objects.filter(is_active=True).select_related("category", "brand")
    if max_price is not None:
        try:
            max_price_value = Decimal(str(max_price))
        except (ArithmeticError, ValueError, TypeError):
            raise ValueError("max_price must be a number.")
        products = products.filter(unit_price__lte=max_price_value)
    scored = []
    for product in products:
        if not _product_type_matches(product, product_type):
            continue
        name_tokens, aux_tokens = _product_match_tokens(product)
        if category_tokens and not tokens_cover(category_tokens, aux_tokens):
            continue
        score = score_tokens(query_tokens, name_tokens, aux_tokens)
        if score is None:
            continue
        scored.append((score, product))
    scored.sort(key=lambda item: (tuple(-value for value in item[0]),
                                  item[1].unit_price, str(item[1].product_name)))
    return [product for _, product in (scored if limit is None else scored[:limit])]


def _unresolved_product_reply(candidates=()):
    """Safe payload when a single product could not be identified.

    Ambiguity is reported with the tied candidate names so the assistant can
    ask a short clarification instead of silently guessing a product; a true
    no-match simply says so.
    """
    if candidates:
        return {
            "product": None,
            "message": ("More than one product matches that name: "
                        + ", ".join(candidates)
                        + ". Ask the user which one they mean."),
            "candidates": list(candidates),
        }
    return {"product": None, "message": "No matching active product was found."}


def search_products(arguments, user):
    query = arguments.get("query") or ""
    category = arguments.get("category") or ""
    resolution = resolve_query_type(query or category)
    products = _search_ranked(
        query,
        category,
        arguments.get("max_price"),
        product_type=resolution.product_type,
    )
    if not products and category:
        # The category row may be missing, or the model may have passed the
        # product type only as "category". Retry using the user's words as
        # plain search text (query first, else the category phrase) rather
        # than reporting nothing when the catalogue does contain matches.
        products = _search_ranked(
            query or category, "", arguments.get("max_price"),
            product_type=resolution.product_type,
        )
    payload = {
        "products": [_public_product(p) for p in products],
        "count": len(products),
    }
    if resolution.product_type:
        payload["product_type"] = resolution.product_type
        payload["product_type_confidence"] = resolution.confidence
    if not products:
        payload["message"] = "No active products matched that search. Try different or fewer keywords."
    return payload


def get_product_details(arguments, user):
    product_id = arguments.get("product_id")
    if product_id:
        try:
            product_id = int(product_id)
        except (TypeError, ValueError):
            raise ValueError("product_id must be a whole number.")
        product = Product.objects.filter(is_active=True).select_related(
            "category", "brand").filter(pk=product_id).first()
        if not product:
            return {"product": None, "message": "No matching active product was found."}
        return {"product": _public_product(product, include_stock=is_manager(user))}
    product, candidates = _resolve_product_with_candidates(arguments.get("product_name", ""))
    if not product:
        return _unresolved_product_reply(candidates)
    return {"product": _public_product(product, include_stock=is_manager(user))}


def find_cheapest_product(arguments, user):
    query = arguments.get("query", "")
    category = arguments.get("category", "")
    resolution = resolve_query_type(query or category)
    products = _search_ranked(
        query, category,
        product_type=resolution.product_type,
    )
    if not products and category:
        # Same fallback as search_products: a missing/misnamed category row
        # must not hide products that the user's own words clearly match.
        products = _search_ranked(
            query or category, "",
            product_type=resolution.product_type,
        )
    available_only = arguments.get("available_only", False)
    result = []
    for product in products:
        data = _public_product(product, include_stock=is_manager(user))
        if not available_only or data["is_available"]:
            result.append(data)
    if not result:
        return {"product": None, "alternatives": [],
                "message": "No matching active products were found."}
    return {"product": result[0], "alternatives": result[1:5]}


def compare_products(arguments, user):
    names = arguments.get("product_names") or []
    if not isinstance(names, (list, tuple)):
        raise ValueError("product_names must be a list of product names.")
    requested_type = arguments.get("product_type")
    if requested_type and requested_type not in {"milk_family", *_PRODUCT_TYPE_NAMES}:
        raise ValueError("product_type must be a supported product type.")
    products, missing, unresolved_candidates, resolved_rows = [], [], {}, []
    unverified_type = False
    for name in names[:5]:
        resolved, candidates, uncertain = _resolve_comparison_candidate(name, requested_type)
        unverified_type = unverified_type or uncertain
        if resolved:
            products.append(_public_product(resolved, include_stock=is_manager(user)))
            resolved_rows.append(resolved)
        else:
            missing.append(str(name))
            if candidates:
                unresolved_candidates[str(name)] = candidates
    comparison_error = _comparison_compatibility_error(resolved_rows)
    result = {"products": products, "missing": missing}
    if unresolved_candidates:
        result["candidates"] = unresolved_candidates
    if comparison_error:
        result["message"] = comparison_error
    elif unverified_type:
        result["message"] = (
            "The matched catalogue records do not contain enough type information to verify the requested "
            "product type. Their names and pack sizes are shown for confirmation."
        )
    return result


def get_current_stock_tool(arguments, user):
    product, candidates = _resolve_product_with_candidates(arguments.get("product_name", ""))
    if not product:
        return _unresolved_product_reply(candidates)
    return {"product": _public_product(product, include_stock=is_manager(user))}


def get_expiring_products(arguments, user):
    """Public near-expiry product information.

    Preserves the expiry-query functionality dev1 implemented in the old
    rule-based chatbot (orders/chatbot.py handle_expiry_query): active
    products with a non-depleted ACTIVE/PENDING_EXPIRY batch expiring
    within a bounded window, ordered by earliest expiry. Customers and
    staff may both use it; it exposes no unrestricted database access.
    """
    days = arguments.get("days") or 30
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("days must be a whole number of days.")
    days = max(1, min(days, 90))
    today = datetime.now(LOCAL_TZ).date()
    # One combined Q so the row filter and the Min() aggregate apply to the
    # SAME joined batch row (chained .filter() calls would create separate
    # joins and let a product qualify via two different batches).
    batch_filter = Q(
        purchasebatch__status__in=["ACTIVE", "PENDING_EXPIRY"],
        purchasebatch__remaining_quantity__gt=0,
        purchasebatch__expiry_date__isnull=False,
        purchasebatch__expiry_date__gte=today,
        purchasebatch__expiry_date__lte=today + timedelta(days=days),
    )
    products = Product.objects.filter(
        is_active=True,
    ).filter(batch_filter).annotate(
        earliest_expiry=Min("purchasebatch__expiry_date", filter=batch_filter)
    ).select_related("category", "brand").distinct().order_by("earliest_expiry", "id")
    if arguments.get("product_name"):
        products = products.filter(product_name__icontains=arguments["product_name"])
    return {"window_days": days, "products": [
        {**_public_product(product),
         "expiry_date": str(product.earliest_expiry),
         "days_until_expiry": (product.earliest_expiry - today).days}
        for product in products[:10]
    ]}


def get_active_discounts(arguments, user):
    """Customer-visible active discounts (today's deals / promotions).

    Queries get_customer_visible_discounts() — the SAME shared definition
    used by GET /api/discounts/active/ and the Products page's "Today's
    Deals" section — so the API, the page and the chatbot can never
    disagree. Allowed for anonymous visitors (registered in PUBLIC_TOOLS);
    the payload is customer-safe only: original price, discount percentage
    and discounted price, with no recovery values, margin data, reviewer
    identity, or any other internal recommendation field.
    """
    discounts = get_customer_visible_discounts()
    return {"discounts": [
        {
            "product_id": rec.product_id,
            "product_name": rec.product.product_name,
            "category": (rec.product.category.category_name
                         if rec.product.category else None),
            "selling_price": float(rec.product.unit_price),
            "discount_percentage": float(rec.recommended_discount_pct),
            "discounted_price": float(rec.recommended_price),
            "expiry_date": str(rec.batch.expiry_date),
        }
        for rec in discounts[:20]
    ]}


def _sales_period(arguments):
    """Shared sales-window parsing — mirrors the analytics default of the
    last 30 days (analytics.views._parse_date_range) so the chatbot never
    invents a different definition of a sales period from the dashboard."""
    end = date.fromisoformat(arguments["date_to"]) if arguments.get("date_to") else datetime.now(LOCAL_TZ).date()
    start = date.fromisoformat(arguments["date_from"]) if arguments.get("date_from") else end - timedelta(days=30)
    if start > end:
        raise ValueError("date_from must be on or before date_to.")
    return start, end


def _resolve_product_with_candidates(name, limit=5, product_type=None):
    """Resolve a single active product from a (possibly natural) name.

    Matching is deterministic and never relies on database row order:

      1. An exact case-insensitive match on the whole product name wins.
      2. Then a normalised whole-name match (accents, punctuation, spacing).
      3. Otherwise the size-normalised, stopword-stripped, plural-folded
         tokens of the query must be covered by the product's name, brand or
         category tokens, with conservative typo tolerance.  Among the
         candidates the strongest signal wins — most exactly matched name
         tokens first, then fuzzy matches, then brand/category-only matches,
         with the fewest surplus (non-query) name tokens — which surfaces the
         most specific product first and keeps similarly named products from
         unrelated product types from winning.
      4. Two candidates tied on that signal are equally plausible; the
         function returns ``None`` (and their names) instead of silently
         picking an arbitrary database row, so callers can ask a short
         clarification.

    Returns ``(product_or_None, candidate_names)``.
    """
    if not name:
        return None, []
    name = str(name).strip()
    if not name:
        return None, []
    products = Product.objects.filter(is_active=True).select_related("category", "brand")
    if product_type:
        products = [p for p in products if _product_type_matches(p, product_type)]

    # 1. Exact match — highest priority (deterministic order for duplicates).
    exact = sorted((p for p in products if p.product_name.lower() == name.lower()), key=lambda p: p.id)
    if len(exact) == 1:
        return exact[0], [exact[0].product_name]
    if len(exact) > 1:
        return None, [p.product_name for p in exact[:limit]]

    # 2. Normalised whole-name match ("MILO's" ~ "Milo", "Nestlé" ~ "Nestle").
    normalized = normalize_text(name)
    normalised_matches = [p for p in products if normalize_text(p.product_name) == normalized]
    if normalised_matches:
        if len(normalised_matches) == 1:
            return normalised_matches[0], [normalised_matches[0].product_name]
        return None, [p.product_name for p in normalised_matches[:limit]]

    # 3. Token-aware scoring with conservative typo tolerance.
    query_tokens = tokenize(name)
    if not query_tokens:
        return None, []

    candidates = []
    for product in products:
        name_tokens, aux_tokens = _product_match_tokens(product)
        score = score_tokens(query_tokens, name_tokens, aux_tokens)
        if score is not None:
            candidates.append((product, score))

    if not candidates:
        return None, []
    # Deterministic ranking; ties are ambiguous → refuse to guess.
    candidates.sort(key=lambda item: (tuple(-value for value in item[1]),
                                      str(item[0].product_name)))
    best_score = candidates[0][1]
    best = [product for product, score in candidates if score == best_score]
    if len(best) > 1:
        return None, [product.product_name for product in best[:limit]]
    return best[0], [best[0].product_name]


def _resolve_product(name):
    """Resolve one product, or ``None`` when missing/ambiguous.

    Thin wrapper over _resolve_product_with_candidates kept for callers that
    only need the product itself.
    """
    product, _candidates = _resolve_product_with_candidates(name)
    return product


def _resolve_comparison_candidate(name, expected_type=None):
    """Resolve with a strict type filter, exposing exact untyped rows as uncertain.

    This narrow fallback preserves comparisons such as legacy brand/size rows
    whose catalogue type is absent, while making the missing evidence visible.
    It never relaxes a row that has a conflicting known type or guesses among
    multiple names.
    """
    product, candidates = _resolve_product_with_candidates(name, product_type=expected_type)
    if product or not expected_type:
        return product, candidates, False
    exact, exact_candidates = _resolve_product_with_candidates(name)
    if not exact:
        return None, candidates or exact_candidates, False
    detected, _confidence = classify_product_by_name(
        exact.product_name, exact.category.category_name if exact.category else None)
    requested = resolve_query_type(name).product_type
    size = _product_size(exact)
    # Only an exact, unique record with no contradictory type and a known pack
    # size can be offered as an uncertain candidate for confirmation.
    if detected is None and size and (requested is None or requested == expected_type):
        return exact, [exact.product_name], True
    return None, candidates or exact_candidates, False


_PRODUCT_TYPE_NAMES = frozenset({
    "milk_powder", "liquid_milk", "yoghurt", "coconut_milk", "chocolate_milk",
    "milk_chocolate", "drinking_yoghurt", "tea", "cooking_oil", "rice", "rice_pasta",
})


def _product_size(product):
    return resolve_query_type(product.product_name).size


def _comparison_compatibility_error(products):
    """Return a safe explanation when known type/size attributes conflict."""
    typed = []
    for product in products:
        kind, _confidence = classify_product_by_name(
            product.product_name, product.category.category_name if product.category else None)
        typed.append((product, kind, _product_size(product)))
    known_types = {kind for _product, kind, _size in typed if kind}
    if len(known_types) > 1:
        return "These products are different product types. Please choose products of the same type."
    sizes = {size for _product, _kind, size in typed if size}
    if len(sizes) > 1:
        labels = ", ".join(f"{p.product_name} ({size or 'pack size unknown'})" for p, _kind, size in typed)
        return "The matched products have different pack sizes: " + labels + ". Please choose equivalent sizes or confirm this comparison."
    return None


def _matching_category_ids(text):
    """IDs of categories whose names cover *text* with the shared matcher.

    Returns ``None`` when *text* carries no searchable words (no filter).
    Matching whole tokens ("cooking oil" ↔ "Cooking Oils") instead of one
    substring keeps singular/plural phrasing consistent with product search.
    """
    tokens = tokenize(text)
    if not tokens:
        return None
    return [category.id for category in Category.objects.all()
            if tokens_cover(tokens, tokenize(category.category_name))]


def _units_sold(product_id, start, end):
    """Units sold in the period — the same Sum(quantity_sold) the
    analytics dashboard uses for 'units sold'."""
    return ItemSalesRecord.objects.filter(
        product_id=product_id, sale_date__range=(start, end)
    ).aggregate(units=Sum("quantity_sold"))["units"] or 0


def compare_product_sales(arguments, user):
    """Deterministic units-sold comparison between two products.

    Uses ItemSalesRecord (the same source the analytics dashboard uses)
    over the standard 30-day analysis period unless explicit dates are
    given. Returns units only; the backend decides which product sells
    more — Gemini only explains the structured result.
    """
    name_a = str(arguments.get("product_a") or "").strip()
    name_b = str(arguments.get("product_b") or "").strip()
    if not name_a or not name_b:
        raise ValueError("Both product_a and product_b are required.")
    start, end = _sales_period(arguments)
    requested_type = arguments.get("product_type")
    if requested_type and requested_type not in {"milk_family", *_PRODUCT_TYPE_NAMES}:
        raise ValueError("product_type must be a supported product type.")
    resolution_a = resolve_query_type(name_a)
    resolution_b = resolve_query_type(name_b)
    type_a_requested = requested_type or resolution_a.product_type
    type_b_requested = requested_type or resolution_b.product_type
    product_a, candidates_a, uncertain_a = _resolve_comparison_candidate(name_a, type_a_requested)
    product_b, candidates_b, uncertain_b = _resolve_comparison_candidate(name_b, type_b_requested)
    missing = [n for n, p in ((name_a, product_a), (name_b, product_b)) if p is None]
    if missing:
        return {"period": {"date_from": str(start), "date_to": str(end)},
                "not_found": missing,
                "candidates": {name_a: candidates_a, name_b: candidates_b},
                "message": "A unique product could not be confirmed for: " + ", ".join(missing) + ". Please clarify the product name or size."}

    # Only compare products of the same type.  Classify both by name/category
    # evidence and refuse cross-type comparisons (e.g. milk powder vs yoghurt)
    # with a structured response instead of claiming one is "better selling"
    # on incomparable products.
    type_a, _conf_a = classify_product_by_name(
        product_a.product_name,
        product_a.category.category_name if product_a.category else None,
    )
    type_b, _conf_b = classify_product_by_name(
        product_b.product_name,
        product_b.category.category_name if product_b.category else None,
    )
    if type_a and type_b and type_a != type_b:
        return {
            "period": {"date_from": str(start), "date_to": str(end)},
            "product_a": {"name": product_a.product_name, "type": type_a},
            "product_b": {"name": product_b.product_name, "type": type_b},
            "better_selling": None,
            "difference_units": None,
            "message": (
                f"Cannot compare {type_a.replace('_', ' ')} '{product_a.product_name}' "
                f"with {type_b.replace('_', ' ')} '{product_b.product_name}' — "
                "they are different product types. Please specify products of the same type."
            ),
        }

    size_a, size_b = _product_size(product_a), _product_size(product_b)
    if size_a and size_b and size_a != size_b:
        return {
            "period": {"date_from": str(start), "date_to": str(end)},
            "product_a": {"name": product_a.product_name, "pack_size": size_a},
            "product_b": {"name": product_b.product_name, "pack_size": size_b},
            "better_selling": None,
            "difference_units": None,
            "message": "The products have different pack sizes. Please specify equivalent sizes or confirm that you want this comparison.",
        }
    if (uncertain_a or uncertain_b) and not (uncertain_a and uncertain_b and size_a and size_b and size_a == size_b):
        return {
            "period": {"date_from": str(start), "date_to": str(end)},
            "product_a": {"name": product_a.product_name, "type": type_a, "pack_size": size_a},
            "product_b": {"name": product_b.product_name, "type": type_b, "pack_size": size_b},
            "better_selling": None,
            "difference_units": None,
            "message": "The catalogue does not verify that these products share the requested type. Please confirm the products before comparing sales.",
        }

    units_a = _units_sold(product_a.id, start, end)
    units_b = _units_sold(product_b.id, start, end)
    if units_a > units_b:
        better = product_a.product_name
    elif units_b > units_a:
        better = product_b.product_name
    else:
        better = None
    return {
        "period": {"date_from": str(start), "date_to": str(end)},
        "product_a": {"name": product_a.product_name, "units_sold": units_a},
        "product_b": {"name": product_b.product_name, "units_sold": units_b},
        "better_selling": better,
        "difference_units": abs(units_a - units_b),
        **({"message": "The catalogue does not record product type for both exact records; sales are compared at the same recorded pack size."}
           if uncertain_a and uncertain_b else {}),
    }


def get_best_selling_products(arguments, user):
    """Top products ranked by units sold over the standard sales period.

    Units are customer-safe; revenue is included only for managers so
    sensitive business analytics stay behind the existing role policy.

    An optional ``category`` constraint narrows the ranking to products in
    the matching Category (filtered through the real Category relationship,
    not a name substring hack on products).  When it is omitted the existing
    all-products behaviour is preserved.
    """
    start, end = _sales_period(arguments)
    try:
        limit = int(arguments.get("limit") or 5)
    except (TypeError, ValueError):
        raise ValueError("limit must be a whole number.")
    limit = max(1, min(limit, 10))
    category = (arguments.get("category") or "").strip()

    sale_filter = Q(sale_date__range=(start, end))
    if category:
        # Filter through the actual Category relationship so only products in
        # that category are aggregated/ranked; products with category=NULL are
        # naturally excluded by the inner join.  The category name is matched
        # with the shared token strategy ("cooking oil" ↔ "Cooking Oils") so
        # singular/plural phrasing behaves like product search does.
        category_ids = _matching_category_ids(category)
        if category_ids is None:
            category_ids = []
        sale_filter &= Q(product__category_id__in=category_ids)

    rows = (ItemSalesRecord.objects.filter(sale_filter)
            .values("product_id")
            .annotate(units=Sum("quantity_sold"), revenue=Sum("total_amount"))
            .order_by("-units", "product_id")[:limit])
    product_map = {p.id: p for p in Product.objects.filter(
        id__in=[row["product_id"] for row in rows]).select_related("brand", "category")}
    include_revenue = is_manager(user)
    products = []
    for row in rows:
        product = product_map.get(row["product_id"])
        if not product:
            continue
        entry = {"product_name": product.product_name, "units_sold": row["units"]}
        if include_revenue:
            entry["revenue"] = _number(row["revenue"] or 0)
        products.append(entry)
    return {"period": {"date_from": str(start), "date_to": str(end)}, "products": products}


def check_purchase_quantity(arguments, user):
    """Purchase feasibility check against real sellable stock.

    Uses inventory.services.stock.get_available_stock() — the single
    authoritative stock implementation — and decides can_fulfill in the
    backend. Read-only: no order is created and no stock is reserved.

    Product resolution goes through _resolve_product() so a natural-language
    request (e.g. "Milk Budget" or "1kg Fortune Vegetable Oil") resolves to
    the right product rather than the first database row.
    """
    product = _resolve_product(str(arguments.get("product_name") or "").strip())
    if not product:
        return {"product": None, "message": "No matching active product was found."}
    try:
        requested = int(arguments.get("quantity"))
    except (TypeError, ValueError):
        raise ValueError("quantity must be a whole number of units.")
    if requested <= 0:
        raise ValueError("quantity must be a positive whole number.")
    available = get_available_stock(product.id)
    return {
        "product": product.product_name,
        "requested_quantity": requested,
        "available_quantity": available,
        "can_fulfill": available >= requested,
        "shortfall": max(0, requested - available),
        "remaining": max(0, available - requested),
    }


def get_low_stock_products(arguments, user):
    from inventory.views import LowStockView
    # Keep the existing deterministic view logic as the source of truth.
    response = LowStockView().get(None)
    return response.data


def get_reorder_recommendations(arguments, user):
    return {"recommendations": check_reorder_needs()[:20]}


def get_product_lifecycle(arguments, user):
    product_name = arguments.get("product_name", "")
    rows = get_latest_lifecycle(arguments.get("status"))
    if product_name:
        rows = [row for row in rows if product_name.lower() in row["product_name"].lower()]
    return {"lifecycle": rows[:20]}


def get_health_score(arguments, user):
    from inventory.models import InventoryHealthScore
    latest_id = InventoryHealthScore.objects.filter(product_id=OuterRef("product_id")).order_by("-calculated_date", "-id").values("id")[:1]
    scores = InventoryHealthScore.objects.filter(id__in=Subquery(latest_id)).select_related("product")
    if arguments.get("product_name"):
        scores = scores.filter(product__product_name__icontains=arguments["product_name"])
    return {"health_scores": [{
        "product_name": score.product.product_name, "overall_score": float(score.overall_score),
        "status": score.status, "recommended_action": score.recommended_action,
        "calculated_date": str(score.calculated_date),
    } for score in scores.order_by("overall_score")[:20]]}


def _date_range(arguments):
    end = date.fromisoformat(arguments["date_to"]) if arguments.get("date_to") else datetime.now(LOCAL_TZ).date()
    start = date.fromisoformat(arguments["date_from"]) if arguments.get("date_from") else end - timedelta(days=30)
    if start > end:
        raise ValueError("date_from must be on or before date_to.")
    return start, end


def get_sales_summary(arguments, user):
    start, end = _date_range(arguments)
    records = ItemSalesRecord.objects.filter(sale_date__range=(start, end))
    totals = records.aggregate(units_sold=Sum("quantity_sold"), revenue=Sum("total_amount"))
    return {"period": {"date_from": str(start), "date_to": str(end)}, "units_sold": totals["units_sold"] or 0, "revenue": _number(totals["revenue"] or 0)}


def get_slow_moving_products(arguments, user):
    from analytics.views import _parse_date_range
    from sales.services.profit_engine import slow_moving
    # The service is the existing analytics calculation; dates are validated locally.
    start, end = _date_range(arguments)
    return {"period": {"date_from": str(start), "date_to": str(end)}, "products": slow_moving(start, end)[:20]}


def get_profit_margin(arguments, user):
    start, end = _date_range(arguments)
    records = ItemSalesRecord.objects.filter(sale_date__range=(start, end))
    revenue = records.aggregate(value=Sum("total_amount"))["value"] or Decimal("0")
    product_units = records.values("product_id").annotate(units=Sum("quantity_sold"))
    products = {p.id: p for p in Product.objects.filter(id__in=[row["product_id"] for row in product_units])}
    cost = sum(Decimal(str(row["units"])) * (products[row["product_id"]].avg_cost_price or products[row["product_id"]].cost_price or 0) for row in product_units)
    profit = revenue - cost
    return {"period": {"date_from": str(start), "date_to": str(end)}, "revenue": float(revenue), "cost": float(cost), "profit": float(profit), "margin_percent": round(float(profit / revenue * 100), 2) if revenue else 0}


TOOL_HANDLERS = {
    "search_products": search_products, "get_product_details": get_product_details,
    "find_cheapest_product": find_cheapest_product, "compare_products": compare_products,
    "get_current_stock": get_current_stock_tool, "get_expiring_products": get_expiring_products,
    "get_active_discounts": get_active_discounts,
    "compare_product_sales": compare_product_sales, "get_best_selling_products": get_best_selling_products,
    "check_purchase_quantity": check_purchase_quantity,
    "get_low_stock_products": get_low_stock_products,
    "get_reorder_recommendations": get_reorder_recommendations, "get_product_lifecycle": get_product_lifecycle,
    "get_health_score": get_health_score, "get_sales_summary": get_sales_summary,
    "get_slow_moving_products": get_slow_moving_products, "get_profit_margin": get_profit_margin,
}


def _tool(name, description, properties, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


TOOLS = [
    _tool("search_products", "Search active products by free text (product type, key words, brand, size) and/or by category, with optional max price. Use for any question about a whole product type or category (e.g. available milk powders) and for price lookups where several products could match; tolerates plurals, word order, and minor misspellings.", {"query": {"type": "string"}, "category": {"type": "string"}, "max_price": {"type": "number"}}),
    _tool("get_product_details", "Get current details for one specific product by product_id or product_name. If the result includes candidates, the name was ambiguous: ask the user which product they mean.", {"product_id": {"type": "integer"}, "product_name": {"type": "string"}}),
    _tool("find_cheapest_product", "Find the lowest priced matching active product within search words or a category.", {"query": {"type": "string"}, "category": {"type": "string"}, "available_only": {"type": "boolean"}}),
    _tool("compare_products", "Compare named products using current database data. Preserve product type and equivalent pack size; pass product_type when the customer specified one.", {"product_names": {"type": "array", "items": {"type": "string"}}, "product_type": {"type": "string"}}, ("product_names",)),
    _tool("get_current_stock", "Get current product availability; exact stock is manager-only.", {"product_name": {"type": "string"}}, ("product_name",)),
    _tool("get_expiring_products", "List active products with in-stock batches expiring within a number of days (default 30, max 90).", {"product_name": {"type": "string"}, "days": {"type": "integer"}}),
    _tool("get_active_discounts", "List current customer-visible active discounts (today's deals): manager-approved discounted products with original price, discounted price, and discount percentage. Use for ANY question about discounts, discounted products, what is on sale, deals, offers, or promotions — never treat the word 'discount' as a product search term.", {}),
    _tool("compare_product_sales", "Compare units sold for two named products over the standard sales period (last 30 days unless dates given). Preserve product type and equivalent pack sizes; pass product_type when the customer specified one.", {"product_a": {"type": "string"}, "product_b": {"type": "string"}, "product_type": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}}, ("product_a", "product_b")),
    _tool("get_best_selling_products", "List the best-selling products by units sold over the standard sales period (last 30 days unless dates given). Pass an optional category name to rank only products in that category.", {"limit": {"type": "integer"}, "category": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}}),
    _tool("check_purchase_quantity", "Check whether a requested quantity of a product can be fulfilled from current sellable stock; read-only, does not place an order.", {"product_name": {"type": "string"}, "quantity": {"type": "integer"}}, ("product_name", "quantity")),
    *[_tool(name, description, props) for name, description, props in [
        ("get_low_stock_products", "List products below their configured reorder threshold.", {}),
        ("get_reorder_recommendations", "Run the existing deterministic reorder recommendation service.", {}),
        ("get_product_lifecycle", "Get latest saved lifecycle classifications.", {"product_name": {"type": "string"}, "status": {"type": "string"}}),
        ("get_health_score", "Get latest inventory health scores.", {"product_name": {"type": "string"}}),
        ("get_sales_summary", "Summarize sales over a date range.", {"date_from": {"type": "string"}, "date_to": {"type": "string"}}),
        ("get_slow_moving_products", "Get existing slow-moving analytics for a date range.", {"date_from": {"type": "string"}, "date_to": {"type": "string"}}),
        ("get_profit_margin", "Get deterministic WAC-based store profit margin.", {"date_from": {"type": "string"}, "date_to": {"type": "string"}}),
    ]],
]


class AgentUnavailable(Exception):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


def _safe_provider_error(exc):
    """Return diagnostics without ever recording configured secret values."""
    message = str(exc)
    # Some third-party clients may include request details in exceptions.
    # Redact the configured value defensively without printing it.
    from os import getenv
    api_key = getenv("GEMINI_API_KEY")
    if api_key:
        message = message.replace(api_key, "[redacted]")
    return message[:1000]


def _safe_tool_arguments(arguments):
    """Short, secret-free summary of tool arguments for log diagnostics."""
    try:
        return str(dict(arguments))[:300]
    except (TypeError, ValueError):
        return repr(arguments)[:300]


def execute_tool(name, arguments, user):
    if name not in TOOL_HANDLERS:
        logger.warning("Chatbot tool rejected: unknown tool %r", str(name)[:100])
        return {"error": "Unknown tool requested."}, "rejected"
    if name in MANAGER_TOOLS and not is_manager(user):
        logger.info("Chatbot tool %s denied: actor is not a manager.", name)
        return {"error": "This information is available only to managers and administrators."}, "forbidden"
    if is_anonymous(user) and name not in PUBLIC_TOOLS:
        # Deny-by-default for anonymous visitors: only explicitly public
        # tools are reachable without any authenticated identity.
        logger.info("Chatbot tool %s denied: anonymous access.", name)
        return {"error": "This information requires you to be logged in."}, "forbidden"
    try:
        return TOOL_HANDLERS[name](arguments, user), "completed"
    except (ValueError, TypeError) as exc:
        # Argument problems are the most common tool failure — log the stage
        # and arguments (user text only; never provider secrets) so they are
        # diagnosable from server logs instead of hidden behind the fallback.
        logger.warning(
            "Chatbot tool %s rejected its arguments (%s): %s | args=%s",
            name, type(exc).__name__, str(exc)[:300], _safe_tool_arguments(arguments),
        )
        return {"error": str(exc)}, "failed"
    except Exception:
        # Do not expose implementation/database details to the model or caller.
        logger.exception(
            "Chatbot tool %s raised an unexpected error | args=%s",
            name, _safe_tool_arguments(arguments),
        )
        return {"error": "The requested data is temporarily unavailable."}, "failed"


def run_agent(message, user, history=None):
    """Run a bounded provider-neutral tool loop and return safe metadata."""
    try:
        provider = get_provider(SYSTEM_INSTRUCTIONS)
    except GeminiUnavailable as exc:
        raise AgentUnavailable(str(exc)) from exc

    contents = provider.make_contents(history, message)
    if is_manager(user):
        available_tools = TOOLS
    elif is_anonymous(user):
        # Anonymous visitors are offered only the explicitly public tools.
        available_tools = [tool for tool in TOOLS if tool["name"] in PUBLIC_TOOLS]
    else:
        available_tools = [tool for tool in TOOLS if tool["name"] not in MANAGER_TOOLS]
    tool_events = []
    try:
        for _ in range(4):
            response = provider.generate(contents, available_tools)
            calls = provider.function_calls(response)
            if not calls:
                return {"bot_response": provider.response_text(response) or "I couldn't produce a response.", "tool_calls": tool_events, "query_success": True}
            results = []
            for call in calls:
                arguments = getattr(call, "args", None)
                if not isinstance(arguments, dict):
                    result, tool_status = {"error": "Invalid tool arguments."}, "failed"
                else:
                    result, tool_status = execute_tool(call.name, arguments, user)
                tool_events.append({"name": call.name, "status": tool_status})
                results.append((call, result))
            provider.append_tool_results(contents, response, results)
    except GeminiQuotaExceeded as exc:
        logger.warning("Gemini chatbot quota exhausted: %s", _safe_provider_error(exc))
        raise AgentUnavailable(str(exc), status_code=429) from exc
    except GeminiUnavailable as exc:
        # Configuration/auth/model failures already carry a safe, mapped
        # message from the provider — surface it instead of re-branding every
        # non-quota failure as a transient outage, so operators can tell a
        # misconfigured key apart from a service blip.
        logger.warning("Gemini chatbot unavailable: %s", _safe_provider_error(exc))
        raise AgentUnavailable(str(exc)) from exc
    except Exception as exc:
        logger.warning(
            "Gemini chatbot request failed (%s): %s",
            type(exc).__name__, _safe_provider_error(exc),
        )
        raise AgentUnavailable("The chatbot service is temporarily unavailable. Please try again.") from exc
    # The bounded loop ran out of rounds without a final answer (typically a
    # model retrying a search that kept returning nothing). This is a normal
    # degenerate conversation, not an outage: log which tools ran so the
    # failing stage is visible during development, then return a graceful
    # fallback instead of the generic 503.
    logger.warning(
        "Chatbot agent loop ended without a final answer | message=%r | tool_calls=%s",
        message[:200], tool_events,
    )
    return {
        "bot_response": (
            "I wasn't able to finish working on that question. "
            "Could you rephrase it — for example with the exact product name "
            "or the product category you have in mind?"
        ),
        "tool_calls": tool_events,
        "query_success": False,
    }

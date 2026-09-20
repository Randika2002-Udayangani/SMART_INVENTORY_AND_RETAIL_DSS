"""Controlled LLM tool-calling agent for the chatbot.

The functions in this module deliberately return small JSON-safe payloads.  The
model never receives a database connection or a queryset and cannot invoke a
tool which is not in ``TOOL_HANDLERS``.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Min, OuterRef, Q, Subquery, Sum

from inventory.services.reorder_logic import check_reorder_needs
from inventory.services.stock import get_available_stock
from inventory.services.lifecycle import get_latest_lifecycle
from products.models import Product
from sales.models import ItemSalesRecord
from .gemini import GeminiQuotaExceeded, GeminiUnavailable
from .provider import get_provider


logger = logging.getLogger(__name__)


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


def _matching_products(query="", category="", max_price=None):
    products = Product.objects.filter(is_active=True).select_related("category", "brand")
    if query:
        products = products.filter(
            Q(product_name__icontains=query)
            | Q(brand__brand_name__icontains=query)
            | Q(category__category_name__icontains=query)
        )
    if category:
        products = products.filter(category__category_name__icontains=category)
    if max_price is not None:
        products = products.filter(unit_price__lte=Decimal(str(max_price)))
    return products.order_by("unit_price", "product_name")[:20]


def search_products(arguments, user):
    products = _matching_products(
        arguments.get("query", ""), arguments.get("category", ""), arguments.get("max_price")
    )
    return {"products": [_public_product(p) for p in products]}


def get_product_details(arguments, user):
    product_id = arguments.get("product_id")
    name = arguments.get("product_name", "")
    products = Product.objects.filter(is_active=True).select_related("category", "brand")
    product = products.filter(pk=product_id).first() if product_id else _matching_products(name).first()
    if not product:
        return {"product": None, "message": "No matching active product was found."}
    return {"product": _public_product(product, include_stock=is_manager(user))}


def find_cheapest_product(arguments, user):
    products = _matching_products(arguments.get("query", ""), arguments.get("category", ""))
    available_only = arguments.get("available_only", False)
    result = []
    for product in products:
        data = _public_product(product, include_stock=is_manager(user))
        if not available_only or data["is_available"]:
            result.append(data)
    return {"product": result[0] if result else None, "alternatives": result[1:5]}


def compare_products(arguments, user):
    names = arguments.get("product_names") or []
    products = []
    for name in names[:5]:
        product = _matching_products(name).first()
        if product:
            products.append(_public_product(product, include_stock=is_manager(user)))
    return {"products": products, "missing": [n for n in names if not any(p["product_name"].lower() == n.lower() for p in products)]}


def get_current_stock_tool(arguments, user):
    product = _matching_products(arguments.get("product_name", "")).first()
    if not product:
        return {"product": None, "message": "No matching active product was found."}
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
    today = date.today()
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
    end = date.fromisoformat(arguments["date_to"]) if arguments.get("date_to") else date.today()
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
    "get_low_stock_products": get_low_stock_products,
    "get_reorder_recommendations": get_reorder_recommendations, "get_product_lifecycle": get_product_lifecycle,
    "get_health_score": get_health_score, "get_sales_summary": get_sales_summary,
    "get_slow_moving_products": get_slow_moving_products, "get_profit_margin": get_profit_margin,
}


def _tool(name, description, properties, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}


TOOLS = [
    _tool("search_products", "Search public active products by product, category, or brand.", {"query": {"type": "string"}, "category": {"type": "string"}, "max_price": {"type": "number"}}),
    _tool("get_product_details", "Get current details for one product.", {"product_id": {"type": "integer"}, "product_name": {"type": "string"}}),
    _tool("find_cheapest_product", "Find the lowest priced matching active product.", {"query": {"type": "string"}, "category": {"type": "string"}, "available_only": {"type": "boolean"}}),
    _tool("compare_products", "Compare named products using current database data.", {"product_names": {"type": "array", "items": {"type": "string"}}}, ("product_names",)),
    _tool("get_current_stock", "Get current product availability; exact stock is manager-only.", {"product_name": {"type": "string"}}, ("product_name",)),
    _tool("get_expiring_products", "List active products with in-stock batches expiring within a number of days (default 30, max 90).", {"product_name": {"type": "string"}, "days": {"type": "integer"}}),
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


def execute_tool(name, arguments, user):
    if name not in TOOL_HANDLERS:
        return {"error": "Unknown tool requested."}, "rejected"
    if name in MANAGER_TOOLS and not is_manager(user):
        return {"error": "This information is available only to managers and administrators."}, "forbidden"
    try:
        return TOOL_HANDLERS[name](arguments, user), "completed"
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}, "failed"
    except Exception:
        # Do not expose implementation/database details to the model or caller.
        return {"error": "The requested data is temporarily unavailable."}, "failed"


def run_agent(message, user, history=None):
    """Run a bounded provider-neutral tool loop and return safe metadata."""
    try:
        provider = get_provider(SYSTEM_INSTRUCTIONS)
    except GeminiUnavailable as exc:
        raise AgentUnavailable(str(exc)) from exc

    contents = provider.make_contents(history, message)
    available_tools = TOOLS if is_manager(user) else [tool for tool in TOOLS if tool["name"] not in MANAGER_TOOLS]
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
    except Exception as exc:
        logger.warning(
            "Gemini chatbot request failed (%s): %s",
            type(exc).__name__, _safe_provider_error(exc),
        )
        raise AgentUnavailable("The chatbot service is temporarily unavailable. Please try again.") from exc
    raise AgentUnavailable("The chatbot could not complete this request. Please try a simpler question.")

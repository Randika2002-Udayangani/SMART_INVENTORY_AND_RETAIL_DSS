# ============================================================
# The current version (55 lines) is a keyword-only stub: it detects
# an intent and returns one of 5 canned strings, never touches the
# database, and returns no products. This replacement is scoped
# tightly to Section 18 of the API doc: exactly the 6 intents that
# ChatbotLog.INTENT_CHOICES already defines (BUDGET_QUERY,
# PRICE_QUERY, AVAILABILITY_QUERY, BRAND_QUERY, PACK_SIZE_QUERY,
# UNKNOWN — note the doc's table also lists BRAND_QUERY under
# "brand name detected via fuzzy match", which is why brand
# detection below is fuzzy-only, not keyword-only).
#
# NOTE: an unmerged branch (week5/kiritharan-clean) already has a
# 1,257-line version of this file with 9 intents (adds GREETING,
# HELP, THANK_YOU, GOODBYE, RECOMMENDATION_QUERY, BEST_SELLING_QUERY)
# and a 50% fuzzy threshold. I didn't start from that version —
# partly because its extra intents fall outside ChatbotLog's current
# choices (would need a migration to log them), and partly because
# the spec explicitly calls for an 80% threshold, not 50%. If you'd
# rather adopt the richer 9-intent version instead of this leaner
# one, that's a valid call — just say so and I'll widen this to
# match it (with the ChatbotLog migration that requires).
#
# Requires: pip install fuzzywuzzy==0.18.0 python-Levenshtein==0.25.1
# and add both to requirements.txt — dev1 currently has neither.
# ============================================================

import re

from fuzzywuzzy import fuzz

from products.models import Product, Brand
from inventory.services.stock import get_available_stock

FUZZY_THRESHOLD = 80  # per Section 18 — corrected from 70% in v2.0


def extract_number(message):
    numbers = re.findall(r"\d+(?:\.\d+)?", message)
    return float(numbers[0]) if numbers else None


def _fuzzy_match_brand(message):
    best, best_score = None, 0
    for brand in Brand.objects.all():
        score = fuzz.partial_ratio(brand.brand_name.lower(), message)
        if score >= FUZZY_THRESHOLD and score > best_score:
            best, best_score = brand, score
    return best


def _fuzzy_match_product(message):
    best, best_score = None, 0
    for product in Product.objects.filter(is_active=True):
        score = fuzz.partial_ratio(product.product_name.lower(), message)
        if score >= FUZZY_THRESHOLD and score > best_score:
            best, best_score = product, score
    return best


def _product_public_dict(product, is_available=None):
    return {
        "product_name": product.product_name,
        "unit_price": float(product.unit_price),
        "is_available": (
            is_available if is_available is not None else product.is_active
        ),
    }


def detect_intent(message):
    """
    Order matters: cheap keyword checks run first (no DB hit),
    fuzzy brand matching runs last since it means a query per brand.
    """
    if any(w in message for w in ["under", "below", "budget", "within", "cheap", "less than"]):
        return "BUDGET_QUERY"

    if any(w in message for w in ["price", "cost", "how much", "rate"]):
        return "PRICE_QUERY"

    if any(w in message for w in ["available", "have", "stock", "in stock"]):
        return "AVAILABILITY_QUERY"

    if re.search(r"\b\d+\s?(l|ml|kg|g)\b", message) or any(
        w in message for w in ["size", "pack"]
    ):
        return "PACK_SIZE_QUERY"

    if _fuzzy_match_brand(message):
        return "BRAND_QUERY"

    return "UNKNOWN"


def handle_budget_query(message):
    amount = extract_number(message)
    if amount is None:
        return {
            "bot_response": "Could you tell me your budget? e.g. 'cooking oil under 500 rupees'",
            "products": [],
            "query_success": False,
        }

    products = Product.objects.filter(
        is_active=True, unit_price__lte=amount
    ).order_by("unit_price")[:10]

    return {
        "bot_response": (
            f"Here are products under Rs. {amount:.0f}:"
            if products else f"No products found under Rs. {amount:.0f}."
        ),
        "products": [_product_public_dict(p) for p in products],
        "query_success": products.exists(),
    }


def handle_price_query(message):
    product = _fuzzy_match_product(message)
    if not product:
        return {
            "bot_response": "I couldn't find that product — could you check the spelling?",
            "products": [],
            "query_success": False,
        }

    return {
        "bot_response": f"{product.product_name} costs Rs. {float(product.unit_price):.2f}.",
        "products": [_product_public_dict(product)],
        "query_success": True,
    }


def handle_availability_query(message):
    product = _fuzzy_match_product(message)
    if not product:
        return {
            "bot_response": "I couldn't find that product — could you check the spelling?",
            "products": [],
            "query_success": False,
        }

    stock = get_available_stock(product.id)
    if stock > 10:
        status_text = "Available"
    elif stock > 0:
        status_text = "Limited Stock"
    else:
        status_text = "Unavailable"

    return {
        "bot_response": f"{product.product_name} is currently {status_text}.",
        "products": [_product_public_dict(product, is_available=stock > 0)],
        "query_success": True,
    }


def handle_brand_query(message):
    brand = _fuzzy_match_brand(message)
    if not brand:
        return {
            "bot_response": "I couldn't recognise that brand.",
            "products": [],
            "query_success": False,
        }

    products = Product.objects.filter(is_active=True, brand=brand)[:10]
    return {
        "bot_response": (
            f"Here are products from {brand.brand_name}:"
            if products else f"No active products found from {brand.brand_name}."
        ),
        "products": [_product_public_dict(p) for p in products],
        "query_success": products.exists(),
    }


def handle_pack_size_query(message):
    size_match = re.search(r"\b\d+\s?(l|ml|kg|g)\b", message)
    if not size_match:
        return {
            "bot_response": "What pack size are you looking for? e.g. '1L' or '500g'.",
            "products": [],
            "query_success": False,
        }

    size_text = size_match.group(0).replace(" ", "")
    products = Product.objects.filter(
        is_active=True, product_name__icontains=size_text
    )[:10]

    return {
        "bot_response": (
            f"Here are products in {size_text}:"
            if products else f"No products found in {size_text}."
        ),
        "products": [_product_public_dict(p) for p in products],
        "query_success": products.exists(),
    }


def handle_unknown():
    return {
        "bot_response": (
            "Sorry, I didn't understand that. Try asking about a product's "
            "price, availability, brand, or a budget (e.g. 'cooking oil under 500')."
        ),
        "products": [],
        "query_success": False,
    }


_HANDLERS = {
    "BUDGET_QUERY": handle_budget_query,
    "PRICE_QUERY": handle_price_query,
    "AVAILABILITY_QUERY": handle_availability_query,
    "BRAND_QUERY": handle_brand_query,
    "PACK_SIZE_QUERY": handle_pack_size_query,
}


def chatbot_response(message, customer_id=None):
    """
    Combined entry point — called from orders/views.py's chatbot()
    view. Signature kept identical to the current stub so the view
    doesn't need to change how it calls this.
    """
    message = message.lower().strip()
    intent = detect_intent(message)

    handler = _HANDLERS.get(intent, handle_unknown)
    result = handler(message) if intent in _HANDLERS else handle_unknown()

    return {
        "intent": intent,
        "bot_response": result["bot_response"],
        "products": result["products"],
        "query_success": result["query_success"],
    }

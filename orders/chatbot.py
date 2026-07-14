import re


# 🔹 Detect user intent from message
def detect_intent(message):
    message = message.lower()

    # Budget-related queries
    if any(word in message for word in ["under", "below", "budget", "less than"]):
        return "BUDGET_QUERY"

    # Price-related queries
    elif any(word in message for word in ["price", "cost", "how much"]):
        return "PRICE_QUERY"

    # Availability queries
    elif any(word in message for word in ["available", "in stock", "have", "stock"]):
        return "AVAILABILITY_QUERY"

    # Brand queries
    elif any(word in message for word in ["brand", "company", "from"]):
        return "BRAND_QUERY"

    # Pack size queries
    elif any(word in message for word in ["size", "pack", "kg", "ml", "litre"]):
        return "PACK_SIZE_QUERY"

    # Unknown queries
    else:
        return "UNKNOWN"


# 🔹 Generate response based on intent
def generate_response(intent):
    responses = {
        "BUDGET_QUERY": "Here are budget-friendly products",
        "PRICE_QUERY": "Here are product prices",
        "AVAILABILITY_QUERY": "Checking available stock",
        "BRAND_QUERY": "Here are products from that brand",
        "PACK_SIZE_QUERY": "Here are available pack sizes",
        "UNKNOWN": "Sorry, I didn't understand your request"
    }

    return responses.get(intent, "Unknown request")


# 🔹 Optional: Extract numbers (for advanced use)
def extract_number(message):
    numbers = re.findall(r'\d+', message)
    return int(numbers[0]) if numbers else None
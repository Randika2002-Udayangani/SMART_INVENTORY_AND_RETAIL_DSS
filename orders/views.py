from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny
from rest_framework.authentication import SessionAuthentication

import json
from django.http import JsonResponse
from .chatbot import detect_intent


@api_view(['POST'])
@authentication_classes([])   # 🔥 disables JWT authentication
@permission_classes([AllowAny])
def chatbot_query(request):
    try:
        data = request.data
        message = data.get("message", "").strip()

        if not message:
            return JsonResponse({
                "error": "Message is required"
            }, status=400)

        intent = detect_intent(message)

        responses = {
            "BUDGET_QUERY": "Here are budget-friendly products",
            "PRICE_QUERY": "Here are product prices",
            "AVAILABILITY_QUERY": "Checking available stock",
            "BRAND_QUERY": "Here are products from that brand",
            "PACK_SIZE_QUERY": "Here are available pack sizes",
            "UNKNOWN": "Sorry, I didn't understand your request"
        }

        reply = responses.get(intent, "Unknown request")

        return JsonResponse({
            "message": message,
            "intent": intent,
            "reply": reply
        })

    except Exception as e:
        return JsonResponse({
            "error": str(e)
        }, status=500)
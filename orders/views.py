from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json

from core.models import AuditLog
from .chatbot import detect_intent


# =========================
# CHATBOT API
# =========================

@csrf_exempt
def chatbot(request):

    if request.method == "POST":

        data = json.loads(request.body)

        message = data.get("message", "")

        intent = detect_intent(message)

        return JsonResponse({
            "message": message,
            "intent": intent
        })

    return JsonResponse({
        "error": "POST method required"
    }, status=405)


# =========================
# ORDER STATUS UPDATE API
# =========================

@csrf_exempt
def update_order_status(request):

    if request.method == "PUT":

        data = json.loads(request.body)

        order_ref = data.get("order_ref")

        status = data.get("status")

        if not order_ref:
            return JsonResponse({
                "error": "order_ref required"
            }, status=400)

        if not status:
            return JsonResponse({
                "error": "status required"
            }, status=400)

        # READY notification
        if status == "READY":

            AuditLog.objects.create(
                action=f"Order {order_ref} is ready for pickup"
            )

        # CANCELLED notification
        elif status == "CANCELLED":

            AuditLog.objects.create(
                action=f"Order {order_ref} has been cancelled"
            )

        # PROCESSING notification
        elif status == "PROCESSING":

            AuditLog.objects.create(
                action=f"Order {order_ref} is processing"
            )

        return JsonResponse({
            "message": "Order updated successfully",
            "order_ref": order_ref,
            "status": status
        })

    return JsonResponse({
        "error": "PUT method required"
    }, status=405)
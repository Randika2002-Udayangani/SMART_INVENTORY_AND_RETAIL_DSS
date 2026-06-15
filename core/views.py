from django.http import JsonResponse
import datetime

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


# 🔹 Public API (no authentication required)
def status(request):
    return JsonResponse({
        "status": "working",
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })


# 🔹 Secure API (requires JWT token)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def secure_status(request):
    return Response({
        "message": "You are authenticated 🎉",
        "user": str(request.user)
    })
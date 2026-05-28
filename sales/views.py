from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import DailyBillSummary


@csrf_exempt
def upload_daily_bill(request):

    if request.method == "POST":

        uploaded_file = request.FILES.get('file')

        if not uploaded_file:
            return JsonResponse({
                "error": "No file uploaded"
            }, status=400)

        DailyBillSummary.objects.create(
            filename=uploaded_file.name,
            total_sales=10000
        )

        return JsonResponse({
            "status": "uploaded successfully",
            "file": uploaded_file.name
        })

    return JsonResponse({
        "error": "POST method required"
    }, status=405)
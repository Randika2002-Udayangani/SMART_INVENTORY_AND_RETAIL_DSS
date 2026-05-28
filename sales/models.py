from django.db import models


class DailyBillSummary(models.Model):

    filename = models.CharField(max_length=255)

    total_sales = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    uploaded_at = models.DateTimeField(auto_now_add=True)
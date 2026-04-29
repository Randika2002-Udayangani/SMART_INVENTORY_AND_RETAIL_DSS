from django.contrib import admin
from .models import UploadLog, DailyBillSummary, ItemSalesRecord

admin.site.register(UploadLog)
admin.site.register(DailyBillSummary)
admin.site.register(ItemSalesRecord)
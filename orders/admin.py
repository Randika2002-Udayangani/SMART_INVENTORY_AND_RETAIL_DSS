from django.contrib import admin
from .models import Customer, OnlineOrder, OnlineOrderItem, ChatbotLog, ProductRating, ProductRatingSummary, Notification

admin.site.register(Customer)
admin.site.register(OnlineOrder)
admin.site.register(OnlineOrderItem)
admin.site.register(ChatbotLog)
admin.site.register(ProductRating)
admin.site.register(ProductRatingSummary)
admin.site.register(Notification)

# Register your models here.

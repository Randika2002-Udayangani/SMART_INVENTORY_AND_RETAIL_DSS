from django.contrib import admin
from .models import StockLedger, StockAdjustment, SupplierReturn, LossRecord, ProductLifecycle, InventoryHealthScore, CategoryHealthScore, DiscountRule, DiscountRecommendation, ReorderRecommendation

admin.site.register(StockLedger)
admin.site.register(StockAdjustment)
admin.site.register(SupplierReturn)
admin.site.register(LossRecord)
admin.site.register(ProductLifecycle)
admin.site.register(InventoryHealthScore)
admin.site.register(CategoryHealthScore)
admin.site.register(DiscountRule)
admin.site.register(DiscountRecommendation)
admin.site.register(ReorderRecommendation)
from django.contrib import admin
from .models import StoreZone, Category, Brand, Product, ZoneRecommendation, ProductZoneOverride

admin.site.register(StoreZone)
admin.site.register(Category)
admin.site.register(Brand)
admin.site.register(Product)
admin.site.register(ZoneRecommendation)
admin.site.register(ProductZoneOverride)
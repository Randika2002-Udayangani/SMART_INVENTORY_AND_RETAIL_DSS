from django.db import models

# Create your models here.



class StoreZone(models.Model):
    TRAFFIC_CHOICES = [
        ('High', 'High'),
        ('Medium', 'Medium'),
        ('Low', 'Low'),
    ]
    zone_name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    traffic_level = models.CharField(max_length=10, choices=TRAFFIC_CHOICES)

    class Meta:
        db_table = 'store_zone'

    def __str__(self):
        return self.zone_name


class Category(models.Model):
    category_name = models.CharField(max_length=100)
    default_zone = models.ForeignKey(
        StoreZone, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='default_zone_id'
    )

    class Meta:
        db_table = 'category'

    def __str__(self):
        return self.category_name


class Brand(models.Model):
    brand_name = models.CharField(max_length=100)
    manufacturer = models.CharField(max_length=100, blank=True)

    class Meta:
        db_table = 'brand'

    def __str__(self):
        return self.brand_name


class Product(models.Model):
    product_name = models.CharField(max_length=150)
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL,
        null=True, blank=True
    )
    brand = models.ForeignKey(
        Brand, on_delete=models.SET_NULL,
        null=True, blank=True
    )
    sku_code = models.CharField(max_length=50, unique=True, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2)
    avg_cost_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    reorder_threshold = models.IntegerField(default=0)
    introduced_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'product'

    def __str__(self):
        return self.product_name


class ZoneRecommendation(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, db_column='product_id'
    )
    current_zone = models.ForeignKey(
        StoreZone, on_delete=models.CASCADE,
        related_name='current_recommendations', db_column='current_zone_id'
    )
    suggested_zone = models.ForeignKey(
        StoreZone, on_delete=models.CASCADE,
        related_name='suggested_recommendations', db_column='suggested_zone_id'
    )
    reason = models.CharField(max_length=255, blank=True)
    performance_score = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    recommendation_date = models.DateField(auto_now_add=True)

    class Meta:
        db_table = 'zone_recommendation'


class ProductZoneOverride(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, db_column='product_id'
    )
    zone = models.ForeignKey(
        StoreZone, on_delete=models.CASCADE, db_column='zone_id'
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'product_zone_override'
from django.db import models
from django.conf import settings

# Create your models here.



class StoreZone(models.Model):
    TRAFFIC_CHOICES = [
        ('High', 'High'),
        ('Medium', 'Medium'),
        ('Low', 'Low'),
    ]
    ZONE_TYPE_CHOICES = [
        ('GENERAL', 'General'),           # regular category-area zone
        ('HIGH_TRAFFIC', 'High Traffic'), # e.g. "Zone A" — top performers
        ('PROMOTIONAL', 'Promotional'),   # end-of-aisle, near-expiry stock
        ('DISCOUNT_BIN', 'Discount Bin'), # slow-moving stock
    ]
    zone_name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    traffic_level = models.CharField(max_length=10, choices=TRAFFIC_CHOICES)
    zone_type = models.CharField(
        max_length=20, choices=ZONE_TYPE_CHOICES, default='GENERAL'
    )
 
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
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
        ('APPLIED', 'Applied'),
    ]
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
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='PENDING'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+'
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


class ZoneCalculationRun(models.Model):
    """
    One row per "Calculate Recommendations" click. Exists purely so the
    Store Zone Recommendations page can show real last-run info (KPI
    counts, last-calculated timestamp) to *anyone* loading the page —
    not just within the browser session that triggered the calculation.
    """
    run_at = models.DateTimeField(auto_now_add=True)
    products_evaluated = models.IntegerField(default=0)
    recommendations_created = models.IntegerField(default=0)
    skipped_no_health_score = models.IntegerField(default=0)
    skipped_no_current_zone = models.IntegerField(default=0)
    skipped_duplicate = models.IntegerField(default=0)
    categories_unmapped_count = models.IntegerField(default=0)

    class Meta:
        db_table = 'zone_calculation_run'
        ordering = ['-run_at']
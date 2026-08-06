from django.db import models

# Create your models here.

from products.models import Product, Category
from purchases.models import PurchaseBatch
from suppliers.models import Supplier
from users.models import AppUser


# ── F3: Inventory & Stock ─────────────────────────────────────────────────────

class StockLedger(models.Model):
    TRANSACTION_TYPES = [
        ('PURCHASE', 'Purchase'),
        ('SALE_SYNC', 'Sale Sync'),
        ('MANUAL_ADJUSTMENT', 'Manual Adjustment'),
        ('INITIAL_IMPORT', 'Initial Import'),
    ]
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    batch = models.ForeignKey(
        PurchaseBatch, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='batch_id'
    )
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    source = models.CharField(max_length=30)
    quantity_change = models.IntegerField()
    transaction_date = models.DateTimeField(auto_now_add=True)
    reference_id = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = 'stock_ledger'


class StockAdjustment(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    batch = models.ForeignKey(
        PurchaseBatch, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='batch_id'
    )
    quantity_change = models.IntegerField()
    reason = models.CharField(max_length=255)
    adjustment_date = models.DateTimeField(auto_now_add=True)
    adjusted_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='adjusted_by'
    )

    class Meta:
        db_table = 'stock_adjustment'


# ── F7: Loss & Supplier Returns ───────────────────────────────────────────────

class SupplierReturn(models.Model):
    RECOVERY_CHOICES = [
        ('CREDIT_NOTE', 'Credit Note'),
        ('REPLACEMENT', 'Replacement'),
        ('CASH', 'Cash'),
    ]
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('CONFIRMED', 'Confirmed'),
        ('REJECTED', 'Rejected'),
    ]
    REASON_CHOICES = [
        ('EXPIRY', 'Expiry'),
        ('DAMAGE', 'Damage'),
        ('WRONG_ITEM', 'Wrong Item'),
    ]
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, db_column='supplier_id'
    )
    batch = models.ForeignKey(
        PurchaseBatch, on_delete=models.PROTECT, db_column='batch_id'
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    return_date = models.DateField()
    quantity_returned = models.IntegerField()
    return_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    return_reason = models.CharField(
        max_length=20, choices=REASON_CHOICES, blank=True
    )
    recovery_type = models.CharField(
        max_length=20, choices=RECOVERY_CHOICES, blank=True
    )
    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default='PENDING'
    )
    recorded_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='recorded_by'
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'supplier_return'


class LossRecord(models.Model):
    LOSS_TYPES = [
        ('EXPIRY', 'Expiry'),
        ('SLOW_MOVING', 'Slow Moving'),
        ('DAMAGE', 'Damage'),
        ('OTHER', 'Other'),
    ]
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    batch = models.ForeignKey(
        PurchaseBatch, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='batch_id'
    )
    loss_type = models.CharField(max_length=20, choices=LOSS_TYPES)
    loss_quantity = models.IntegerField()
    loss_value = models.DecimalField(max_digits=12, decimal_places=2)
    loss_date = models.DateField()
    recorded_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='recorded_by'
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'loss_record'


# ── F6: Product Lifecycle ─────────────────────────────────────────────────────

class ProductLifecycle(models.Model):
    STATUS_CHOICES = [
        ('NEW', 'New'),
        ('GROWING', 'Growing'),
        ('STABLE', 'Stable'),
        ('DECLINING', 'Declining'),
        ('SLOW_MOVING', 'Slow Moving'),
    ]
    RECOMMENDATION_CHOICES = [
        ('RETAIN', 'Retain'),
        ('DISCOUNT', 'Discount'),
        ('DISCONTINUE', 'Discontinue'),
        ('MONITOR', 'Monitor'),
    ]
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, db_column='product_id'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    sales_velocity = models.DecimalField(max_digits=10, decimal_places=2)
    comparison_period = models.CharField(max_length=10)
    recommendation = models.CharField(
        max_length=15, choices=RECOMMENDATION_CHOICES
    )
    calculated_date = models.DateField(auto_now_add=True)

    class Meta:
        db_table = 'product_lifecycle'


# ── F8: Inventory Health Score ────────────────────────────────────────────────

class InventoryHealthScore(models.Model):
    STATUS_CHOICES = [
        ('HEALTHY', 'Healthy'),
        ('WATCH', 'Watch'),
        ('AT RISK', 'At Risk'),
        ('CRITICAL', 'Critical'),
    ]
    WEIGHTING_CHOICES = [
        ('4-COMPONENT', '4 Component'),
        ('5-COMPONENT', '5 Component'),
    ]
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, db_column='product_id'
    )
    velocity_score = models.DecimalField(max_digits=5, decimal_places=2)
    margin_score = models.DecimalField(max_digits=5, decimal_places=2)
    expiry_risk_score = models.DecimalField(max_digits=5, decimal_places=2)
    stock_duration_score = models.DecimalField(max_digits=5, decimal_places=2)
    rating_score = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    overall_score = models.DecimalField(max_digits=5, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    recommended_action = models.CharField(max_length=255, blank=True)
    rating_sufficient = models.BooleanField(default=False)
    weighting_mode = models.CharField(
        max_length=15, choices=WEIGHTING_CHOICES, default='4-COMPONENT'
    )
    calculated_date = models.DateField(auto_now_add=True)
    calculated_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='calculated_by'
    )

    class Meta:
        db_table = 'inventory_health_score'


class CategoryHealthScore(models.Model):
    STATUS_CHOICES = [
        ('HEALTHY', 'Healthy'),
        ('WATCH', 'Watch'),
        ('AT RISK', 'At Risk'),
        ('CRITICAL', 'Critical'),
    ]
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, db_column='category_id'
    )
    avg_health_score = models.DecimalField(max_digits=5, decimal_places=2)
    healthy_count = models.IntegerField(default=0)
    watch_count = models.IntegerField(default=0)
    at_risk_count = models.IntegerField(default=0)
    critical_count = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    calculated_date = models.DateField(auto_now_add=True)
    calculated_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='calculated_by'
    )

    class Meta:
        db_table = 'category_health_score'


# ── F9: Discount Engine ───────────────────────────────────────────────────────

class DiscountRule(models.Model):
    days_from_expiry_min = models.IntegerField()
    days_from_expiry_max = models.IntegerField()
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    minimum_margin_pct = models.DecimalField(max_digits=5, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='created_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'discount_rule'


class DiscountRecommendation(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPLIED', 'Applied'),
        ('IGNORED', 'Ignored'),
        ('EXPIRED', 'Expired'),
    ]
    BEST_ACTION_CHOICES = [
        ('DISCOUNT', 'Discount'),
        ('RETURN', 'Return'),
        ('DISCARD', 'Discard'),
    ]
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    batch = models.ForeignKey(
        PurchaseBatch, on_delete=models.PROTECT, db_column='batch_id'
    )
    days_until_expiry = models.IntegerField()
    current_price = models.DecimalField(max_digits=10, decimal_places=2)
    recommended_discount_pct = models.DecimalField(max_digits=5, decimal_places=2)
    recommended_price = models.DecimalField(max_digits=10, decimal_places=2)
    profit_protected = models.BooleanField(default=False)
    recovery_sell = models.DecimalField(max_digits=12, decimal_places=2)
    recovery_return = models.DecimalField(max_digits=12, decimal_places=2)
    recovery_discard = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    best_action = models.CharField(
        max_length=10, choices=BEST_ACTION_CHOICES
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='PENDING'
    )
    calculated_date = models.DateField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reviewed_discounts', db_column='reviewed_by'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'discount_recommendation'


# ── F11: Reorder Recommendations ─────────────────────────────────────────────

class ReorderRecommendation(models.Model):
    URGENCY_CHOICES = [
        ('CRITICAL', 'Critical'),
        ('HIGH', 'High'),
        ('NORMAL', 'Normal'),
    ]
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ORDERED', 'Ordered'),
        ('IGNORED', 'Ignored'),
    ]
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='supplier_id'
    )
    current_stock = models.IntegerField()
    avg_daily_sales = models.DecimalField(max_digits=10, decimal_places=2)
    days_of_stock = models.IntegerField()
    safety_stock = models.IntegerField()
    suggested_quantity = models.IntegerField()
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2)
    urgency = models.CharField(max_length=10, choices=URGENCY_CHOICES)
    calculation_date = models.DateField(auto_now_add=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='PENDING'
    )
    actioned_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='actioned_by'
    )

    class Meta:
        db_table = 'reorder_recommendation'
from django.db import models

# Create your models here.

from products.models import Product
from users.models import AppUser


class Customer(models.Model):
    name = models.CharField(max_length=150)
    contact_number = models.CharField(max_length=15, blank=True)
    email = models.CharField(max_length=100, unique=True)
    address = models.CharField(max_length=255, blank=True)
    password_hash = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'customer'

    def __str__(self):
        return self.name


class OnlineOrder(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('CONFIRMED', 'Confirmed'),
        ('READY', 'Ready'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
    ]
    PAYMENT_STATUS = [
        ('UNPAID', 'Unpaid'),
        ('PAID', 'Paid'),
    ]
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, db_column='customer_id'
    )
    order_reference = models.CharField(max_length=20, unique=True)
    order_date = models.DateTimeField(auto_now_add=True)
    pickup_date = models.DateField()
    pickup_time_slot = models.CharField(max_length=15, blank=True)
    collection_deadline = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='PENDING'
    )
    cancel_reason = models.CharField(max_length=255, blank=True)
    cancelled_by = models.CharField(max_length=10, blank=True)
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    payment_status = models.CharField(
        max_length=10, choices=PAYMENT_STATUS, default='UNPAID'
    )
    notes = models.CharField(max_length=255, blank=True)
    confirmed_by = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='confirmed_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'online_order'

    def __str__(self):
        return self.order_reference


class OnlineOrderItem(models.Model):
    order = models.ForeignKey(
        OnlineOrder, on_delete=models.CASCADE, db_column='order_id'
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, db_column='product_id'
    )
    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    is_reserved = models.BooleanField(default=False)
    reserved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'online_order_item'


class ChatbotLog(models.Model):
    INTENT_CHOICES = [
        ('BUDGET_QUERY', 'Budget Query'),
        ('BRAND_QUERY', 'Brand Query'),
        ('PRICE_QUERY', 'Price Query'),
        ('AVAILABILITY_QUERY', 'Availability Query'),
        ('PACK_SIZE_QUERY', 'Pack Size Query'),
        ('UNKNOWN', 'Unknown'),
    ]
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='customer_id'
    )
    session_id = models.CharField(max_length=50)
    user_message = models.TextField()
    bot_response = models.TextField()
    intent_detected = models.CharField(
        max_length=30, choices=INTENT_CHOICES, default='UNKNOWN'
    )
    query_success = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'chatbot_log'


class ProductRating(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, db_column='product_id'
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, db_column='customer_id'
    )
    order = models.ForeignKey(
        OnlineOrder, on_delete=models.SET_NULL,
        null=True, blank=True, db_column='order_id'
    )
    rating = models.IntegerField()
    feedback_text = models.CharField(max_length=500, blank=True)
    is_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'product_rating'
        unique_together = ('customer', 'product')


class ProductRatingSummary(models.Model):
    TREND_CHOICES = [
        ('IMPROVING', 'Improving'),
        ('STABLE', 'Stable'),
        ('DECLINING', 'Declining'),
    ]
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, db_column='product_id'
    )
    period = models.CharField(max_length=10)
    avg_rating = models.DecimalField(max_digits=3, decimal_places=2)
    rating_count = models.IntegerField(default=0)
    verified_count = models.IntegerField(default=0)
    trend = models.CharField(
        max_length=15, choices=TREND_CHOICES, default='STABLE'
    )
    calculated_date = models.DateField(auto_now_add=True)

    class Meta:
        db_table = 'product_rating_summary'


class Notification(models.Model):
    PRIORITY_CHOICES = [
        ('CRITICAL', 'Critical'),
        ('HIGH', 'High'),
        ('MEDIUM', 'Medium'),
        ('LOW', 'Low'),
    ]
    user = models.ForeignKey(
        AppUser, on_delete=models.SET_NULL,       # ← was CASCADE, now SET_NULL
        null=True, blank=True, db_column='user_id'
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL,      # ← was CASCADE, now SET_NULL
        null=True, blank=True, db_column='customer_id'
    )
    type = models.CharField(max_length=50)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES)
    title = models.CharField(max_length=100)
    message = models.CharField(max_length=255)
    reference_table = models.CharField(max_length=50, blank=True)
    reference_id = models.IntegerField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'notification'
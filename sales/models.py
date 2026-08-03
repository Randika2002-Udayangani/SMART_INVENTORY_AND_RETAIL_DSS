from django.db import models
from products.models import Product


class UploadLog(models.Model):
    UPLOAD_TYPES = [
        ('ITEM_SALES', 'Item Sales Excel'),
        ('DAILY_BILLS', 'Daily Bills PDF'),
        ('ITEM_MASTER', 'Item Master Excel'),
        ('EXPORT', 'Report Export'),
        ('SUPPLIER_INVOICE', 'Supplier Invoice PDF'),
    ]

    STATUS_CHOICES = [
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
        ('PARTIAL', 'Partial'),
    ]

    file_name = models.CharField(max_length=150)
    upload_date = models.DateTimeField(auto_now_add=True)
    upload_type = models.CharField(max_length=30, choices=UPLOAD_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    error_message = models.TextField(blank=True)
    uploaded_by = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = 'upload_log'


class DailyBillSummary(models.Model):
    PAYMENT_TYPES = [
        ('CASH', 'Cash'),
        ('CREDIT', 'Credit'),
        ('CARD', 'Card'),
    ]

    sale_date = models.DateField()
    bill_no = models.CharField(max_length=20)
    customer_name = models.CharField(max_length=150, blank=True)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    final_amount = models.DecimalField(max_digits=12, decimal_places=2)

    payment_type = models.CharField(
        max_length=10,
        choices=PAYMENT_TYPES,
        blank=True
    )

    is_flagged = models.BooleanField(default=False)
    is_full_discount = models.BooleanField(default=False)

    upload = models.ForeignKey(
        UploadLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='upload_id'
    )

    class Meta:
        db_table = 'daily_bill_summary'


class ItemSalesRecord(models.Model):
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column='product_id'
    )

    sale_date = models.DateField()
    quantity_sold = models.IntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)

    upload = models.ForeignKey(
        UploadLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='upload_id'
    )

    class Meta:
        db_table = 'item_sales_record'


class SyncLog(models.Model):

    PIPELINE_CHOICES = [
        ('ITEM_SALES', 'Item Sales Excel'),
        ('DAILY_BILLS', 'Daily Bills PDF'),
        ('ITEM_MASTER', 'Item Master Excel'),
    ]

    STATUS_CHOICES = [
        ('SUCCESS', 'Success'),
        ('PARTIAL_SUCCESS', 'Partial Success'),
        ('FAILED', 'Failed'),
    ]

    pipeline_type = models.CharField(
        max_length=30,
        choices=PIPELINE_CHOICES
    )
    file_name = models.CharField(max_length=150)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)
    bills_processed = models.IntegerField(default=0)
    records_inserted = models.IntegerField(default=0)
    records_skipped = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    error_detail = models.JSONField(default=list)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES
    )
    resume_point_after = models.CharField(
        max_length=100,
        null=True,
        blank=True
    )

    class Meta:
        db_table = 'sync_log'
        ordering = ['-start_time']

    def __str__(self):
        return f"{self.pipeline_type} | {self.file_name} | {self.status}"
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('products', '0001_initial'),
    ]

    operations = [

        migrations.CreateModel(
            name='UploadLog',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('file_name', models.CharField(max_length=150)),
                ('upload_date', models.DateTimeField(auto_now_add=True)),
                ('upload_type', models.CharField(
                    max_length=30,
                    choices=[
                        ('ITEM_SALES', 'Item Sales Excel'),
                        ('DAILY_BILLS', 'Daily Bills PDF'),
                        ('ITEM_MASTER', 'Item Master Excel'),
                        ('EXPORT', 'Report Export')
                    ]
                )),
                ('status', models.CharField(
                    max_length=20,
                    choices=[
                        ('SUCCESS', 'Success'),
                        ('FAILED', 'Failed'),
                        ('PARTIAL', 'Partial')
                    ]
                )),
                ('error_message', models.CharField(max_length=500, blank=True)),
                ('uploaded_by', models.IntegerField(blank=True, null=True)),
            ],
            options={'db_table': 'upload_log'},
        ),

        migrations.CreateModel(
            name='ItemSalesRecord',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('sale_date', models.DateField()),
                ('quantity_sold', models.IntegerField()),
                ('unit_price', models.DecimalField(max_digits=10, decimal_places=2)),
                ('total_amount', models.DecimalField(max_digits=12, decimal_places=2)),
                ('product', models.ForeignKey(
                    to='products.product',
                    db_column='product_id',
                    on_delete=django.db.models.deletion.PROTECT
                )),
                ('upload', models.ForeignKey(
                    to='sales.uploadlog',
                    db_column='upload_id',
                    null=True,
                    blank=True,
                    on_delete=django.db.models.deletion.SET_NULL
                )),
            ],
            options={'db_table': 'item_sales_record'},
        ),

        migrations.CreateModel(
            name='DailyBillSummary',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('sale_date', models.DateField()),
                ('bill_no', models.CharField(max_length=20)),
                ('customer_name', models.CharField(max_length=150, blank=True)),
                ('gross_amount', models.DecimalField(max_digits=12, decimal_places=2)),
                ('discount', models.DecimalField(max_digits=12, decimal_places=2, default=0)),
                ('final_amount', models.DecimalField(max_digits=12, decimal_places=2)),
                ('payment_type', models.CharField(
                    max_length=10,
                    blank=True,
                    choices=[
                        ('CASH', 'Cash'),
                        ('CREDIT', 'Credit'),
                        ('CARD', 'Card')
                    ]
                )),
                ('is_flagged', models.BooleanField(default=False)),
                ('upload', models.ForeignKey(
                    to='sales.uploadlog',
                    db_column='upload_id',
                    null=True,
                    blank=True,
                    on_delete=django.db.models.deletion.SET_NULL
                )),
            ],
            options={'db_table': 'daily_bill_summary'},
        ),
    ]
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('inventory', '0009_supplierreturn_return_bill_no')]
    operations = [
        migrations.AlterField(
            model_name='stockledger',
            name='transaction_type',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('PURCHASE', 'Purchase'),
                    ('SALE_SYNC', 'Sale Sync'),
                    ('MANUAL_ADJUSTMENT', 'Manual Adjustment'),
                    ('INITIAL_IMPORT', 'Initial Import'),
                    ('SUPPLIER_RETURN', 'Supplier Return'),
                ],
            ),
        ),
    ]
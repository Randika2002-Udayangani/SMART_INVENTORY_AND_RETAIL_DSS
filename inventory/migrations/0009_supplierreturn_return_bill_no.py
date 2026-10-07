from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('inventory', '0008_alter_productlifecycle_recommendation')]
    operations = [
        migrations.AddField(
            model_name='supplierreturn',
            name='return_bill_no',
            field=models.CharField(max_length=50, blank=True, null=True),
        ),
    ]
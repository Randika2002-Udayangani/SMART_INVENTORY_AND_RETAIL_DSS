# Adds composite index on ItemSalesRecord (product, sale_date)
#
# Why this index:
#   F06, F07, F08 all query ItemSalesRecord filtered heavily by
#   product_id + sale_date range. Without this index PostgreSQL
#   does a full table scan on every query. As the table grows to
#   millions of rows this becomes very slow.
#   With this index: PostgreSQL jumps directly to matching rows.
#


from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
       
        ('sales', '0002_alter_uploadlog_error_message'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='itemsalesrecord',
            index=models.Index(
                fields=['product', 'sale_date'],
                name='item_sales_product_date_idx'
            ),
        ),
    ]
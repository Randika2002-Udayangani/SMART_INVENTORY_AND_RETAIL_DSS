# Adds zone_type to StoreZone. Without this, there's no structured
# way to tell a regular category-area zone (Dairy, Snacks, Beverages...)
# apart from a small number of special-purpose zones (the high-traffic
# "Zone A", an end-of-aisle promo zone, a discount-bin zone) — which
# the zone recommendation engine needs to distinguish. String-matching
# on zone_name ("contains 'discount'") would break the moment someone
# renames a zone; this is the one-line schema fix instead.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0003_alter_product_brand_alter_product_category'),
    ]

    operations = [
        migrations.AddField(
            model_name='storezone',
            name='zone_type',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('GENERAL', 'General'),
                    ('HIGH_TRAFFIC', 'High Traffic'),
                    ('PROMOTIONAL', 'Promotional'),
                    ('DISCOUNT_BIN', 'Discount Bin'),
                ],
                default='GENERAL',
            ),
        ),
    ]
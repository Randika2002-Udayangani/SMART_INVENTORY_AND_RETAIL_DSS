# inventory/migrations/0002_lifecycle_unique_constraint.py
# Data integrity constraint
# Adds UniqueConstraint on ProductLifecycle (product, comparison_period)
#
# Why this constraint:
#   Prevents duplicate lifecycle records if manager triggers calculation
#   multiple times in the same month.
#   Ensures: 1 product = 1 lifecycle record per comparison_period
#
#   The code already deletes existing records before bulk_create,
#   but this constraint is a DB-level safety net in case of bugs.
#
# How to apply:
#   python manage.py migrate

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0001_initial'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='productlifecycle',
            constraint=models.UniqueConstraint(
                fields=['product', 'comparison_period'],
                name='unique_product_lifecycle_period'
            ),
        ),
    ]
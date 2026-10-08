# inventory/migrations/0005_repoint_appuser_fields.py
#
# Repoints the 7 fields in this app still pointing at the dead
# AppUser table onto settings.AUTH_USER_MODEL — same fix already
# applied to AuditLog.user (users/migrations/0002_alter_auditlog_user.py).
# No data loss: none of these fields are ever written to today
# (all null or explicitly set to None in inventory/views.py).
#
# Must be applied BEFORE users/migrations/0004_delete_appuser_role_usersession.py
# — see the dependency on that ordering there.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0004_alter_reorderrecommendation_calculation_date_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='stockadjustment',
            name='adjusted_by',
            field=models.ForeignKey(
                blank=True, db_column='adjusted_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='supplierreturn',
            name='recorded_by',
            field=models.ForeignKey(
                blank=True, db_column='recorded_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='lossrecord',
            name='recorded_by',
            field=models.ForeignKey(
                blank=True, db_column='recorded_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='inventoryhealthscore',
            name='calculated_by',
            field=models.ForeignKey(
                blank=True, db_column='calculated_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='categoryhealthscore',
            name='calculated_by',
            field=models.ForeignKey(
                blank=True, db_column='calculated_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='discountrule',
            name='created_by',
            field=models.ForeignKey(
                blank=True, db_column='created_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='discountrecommendation',
            name='reviewed_by',
            field=models.ForeignKey(
                blank=True, db_column='reviewed_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='reviewed_discounts',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='reorderrecommendation',
            name='actioned_by',
            field=models.ForeignKey(
                blank=True, db_column='actioned_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
# orders/migrations/0003_repoint_appuser_fields.py
#
# Repoints OnlineOrder.confirmed_by and Notification.user from the
# dead AppUser table onto settings.AUTH_USER_MODEL. Same reasoning
# as inventory/migrations/0005_repoint_appuser_fields.py — no data
# loss, neither field is currently written to with a real value.
#
# Must be applied BEFORE users/migrations/0004_delete_appuser_role_usersession.py

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('orders', '0002_alter_onlineorder_pickup_time_slot'),
    ]

    operations = [
        migrations.AlterField(
            model_name='onlineorder',
            name='confirmed_by',
            field=models.ForeignKey(
                blank=True, db_column='confirmed_by', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='notification',
            name='user',
            field=models.ForeignKey(
                blank=True, db_column='user_id', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
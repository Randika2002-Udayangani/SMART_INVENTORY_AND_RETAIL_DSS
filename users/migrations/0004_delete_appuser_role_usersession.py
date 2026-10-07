# users/migrations/0004_delete_appuser_role_usersession.py
#
# Removes the dead Role, AppUser, UserSession models entirely.
# UserSession is deleted first since it FKs to AppUser; Role is
# deleted last since AppUser FKs to it.
#
# IMPORTANT — explicit dependency on inventory.0005 and orders.0003:
# both of those repoint their FKs off AppUser first. If this
# migration ran before either of those, dropping the app_user table
# here would fail against Postgres' FK constraints (or, if it
# somehow succeeded, would silently orphan the older FK columns).
# Applying `migrate` normally resolves this automatically once the
# dependency is declared, but do NOT run `migrate users` in
# isolation before `migrate inventory` and `migrate orders` have
# been run with their 0005/0003 migrations.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0003_userloginsecurity'),
        ('inventory', '0005_repoint_appuser_fields'),
        ('orders', '0003_repoint_appuser_fields'),
    ]

    operations = [
        migrations.DeleteModel(name='UserSession'),
        migrations.DeleteModel(name='AppUser'),
        migrations.DeleteModel(name='Role'),
    ]
    
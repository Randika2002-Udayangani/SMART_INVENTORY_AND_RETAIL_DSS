from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('orders', '0004_notificationread'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name='chatbotlog', name='staff_user', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='chatbot_logs', to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name='chatbotlog', name='tool_calls', field=models.JSONField(blank=True, default=list)),
        migrations.AlterField(model_name='chatbotlog', name='intent_detected', field=models.CharField(choices=[('AGENT_QUERY', 'Agent Query'), ('BUDGET_QUERY', 'Budget Query'), ('BRAND_QUERY', 'Brand Query'), ('PRICE_QUERY', 'Price Query'), ('AVAILABILITY_QUERY', 'Availability Query'), ('PACK_SIZE_QUERY', 'Pack Size Query'), ('UNKNOWN', 'Unknown')], default='UNKNOWN', max_length=30)),
    ]

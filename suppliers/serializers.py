from rest_framework import serializers
from .models import Supplier


class SupplierSerializer(serializers.ModelSerializer):
    # Override to force this to be required — model likely has default=0/blank=True,
    # which silently lets DRF treat it as optional. This makes omission a real error.
    lead_time_days = serializers.IntegerField(
        required=True,
        min_value=0,
        error_messages={
            'required': 'lead_time_days is required.',
            'min_value': 'lead_time_days cannot be negative.'
        }
    )

    class Meta:
        model = Supplier
        fields = '__all__'

    def validate(self, attrs):
        """
        Object-level check: at least one contact method (email or
        contact_number) must be provided. Falls back to the existing
        instance value on partial updates (PATCH) where the field
        might not be present in attrs at all.
        """
        if self.instance is not None:
            email = attrs.get('email', self.instance.email)
            contact_number = attrs.get('contact_number', self.instance.contact_number)
        else:
            email = attrs.get('email', '')
            contact_number = attrs.get('contact_number', '')

        if not email and not contact_number:
            raise serializers.ValidationError({
                'non_field_errors': (
                    'At least one contact method is required — '
                    'provide either email or contact_number.'
                )
            })

        return attrs
from rest_framework import generics
from .models import Supplier
from .serializers import SupplierSerializer
from users.audit import log_action


class SupplierListCreateView(generics.ListCreateAPIView):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer

    def perform_create(self, serializer):
        supplier = serializer.save()
        log_action(
            user=self.request.user,
            action='CREATE',
            table_name='supplier',
            record_id=supplier.id,
            old_value=None,
            new_value=SupplierSerializer(supplier).data,
            request=self.request,
        )


class SupplierDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer

    def perform_update(self, serializer):
        old_data = SupplierSerializer(self.get_object()).data
        supplier = serializer.save()
        log_action(
            user=self.request.user,
            action='UPDATE',
            table_name='supplier',
            record_id=supplier.id,
            old_value=old_data,
            new_value=SupplierSerializer(supplier).data,
            request=self.request,
        )

    def perform_destroy(self, instance):
        old_data = SupplierSerializer(instance).data
        log_action(
            user=self.request.user,
            action='DELETE',
            table_name='supplier',
            record_id=instance.id,
            old_value=old_data,
            new_value=None,
            request=self.request,
        )
        instance.delete()
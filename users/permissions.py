from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """
    ADMIN only. Same check already used inline in users/views.py
    (RegisterView, UserDetailView, UnlockUserView, SystemConfigDetailView,
    AuditLogListView/Detail) — pulled out here so it can be reused as a
    permission_classes entry instead of copy-pasted per view.
    """
    message = 'Admin access required.'

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and
            (user.is_superuser or user.groups.filter(name='ADMIN').exists())
        )


class IsManagerOrAdmin(BasePermission):
    """
    ADMIN or MANAGER. For endpoints the API Design Doc (Section 23,
    Role Access Matrix) restricts to Manager+ — e.g. discount
    calculate/rules, health-score calculate, lifecycle calculate.
    A plain STAFF-role account is rejected.
    """
    message = 'Manager or Admin access required.'

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and
            (user.is_superuser or user.groups.filter(name__in=['ADMIN', 'MANAGER']).exists())
        )
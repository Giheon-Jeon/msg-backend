from rest_framework.permissions import BasePermission

from apps.common.exceptions import Forbidden, TokenMissing


class IsAuthenticated(BasePermission):
    def has_permission(self, request, view):
        if request.user is None:
            raise TokenMissing()
        return True


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if user is None:
            raise TokenMissing()
        if user.role != "ADMIN":
            raise Forbidden()
        return True

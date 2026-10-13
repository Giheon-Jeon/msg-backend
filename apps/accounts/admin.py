from django.contrib import admin

from .models import RefreshToken, Team, User


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("team_name", "team_score", "mileage", "is_banned")
    search_fields = ("team_name",)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("login_id", "nickname", "team", "role", "is_leader", "is_staff")
    list_filter = ("role", "is_leader", "is_staff")
    search_fields = ("login_id", "nickname")

    def save_model(self, request, obj, form, change):
        if "password" in form.changed_data:
            obj.set_password(obj.password)
        super().save_model(request, obj, form, change)


@admin.register(RefreshToken)
class RefreshTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "expires_at", "created_at")

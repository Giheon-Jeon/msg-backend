from django.contrib import admin

from .models import MileageHistory, PaymentToken

# Register your models here.


@admin.register(MileageHistory)
class MileageHistoryAdmin(admin.ModelAdmin):
    list_display = ("team", "type", "amount", "item_name", "is_refunded", "created_at")
    list_filter = ("type", "is_refunded")


@admin.register(PaymentToken)
class PaymentTokenAdmin(admin.ModelAdmin):
    list_display = ("team", "status", "expires_at", "used_at", "created_at")
    list_filter = ("status",)

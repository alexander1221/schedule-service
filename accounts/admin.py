from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import AllowedEmail, LoginCode, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["email"]
    list_display = ["email", "first_name", "last_name", "is_staff", "is_active"]
    search_fields = ["email", "first_name", "last_name"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Личные данные", {"fields": ("first_name", "last_name")}),
        ("Права", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Даты", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)


@admin.register(AllowedEmail)
class AllowedEmailAdmin(admin.ModelAdmin):
    list_display = ["email", "role", "note", "added_at"]
    list_filter = ["role"]
    search_fields = ["email", "note"]


@admin.register(LoginCode)
class LoginCodeAdmin(admin.ModelAdmin):
    """Только просмотр: когда и кому отправлялись коды (сами коды не хранятся)."""

    list_display = ["email", "created_at", "expires_at", "attempts", "used_at"]
    search_fields = ["email"]
    readonly_fields = ["email", "created_at", "expires_at", "attempts", "used_at"]
    exclude = ["code_hash"]

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

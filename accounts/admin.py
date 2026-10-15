from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils import timezone

from .models import AllowedEmail, ApiToken, LoginCode, User


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


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    """Выпуск и отзыв токенов API. Токен показывается один раз — сразу после сохранения."""

    list_display = ["prefix", "name", "user", "created_at", "expires_at", "last_used_at", "revoked_at"]
    list_filter = ["revoked_at"]
    search_fields = ["name", "user__email", "prefix"]
    fields = ["user", "name", "expires_at", "prefix", "created_at", "last_used_at", "revoked_at"]
    readonly_fields = ["prefix", "created_at", "last_used_at", "revoked_at"]
    actions = ["revoke"]

    def get_readonly_fields(self, request, obj=None):  # type: ignore[no-untyped-def]
        return [*self.readonly_fields, "user"] if obj else self.readonly_fields

    def save_model(self, request, obj, form, change):  # type: ignore[no-untyped-def]
        if not change:
            raw = obj.set_new_key()
            super().save_model(request, obj, form, change)
            messages.warning(
                request,
                f"Токен: {raw}  — скопируйте его сейчас: он показывается один раз и больше нигде не хранится. "
                "Вставьте его в настройки программы (заголовок Authorization: Bearer …).",
            )
            return
        super().save_model(request, obj, form, change)

    @admin.action(description="Отозвать выбранные токены")
    def revoke(self, request, queryset):  # type: ignore[no-untyped-def]
        n = queryset.filter(revoked_at=None).update(revoked_at=timezone.now())
        messages.success(request, f"Отозвано токенов: {n}")

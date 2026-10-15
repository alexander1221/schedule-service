"""Пользователи: вход по корпоративной почте.

Собственная модель пользователя заводится сразу, на этапе 4: Django позволяет
заменить AUTH_USER_MODEL только до первой миграции, а этап 7 (вход по почте,
django-allauth) требует почту как идентификатор. Логина-«username» нет.

Роли — группы Django («Администратор», «Диспетчер», «Преподаватель»), их создаёт
команда ``manage.py init_roles``.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("Нужна почта")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._create(email, password, **extra)


class User(AbstractUser):
    username = None  # type: ignore[assignment]
    email = models.EmailField("корпоративная почта", unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()  # type: ignore[misc]

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"

    def __str__(self) -> str:
        return self.get_full_name() or self.email


class AllowedEmail(models.Model):
    """Белый список: войти (этап 7) сможет только тот, чья почта здесь есть."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Администратор"
        DISPATCHER = "dispatcher", "Диспетчер"
        TEACHER = "teacher", "Преподаватель"

    email = models.EmailField("почта", unique=True)
    role = models.CharField("роль", max_length=16, choices=Role.choices, default=Role.TEACHER)
    note = models.CharField("примечание", max_length=200, blank=True)
    added_at = models.DateTimeField("добавлен", auto_now_add=True)

    class Meta:
        verbose_name = "разрешённый адрес"
        verbose_name_plural = "белый список адресов"
        ordering = ["email"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.email} ({self.get_role_display()})"


class LoginCode(models.Model):
    """Одноразовый код входа, отправленный на почту. Сам код не хранится — только его HMAC."""

    email = models.EmailField("почта", db_index=True)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField("создан", auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField("действует до")
    attempts = models.PositiveSmallIntegerField("попыток ввода", default=0)
    used_at = models.DateTimeField("использован", null=True, blank=True)

    class Meta:
        verbose_name = "код входа"
        verbose_name_plural = "коды входа"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.email} · {self.created_at:%d.%m %H:%M}"

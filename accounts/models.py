"""Пользователи: вход по корпоративной почте.

Собственная модель пользователя заводится сразу, на этапе 4: Django позволяет
заменить AUTH_USER_MODEL только до первой миграции, а этап 7 (вход по почте,
django-allauth) требует почту как идентификатор. Логина-«username» нет.

Роли — группы Django («Администратор», «Диспетчер», «Преподаватель»), их создаёт
команда ``manage.py init_roles``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


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


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiToken(models.Model):
    """Токен доступа к API (/api/v1/) для программ: MCP-сервер агента диспетчера, скрипты.

    Хранится только SHA-256; сам токен показывается один раз при выпуске. Права — как у владельца
    (роль «Диспетчер» и т. п.), владелец должен иметь доступ в админку (is_staff).
    """

    PREFIX = "rsp_"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens", verbose_name="владелец"
    )
    name = models.CharField("для чего", max_length=100, help_text="например: «агент на компьютере диспетчера»")
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField("начало токена", max_length=12, editable=False)
    created_at = models.DateTimeField("выпущен", auto_now_add=True)
    expires_at = models.DateTimeField("действует до", null=True, blank=True, help_text="пусто — бессрочно")
    last_used_at = models.DateTimeField("последнее использование", null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField("отозван", null=True, blank=True)

    class Meta:
        verbose_name = "токен API"
        verbose_name_plural = "токены API"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.prefix}… · {self.name} · {self.user}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > timezone.now())

    def set_new_key(self) -> str:
        """Сгенерировать новый токен; вернуть его открытый текст (больше его нигде не будет)."""
        raw = self.PREFIX + secrets.token_urlsafe(32)
        self.key_hash, self.prefix = _token_hash(raw), raw[:12]
        return raw

    @classmethod
    def issue(cls, user: Any, name: str, days: int | None = None) -> tuple[ApiToken, str]:
        tok = cls(user=user, name=name)
        if days:
            tok.expires_at = timezone.now() + dt.timedelta(days=days)
        raw = tok.set_new_key()
        tok.save()
        return tok, raw

    @classmethod
    def authenticate(cls, raw: str) -> Any | None:
        """Открытый токен → пользователь (или None). Отметку «использован» пишем не чаще раза в минуту."""
        if not raw.startswith(cls.PREFIX):
            return None
        tok = cls.objects.select_related("user").filter(key_hash=_token_hash(raw)).first()
        if tok is None or not tok.is_active or not tok.user.is_active:
            return None
        now = timezone.now()
        if tok.last_used_at is None or now - tok.last_used_at > dt.timedelta(minutes=1):
            cls.objects.filter(pk=tok.pk).update(last_used_at=now)
        return tok.user

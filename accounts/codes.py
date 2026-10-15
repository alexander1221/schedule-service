"""Вход по коду на почту: выдача, проверка, ограничения частоты.

Код — 6 цифр, действует ``CODE_TTL``, на ввод даётся ``MAX_ATTEMPTS`` попыток. Новый код можно
запросить не чаще раза в ``RESEND_AFTER`` и не больше ``MAX_PER_HOUR`` в час. В базе хранится
только HMAC кода (ключ — SECRET_KEY), так что утечка базы не даёт войти.

Войти может только адрес из белого списка (``AllowedEmail``). Для чужого адреса ничего не
отправляется, но ответ пользователю тот же — по форме нельзя выяснить, кто есть в списке.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import AllowedEmail, LoginCode, User

log = logging.getLogger("accounts")

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=60)
MAX_PER_HOUR = 5

ROLE_GROUP = {
    AllowedEmail.Role.TEACHER: "Преподаватель",
    AllowedEmail.Role.DISPATCHER: "Диспетчер",
    AllowedEmail.Role.ADMIN: "Администратор",
}


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def _hash(email: str, code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


@dataclass
class IssueResult:
    sent: bool  # письмо реально ушло (адрес в белом списке)
    wait_s: int = 0  # сколько подождать до следующего кода (0 — можно сейчас)
    error: str = ""  # почта не отправилась (неверные настройки SMTP и т. п.)


def issue_code(email: str) -> IssueResult:
    email = normalize(email)
    if not AllowedEmail.objects.filter(email=email).exists():
        return IssueResult(sent=False)
    now = timezone.now()
    recent = LoginCode.objects.filter(email=email, created_at__gte=now - timedelta(hours=1))
    last = recent.first()
    if last and now - last.created_at < RESEND_AFTER:
        return IssueResult(sent=False, wait_s=int((RESEND_AFTER - (now - last.created_at)).total_seconds()) + 1)
    if recent.count() >= MAX_PER_HOUR:
        return IssueResult(sent=False, wait_s=int((last.created_at + timedelta(hours=1) - now).total_seconds()) + 1)
    code = f"{secrets.randbelow(10**6):06d}"
    LoginCode.objects.filter(email=email, used_at=None).update(used_at=now)  # старые коды больше не действуют
    LoginCode.objects.create(email=email, code_hash=_hash(email, code), expires_at=now + CODE_TTL)
    minutes = int(CODE_TTL.total_seconds() // 60)
    try:
        send_mail(
            subject=f"Код входа: {code}",
            message=(
                f"Ваш код для входа в сервис расписания: {code}\n\n"
                f"Код действует {minutes} минут. Если вы не запрашивали вход, просто удалите это письмо."
            ),
            from_email=None,
            recipient_list=[email],
        )
    except Exception as e:  # SMTP недоступен, неверный пароль приложения и т. п.
        log.error("Код входа для %s не отправлен: %s", email, e)
        return IssueResult(sent=False, error=str(e) or e.__class__.__name__)
    if _console_mail():
        # почта не настроена: письмо выше напечатано в base64 — продублируем код читаемо
        log.warning("Код входа для %s: %s (почта не настроена, письмо не отправлялось)", email, code)
    return IssueResult(sent=True)


def _console_mail() -> bool:
    backend = getattr(settings, "MAILERS", {}).get("default", {}).get("BACKEND", "")
    return backend.endswith("console.EmailBackend")


def check_code(email: str, code: str) -> User | None:
    """Проверить код; при успехе — пользователь (создаётся при первом входе) с ролью из белого списка."""
    email, code = normalize(email), "".join(ch for ch in (code or "") if ch.isdigit())
    now = timezone.now()
    with transaction.atomic():
        lc = (
            LoginCode.objects.select_for_update()
            .filter(email=email, used_at=None, expires_at__gt=now)
            .order_by("-created_at")
            .first()
        )
        if lc is None or lc.attempts >= MAX_ATTEMPTS:
            return None
        lc.attempts += 1
        ok = len(code) == 6 and hmac.compare_digest(lc.code_hash, _hash(email, code))
        if ok or lc.attempts >= MAX_ATTEMPTS:
            lc.used_at = now
        lc.save(update_fields=["attempts", "used_at"])
        if not ok:
            return None
        allowed = AllowedEmail.objects.filter(email=email).first()
        if allowed is None:  # убрали из белого списка, пока код был в пути
            return None
        user, _ = User.objects.get_or_create(email=email)
        if not user.is_active:
            return None
        apply_role(user, AllowedEmail.Role(allowed.role))
        link_teacher(user)
    return user


def apply_role(user: User, role: AllowedEmail.Role) -> None:
    group, _ = Group.objects.get_or_create(name=ROLE_GROUP[role])
    user.groups.add(group)
    if role in (AllowedEmail.Role.DISPATCHER, AllowedEmail.Role.ADMIN) and not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])


def link_teacher(user: User) -> None:
    """Привязать учётку к карточке преподавателя с той же почтой (если ещё не привязана)."""
    from timetable.models import Teacher

    t = Teacher.objects.filter(email=user.email, user=None).first()
    if t is not None and not Teacher.objects.filter(user=user).exists():
        t.user = user
        t.save(update_fields=["user"])


def remaining_attempts(email: str) -> int:
    lc = LoginCode.objects.filter(email=normalize(email), used_at=None, expires_at__gt=timezone.now()).first()
    return MAX_ATTEMPTS - lc.attempts if lc else 0

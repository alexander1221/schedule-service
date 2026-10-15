"""Выпустить токен API для пользователя (например, для MCP-сервера на компьютере диспетчера)."""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import ApiToken, User


class Command(BaseCommand):
    help = "Выпустить токен доступа к API /api/v1/ (права — как у пользователя)"

    def add_arguments(self, parser):
        parser.add_argument("email", help="почта пользователя (должен иметь доступ в админку)")
        parser.add_argument("--name", default="программа", help="для чего токен")
        parser.add_argument("--days", type=int, help="срок действия в днях (по умолчанию бессрочно)")

    def handle(self, *args, **o):
        user = User.objects.filter(email__iexact=o["email"].strip()).first()
        if user is None:
            raise CommandError(f"Пользователь {o['email']} не найден")
        if not user.is_staff:
            raise CommandError("У пользователя нет доступа в админку (is_staff) — API ему недоступен")
        tok, raw = ApiToken.issue(user, o["name"], o["days"])
        self.stdout.write(self.style.SUCCESS(f"Токен «{tok.name}» для {user.email}:"))
        self.stdout.write(raw)
        self.stdout.write("Скопируйте его сейчас — он показывается один раз. Отозвать: админка → «Токены API».")

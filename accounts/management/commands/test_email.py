"""Проверить настройки почты: отправить тестовое письмо."""

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Отправить тестовое письмо на указанный адрес (проверка EMAIL_* в .env)"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("to", help="куда отправить")

    def handle(self, *args, **o):  # type: ignore[no-untyped-def]
        cfg = settings.MAILERS["default"]
        if "console" in cfg["BACKEND"]:
            self.stdout.write(
                self.style.WARNING("EMAIL_HOST не задан — письмо будет напечатано здесь, а не отправлено.")
            )
        else:
            opt = cfg["OPTIONS"]
            self.stdout.write(f"Сервер: {opt['host']}:{opt['port']}, от: {settings.DEFAULT_FROM_EMAIL}")
        try:
            send_mail("Проверка почты сервиса расписания", "Если вы это читаете — почта настроена.", None, [o["to"]])
        except Exception as e:
            raise CommandError(
                f"Письмо не отправилось: {e}\nПроверьте EMAIL_HOST/EMAIL_PORT, логин и ПАРОЛЬ ПРИЛОЖЕНИЯ в .env "
                "и что сеть пускает на порт 465/587."
            ) from e
        self.stdout.write(self.style.SUCCESS(f"Отправлено на {o['to']}"))

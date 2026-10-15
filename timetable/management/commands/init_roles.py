"""Создать группы-роли с правами: Администратор, Диспетчер, Преподаватель."""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

TIMETABLE_MODELS = [
    "semester",
    "pairtime",
    "course",
    "group",
    "subgroup",
    "teacher",
    "teacheravailability",
    "discipline",
    "roomtype",
    "room",
    "syncblock",
    "mergegroup",
    "workloaditem",
    "pin",
    "weightpreset",
    "schedulevariant",
    "scheduleassignment",
    "solverun",
]


class Command(BaseCommand):
    help = "Создать (или обновить) роли и их права. Идемпотентно."

    def handle(self, *args, **o):
        all_tt = Permission.objects.filter(
            content_type__app_label="timetable", content_type__model__in=TIMETABLE_MODELS
        )
        dispatcher, _ = Group.objects.get_or_create(name="Диспетчер")
        dispatcher.permissions.set(all_tt)
        admin, _ = Group.objects.get_or_create(name="Администратор")
        admin.permissions.set(list(all_tt) + list(Permission.objects.filter(content_type__app_label="accounts")))
        teacher, _ = Group.objects.get_or_create(name="Преподаватель")
        teacher.permissions.set(
            Permission.objects.filter(
                content_type__app_label="timetable",
                codename__in=["view_teacheravailability", "change_teacheravailability", "view_schedulevariant"],
            )
        )
        self.stdout.write(self.style.SUCCESS("Роли: Администратор, Диспетчер, Преподаватель — готово"))

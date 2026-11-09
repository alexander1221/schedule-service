"""Регрессии аудита: отзыв прав, утверждение и повторные переносы."""

import datetime as dt
from dataclasses import replace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse

from accounts.codes import apply_role
from accounts.models import AllowedEmail, ApiToken, User
from scheduler.domain import Assignment, Schedule, SyncBlock, Week
from scheduler.solver import SolveParams
from scheduler.validator import validate
from tests.helpers import add_item, tiny
from timetable import models as m
from timetable.services.problem import build_problem, import_problem
from timetable.services.rebuild import LessonMove, RebuildError, rebuild_variant
from timetable.services.runs import RunConflict, enqueue, request_cancel
from timetable.services.variants import ApproveError, approve_variant, load_variant, run_and_save, save_variant


class AuditFixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("init_roles", verbosity=0)
        p = tiny(days=[1, 2], n_teachers=2)
        add_item(p, "lesson", n=4)
        add_item(p, "other", n=2, teacher="t2", disc="d2")
        sem, ids = import_problem(p, "Аудит")
        assignments = [
            Assignment("lesson", Week.BOTH, "1-1", "r1"),
            Assignment("lesson", Week.BOTH, "1-2", "r1"),
            Assignment("other", Week.BOTH, "1-3", "r1"),
        ]
        variant = save_variant(sem, build_problem(sem), ids.schedule(Schedule(assignments)), "Исходный")
        cls.variant_id = variant.pk
        cls.sem_id = sem.pk
        cls.item_id = variant.assignments.get(day=1, pair=1).item_id

    def setUp(self):
        self.variant = m.ScheduleVariant.objects.get(pk=self.variant_id)
        self.sem = m.Semester.objects.get(pk=self.sem_id)

    def move(self, source="1-1", target="2-1", week=Week.BOTH):
        return LessonMove(self.item_id, week, source, target)

    def test_role_downgrade_revokes_staff_access_and_token(self):
        user = User.objects.create_user("role@college.example")
        extra = Group.objects.create(name="Дополнительная группа")
        user.groups.add(extra)
        allowed = AllowedEmail.objects.create(email=user.email, role=AllowedEmail.Role.ADMIN)
        user.refresh_from_db()
        self.assertTrue(user.has_perm("timetable.change_workloaditem"))
        self.client.force_login(user)
        tok, raw = ApiToken.issue(user, "Проверка отзыва роли")
        allowed.role = AllowedEmail.Role.TEACHER
        allowed.save(update_fields=["role"])
        user = User.objects.get(pk=user.pk)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_perm("timetable.change_workloaditem"))
        self.assertEqual(set(user.groups.values_list("name", flat=True)), {extra.name, "Преподаватель"})
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 302)
        # Сам токен остаётся, но его владелец больше не проходит проверку is_staff в TokenAuth.
        self.assertFalse(ApiToken.lookup(raw).user.is_staff)
        tok.refresh_from_db()
        self.assertIsNone(tok.revoked_at)

    def test_apply_role_clears_cached_permissions_and_keeps_superuser(self):
        user = User.objects.create_user("cached@college.example")
        apply_role(user, AllowedEmail.Role.ADMIN)
        self.assertTrue(user.has_perm("timetable.change_workloaditem"))
        apply_role(user, AllowedEmail.Role.TEACHER)
        self.assertFalse(user.has_perm("timetable.change_workloaditem"))
        root = User.objects.create_superuser("root@college.example", "example-password")
        apply_role(root, AllowedEmail.Role.TEACHER)
        root.refresh_from_db()
        self.assertTrue(root.is_staff and root.is_superuser)

    def test_admin_form_cannot_set_approved_status(self):
        root = User.objects.create_superuser("admin@college.example", "example-password")
        request = RequestFactory().get("/admin/")
        request.user = root
        model_admin = admin.site._registry[m.ScheduleVariant]
        form = model_admin.get_form(request, self.variant)
        self.assertNotIn("status", form.base_fields)
        self.client.force_login(root)
        rows = list(self.variant.assignments.all())
        data = {
            "name": "Проверка формы",
            "status": "approved",
            "note": "",
            "assignments-TOTAL_FORMS": len(rows),
            "assignments-INITIAL_FORMS": len(rows),
            "assignments-MIN_NUM_FORMS": 0,
            "assignments-MAX_NUM_FORMS": 0,
        }
        for i, row in enumerate(rows):
            data[f"assignments-{i}-id"] = row.pk
        response = self.client.post(reverse("admin:timetable_schedulevariant_change", args=[self.variant.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.name, "Проверка формы")
        self.assertEqual(self.variant.status, m.ScheduleVariant.Status.DRAFT)

    def test_approval_replaces_previous_and_rejects_invalid_variant(self):
        problem, sched = load_variant(self.variant)
        other = save_variant(self.sem, problem, sched, "Другой")
        approve_variant(self.variant)
        approve_variant(other)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.status, m.ScheduleVariant.Status.DRAFT)
        self.assertEqual(list(self.sem.variants.filter(status="approved")), [other])
        invalid = save_variant(self.sem, problem, Schedule([]), "Неполный")
        with self.assertRaises(ApproveError):
            approve_variant(invalid)
        self.assertEqual(list(self.sem.variants.filter(status="approved")), [other])

    def test_database_prevents_two_approved_variants(self):
        problem, sched = load_variant(self.variant)
        other = save_variant(self.sem, problem, sched, "Другой")
        approve_variant(self.variant)
        with self.assertRaises(IntegrityError), transaction.atomic():
            m.ScheduleVariant.objects.filter(pk=other.pk).update(status="approved")

    def test_repeated_move_replaces_only_its_pin_and_remains_solvable(self):
        m.Pin.objects.create(item_id=self.item_id, week="both", day=1, pair=1)
        untouched = m.Pin.objects.create(item_id=self.item_id, week="both", day=1, pair=2)
        first = rebuild_variant(self.variant, [self.move()], time_limit_s=10)
        _, sched = load_variant(self.variant)
        sched.assignments = [replace(a, slot_id="2-1") if a.slot_id == "1-1" else a for a in sched.assignments]
        next_variant = save_variant(self.sem, build_problem(self.sem), sched, "После переноса")
        request_cancel(first)
        rebuild_variant(next_variant, [self.move("2-1", "2-2")], time_limit_s=10)
        self.assertTrue(m.Pin.objects.filter(pk=untouched.pk).exists())
        self.assertEqual(set(m.Pin.objects.filter(item_id=self.item_id).values_list("day", "pair")), {(1, 2), (2, 2)})
        result, saved = run_and_save(self.sem, SolveParams(time_limit_s=5, variants=1, workers=1))
        self.assertTrue(saved, result.diagnostics)
        p, s = load_variant(saved[0])
        self.assertTrue(validate(p, s).ok)

    def test_active_run_conflict_leaves_pins_unchanged(self):
        pin = m.Pin.objects.create(item_id=self.item_id, week="both", day=1, pair=1)
        enqueue(self.sem)
        with self.assertRaises(RunConflict):
            rebuild_variant(self.variant, [self.move()])
        pin.refresh_from_db()
        self.assertEqual((pin.day, pin.pair), (1, 1))
        self.assertEqual(m.Pin.objects.count(), 1)

    def test_failure_after_pin_changes_rolls_back_pins_and_run(self):
        pin = m.Pin.objects.create(item_id=self.item_id, week="both", day=1, pair=1)
        with (
            patch(
                "timetable.services.rebuild.m.Pin.objects.get_or_create", side_effect=RuntimeError("Проверка отката")
            ),
            self.assertRaises(RuntimeError),
        ):
            rebuild_variant(self.variant, [self.move()])
        self.assertTrue(m.Pin.objects.filter(pk=pin.pk, day=1, pair=1).exists())
        self.assertEqual(m.Pin.objects.count(), 1)
        self.assertFalse(m.SolveRun.objects.exists())

    def test_move_preserves_pin_of_other_cycle_week(self):
        problem, sched = load_variant(self.variant)
        first = sched.assignments[0]
        sched.assignments = [replace(first, week=Week.ODD), replace(first, week=Week.EVEN), *sched.assignments[1:]]
        split = save_variant(self.sem, problem, sched, "Недели отдельно")
        m.Pin.objects.create(item_id=self.item_id, week="both", day=1, pair=first.slot_id.split("-")[1])
        rebuild_variant(split, [self.move(first.slot_id, "2-1", Week.ODD)])
        self.assertEqual(
            set(m.Pin.objects.filter(item_id=self.item_id).values_list("week", "day", "pair")),
            {("even", 1, int(first.slot_id.split("-")[1])), ("odd", 2, 1)},
        )

    def test_invalid_source_or_destination_does_not_enqueue(self):
        for move in (self.move("6-12"), self.move(target="6-12")):
            with self.subTest(move=move), self.assertRaises(RebuildError):
                rebuild_variant(self.variant, [move])
        self.assertFalse(m.Pin.objects.exists())
        self.assertFalse(m.SolveRun.objects.exists())

    def test_moves_cannot_overlap_other_occurrence_of_same_item(self):
        with self.assertRaises(RebuildError):
            rebuild_variant(self.variant, [self.move(target="1-2")])
        self.assertFalse(m.SolveRun.objects.exists())
        self.assertFalse(m.Pin.objects.exists())

    def test_sync_block_moves_only_as_a_whole(self):
        p = tiny(days=[1, 2], n_teachers=2)
        add_item(p, "a", groups=[], teacher="t1", headcount=10)
        add_item(p, "b", groups=[], teacher="t2", headcount=10)
        p.blocks.append(SyncBlock("block", "Английский", ["g1"], ["a", "b"]))
        sem, ids = import_problem(p, "Блок")
        sched = ids.schedule(
            Schedule(
                [
                    Assignment("a", Week.BOTH, "1-1", "r1"),
                    Assignment("b", Week.BOTH, "1-1", "r2"),
                ]
            )
        )
        variant = save_variant(sem, build_problem(sem), sched, "Блок")
        moves = [LessonMove(a.item_id, Week(a.week), "1-1", "2-1") for a in variant.assignments.all()]
        with self.assertRaises(RebuildError):
            rebuild_variant(variant, moves[:1])
        self.assertFalse(m.SolveRun.objects.exists())
        rebuild_variant(variant, moves)
        self.assertEqual(m.Pin.objects.filter(item__semester=sem, day=2, pair=1).count(), 2)


class ApprovedVariantMigrationTests(TransactionTestCase):
    def test_existing_duplicates_keep_latest_variant_without_deletion(self):
        before = [("timetable", "0007_v080_solve_run")]
        after = [("timetable", "0008_v0140_unique_approved_variant")]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            Semester = apps.get_model("timetable", "Semester")
            Variant = apps.get_model("timetable", "ScheduleVariant")
            sem = Semester.objects.create(name="Старые данные", start_date=dt.date(2026, 9, 1))
            old = Variant.objects.create(semester=sem, name="Первый", status="approved")
            new = Variant.objects.create(semester=sem, name="Последний", status="approved")
            MigrationExecutor(connection).migrate(after)
            self.assertEqual(m.ScheduleVariant.objects.get(pk=old.pk).status, "draft")
            self.assertEqual(m.ScheduleVariant.objects.get(pk=new.pk).status, "approved")
            self.assertEqual(m.ScheduleVariant.objects.count(), 2)
        finally:
            MigrationExecutor(connection).migrate(after)

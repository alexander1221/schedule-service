"""Админка: страницы открываются, сетка предпочтений сохраняется, массовые операции работают."""

import pytest
from django.urls import reverse

from timetable import models as m
from timetable.xlsx.workload import export_workload

pytestmark = pytest.mark.django_db

MODELS = [
    "semester",
    "course",
    "group",
    "teacher",
    "teacheravailability",
    "discipline",
    "roomtype",
    "room",
    "workloaditem",
    "syncblock",
    "mergegroup",
    "pin",
    "weightpreset",
    "schedulevariant",
]


@pytest.mark.parametrize("model", MODELS)
def test_changelist_and_add_pages_open(admin_client_ru, synthetic, model):
    assert admin_client_ru.get(reverse(f"admin:timetable_{model}_changelist")).status_code == 200
    assert admin_client_ru.get(reverse(f"admin:timetable_{model}_add")).status_code == 200


def test_change_pages_open(admin_client_ru, synthetic):
    sem = synthetic[0]
    for obj in [
        sem,
        m.WorkloadItem.objects.first(),
        m.Group.objects.first(),
        m.Course.objects.first(),
        m.TeacherAvailability.objects.first(),
        m.SyncBlock.objects.first(),
        m.Teacher.objects.first(),
    ]:
        url = reverse(f"admin:timetable_{obj._meta.model_name}_change", args=[obj.pk])
        r = admin_client_ru.get(url)
        assert r.status_code == 200, url


def test_workload_changelist_filtered_by_semester(admin_client_ru, synthetic):
    sem = synthetic[0]
    r = admin_client_ru.get(reverse("admin:timetable_workloaditem_changelist") + f"?semester__id__exact={sem.pk}")
    assert r.status_code == 200
    assert "Импорт xlsx" in r.content.decode()


def test_pref_grid_renders_and_saves(admin_client_ru, synthetic):
    av = m.TeacherAvailability.objects.first()
    url = reverse("admin:timetable_teacheravailability_change", args=[av.pk])
    html = admin_client_ru.get(url).content.decode()
    assert 'class="pref-grid"' in html and 'name="prefs__1-1"' in html
    data = {"teacher": av.teacher_id, "semester": av.semester_id, "submitted_at_0": "", "submitted_at_1": ""}
    for d in av.semester.days:
        for p in av.semester.pair_numbers():
            data[f"prefs__{d}-{p}"] = "0"
    data["prefs__1-1"] = "-2"
    data["prefs__3-4"] = "2"
    r = admin_client_ru.post(url, data)
    assert r.status_code == 302, r.content.decode()[:2000]
    av.refresh_from_db()
    assert av.prefs == {"1-1": -2, "3-4": 2}


def test_precheck_and_downloads(admin_client_ru, synthetic):
    sem = synthetic[0]
    r = admin_client_ru.get(reverse("admin:timetable_semester_precheck", args=[sem.pk]))
    assert r.status_code == 200 and "Ошибки" in r.content.decode()
    r = admin_client_ru.get(reverse("admin:timetable_semester_workload_export", args=[sem.pk]))
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = admin_client_ru.get(reverse("admin:timetable_semester_refs_export", args=[sem.pk]))
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = admin_client_ru.get(reverse("admin:timetable_semester_problem_json", args=[sem.pk]))
    assert r.status_code == 200 and b"schema_version" in r.content


def test_import_view_dry_run_then_apply(admin_client_ru, synthetic):
    from django.core.files.uploadedfile import SimpleUploadedFile

    sem = synthetic[0]
    url = reverse("admin:timetable_semester_workload_import", args=[sem.pk])
    data = export_workload(sem)
    r = admin_client_ru.post(url, {"file": SimpleUploadedFile("w.xlsx", data), "mode": "merge", "dry_run": "on"})
    assert r.status_code == 200 and "Проверка пройдена" in r.content.decode()
    r = admin_client_ru.post(url, {"file": SimpleUploadedFile("w.xlsx", data), "mode": "merge"})
    assert r.status_code == 302


def test_mass_create(admin_client_ru, synthetic):
    sem = synthetic[0]
    groups = list(m.Group.objects.filter(course__semester=sem)[:3])
    before = m.WorkloadItem.objects.count()
    r = admin_client_ru.post(
        reverse("admin:timetable_workload_mass"),
        {
            "semester": sem.pk,
            "discipline": m.Discipline.objects.first().pk,
            "kind": "practice",
            "teachers": [m.Teacher.objects.first().pk],
            "groups": [g.pk for g in groups],
            "pairs_per_cycle": 2,
            "format": "in_person",
            "room_type": m.RoomType.objects.get(code="regular").pk,
        },
    )
    assert r.status_code == 302, r.content.decode()[:3000]
    assert m.WorkloadItem.objects.count() == before + 3
    r = admin_client_ru.post(
        reverse("admin:timetable_workload_mass"),
        {
            "semester": sem.pk,
            "discipline": m.Discipline.objects.first().pk,
            "kind": "lecture",
            "teachers": [m.Teacher.objects.first().pk],
            "groups": [g.pk for g in groups],
            "pairs_per_cycle": 2,
            "format": "in_person",
            "room_type": m.RoomType.objects.get(code="lecture").pk,
            "as_merge": "on",
        },
    )
    assert r.status_code == 302
    assert m.MergeGroup.objects.filter(semester=sem).latest("pk").items.count() == 3


def test_duplicate_action(admin_client_ru, synthetic):
    item = m.WorkloadItem.objects.filter(groups__isnull=False).first()
    before = m.WorkloadItem.objects.count()
    r = admin_client_ru.post(
        reverse("admin:timetable_workloaditem_changelist"), {"action": "duplicate", "_selected_action": [item.pk]}
    )
    assert r.status_code == 302
    copy = m.WorkloadItem.objects.latest("pk")
    assert m.WorkloadItem.objects.count() == before + 1
    assert set(copy.groups.all()) == set(item.groups.all()) and "(копия)" in copy.comment


def test_weight_preset_form(admin_client_ru, db):
    from scheduler.criteria import DEFAULT_WEIGHTS, Crit

    data = {
        "name": "Мой",
        "mode": "weighted",
        "is_default": "on",
        "note": "",
        "lex_levels": "[]",
        "c_-1": 10,
        "c_1": -1,
        "c_2": -2,
    }
    data |= {f"w_{c.value}": DEFAULT_WEIGHTS.get(c, 0) for c in Crit}
    data["w_group_gaps"] = 77
    r = admin_client_ru.post(reverse("admin:timetable_weightpreset_add"), data)
    assert r.status_code == 302, r.content.decode()[:3000]
    p = m.WeightPreset.objects.get(name="Мой")
    assert p.weights["group_gaps"] == 77 and p.is_default


def test_approve_variant(admin_client_ru, synthetic):
    from timetable.services.problem import build_problem
    from timetable.services.variants import save_variant

    sem, ids, planted, _ = synthetic
    v = save_variant(sem, build_problem(sem), ids.schedule(planted), "Посаженное")
    r = admin_client_ru.post(
        reverse("admin:timetable_schedulevariant_changelist"), {"action": "approve", "_selected_action": [v.pk]}
    )
    assert r.status_code == 302
    v.refresh_from_db()
    assert v.status == "approved"
    r = admin_client_ru.get(reverse("admin:timetable_schedulevariant_change", args=[v.pk]))
    assert r.status_code == 200

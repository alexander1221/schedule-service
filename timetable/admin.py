"""Админка диспетчера (этап 4): справочники, нагрузка, блоки, объединения, пресеты.

Полноценный интерфейс диспетчера (React, перетаскивание, сравнение вариантов) —
этап 6; здесь — рабочий ввод данных, импорт/экспорт xlsx и проверки задачи.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.core.validators import MaxValueValidator
from django.db import transaction
from django.db.models import Count, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from scheduler.criteria import DEFAULT_PREF_COST, DEFAULT_WEIGHTS, LABELS, Crit
from scheduler.domain import DAY_NAMES
from scheduler.precheck import precheck
from scheduler.serialization import problem_to_dict
from scheduler.validator import validate

from . import models as m
from .services.problem import ProblemBuildError, build_problem, manual_items_status
from .services.runs import RunConflict, active_run, enqueue, max_workers, request_cancel
from .services.variants import ApproveError, approve_variant, default_preset, load_variant
from .widgets import BusyGridField, BusyGridWidget, PrefGridField, PrefGridWidget, grid_for
from .xlsx.references import export_references, import_references
from .xlsx.workload import export_workload, import_workload

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(data: bytes, filename: str) -> HttpResponse:
    resp = HttpResponse(data, content_type=XLSX)
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


class PrefGridMixin:
    """Подменяет JSON-поля сеток предпочтений на таблицу «дни × пары» с сеткой нужного семестра."""

    pref_fields: tuple[str, ...] = ()

    def semester_of(self, obj: Any) -> m.Semester | None:
        return None

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        form = super().get_form(request, obj, change, **kwargs)  # type: ignore[misc]
        days, pairs = grid_for(self.semester_of(obj) if obj else None)
        for name in self.pref_fields:
            if name in form.base_fields:
                old = form.base_fields[name]
                form.base_fields[name] = PrefGridField(
                    required=False,
                    label=old.label,
                    help_text=old.help_text,
                    widget=PrefGridWidget(days, pairs),
                    initial=dict,
                )
        return form


# ====================================================================== семестр


class PairTimeInline(admin.TabularInline):
    model = m.PairTime
    extra = 0


class CourseInline(admin.TabularInline):
    model = m.Course
    extra = 0
    fields = [
        "number",
        "level",
        "max_per_day",
        "max_per_day_hard",
        "min_per_day",
        "min_per_day_hard",
        "max_consecutive",
        "max_consecutive_hard",
    ]
    show_change_link = True


class WorkloadImportForm(forms.Form):
    file = forms.FileField(label="Файл xlsx")
    mode = forms.ChoiceField(
        label="Режим",
        choices=[
            ("merge", "добавить новые и обновить строки с ID"),
            ("replace", "как в файле: удалить элементы семестра, которых нет в файле"),
        ],
        initial="merge",
    )
    dry_run = forms.BooleanField(label="Только проверить, ничего не записывать", required=False, initial=True)


class DraftForm(forms.Form):
    file = forms.FileField(label="Текущее расписание (xlsx)")


class ReferencesImportForm(forms.Form):
    file = forms.FileField(label="Файл xlsx")
    dry_run = forms.BooleanField(label="Только проверить, ничего не записывать", required=False, initial=True)


class SolveForm(forms.Form):
    minutes = forms.FloatField(
        label="Время на один вариант, мин",
        min_value=0.5,
        max_value=120,
        initial=5,
        help_text="Чем больше, тем лучше расписание. Для семестра колледжа разумно 3–10 минут.",
    )
    variants = forms.IntegerField(label="Сколько вариантов", min_value=1, max_value=10, initial=3)
    preset = forms.ModelChoiceField(
        m.WeightPreset.objects.all(), required=False, label="Пресет весов", empty_label="отмеченный «по умолчанию»"
    )
    reference = forms.ModelChoiceField(
        m.ScheduleVariant.objects.none(),
        required=False,
        label="Пересобрать от варианта",
        empty_label="нет — строить с нуля",
        help_text="Новое расписание будет как можно ближе к выбранному: меняется только то, что нужно "
        "(новые пожелания, закрепления, правки нагрузки).",
    )
    name_prefix = forms.CharField(label="Название вариантов", max_length=80, initial="Вариант")
    workers = forms.IntegerField(label="Ядер процессора", min_value=1)
    min_diff = forms.IntegerField(
        label="Варианты отличаются хотя бы на, пар", min_value=0, initial=10, help_text="Обычно менять не нужно."
    )
    seed = forms.IntegerField(
        label="Seed", initial=0, help_text="Другое число — другой путь поиска при тех же данных. Обычно 0."
    )

    def __init__(self, sem: m.Semester, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        f = self.fields
        f["reference"].queryset = sem.variants.order_by("status", "-created_at")  # type: ignore[attr-defined]
        approved = sem.variants.filter(status=m.ScheduleVariant.Status.APPROVED).first()
        f["reference"].help_text += f" Утверждённый сейчас: «{approved.name}»." if approved else ""
        f["workers"].max_value = max_workers()
        f["workers"].initial = max_workers()
        f["workers"].validators.append(MaxValueValidator(max_workers()))
        f["workers"].help_text = f"Не больше {max_workers()} — остальное оставлено сайту (SOLVER_MAX_WORKERS)."
        f["preset"].initial = default_preset()


@admin.register(m.Semester)
class SemesterAdmin(admin.ModelAdmin):
    list_display = ["name", "start_date", "first_week", "saturday", "status", "n_groups", "n_items", "tools"]
    list_filter = ["status"]
    search_fields = ["name"]
    inlines = [PairTimeInline, CourseInline]
    fieldsets = (
        (
            None,
            {"fields": ("name", "start_date", "end_date", "first_week", "saturday", "status", "availability_deadline")},
        ),
        (
            "Правила учебного заведения",
            {
                "fields": (
                    "format_gap_min",
                    "discipline_per_day",
                    "heavy_threshold",
                    "heavy_max_consecutive",
                    "teacher_max_per_day",
                    "teacher_max_per_day_hard",
                    "teacher_min_per_day",
                    "hours_per_pair",
                    "check_capacity",
                )
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.Semester]:
        return (
            super()
            .get_queryset(request)
            .annotate(_items=Count("items", distinct=True), _groups=Count("courses__groups", distinct=True))
        )

    @admin.display(description="групп", ordering="_groups")
    def n_groups(self, obj: m.Semester) -> int:
        return obj._groups  # type: ignore[attr-defined]

    @admin.display(description="элементов нагрузки", ordering="_items")
    def n_items(self, obj: m.Semester) -> int:
        return obj._items  # type: ignore[attr-defined]

    @admin.display(description="действия")
    def tools(self, obj: m.Semester) -> str:
        links = [
            ("Построить расписание", "admin:timetable_semester_solve"),
            ("Проверить", "admin:timetable_semester_precheck"),
            ("Нагрузка ⇣xlsx", "admin:timetable_semester_workload_export"),
            ("Нагрузка ⇡xlsx", "admin:timetable_semester_workload_import"),
            ("Справочники ⇣", "admin:timetable_semester_refs_export"),
            ("Справочники ⇡", "admin:timetable_semester_refs_import"),
            ("Задача JSON", "admin:timetable_semester_problem_json"),
            ("Пожелания", "admin:timetable_semester_wishes"),
        ]
        html = format_html_join(" · ", '<a href="{}">{}</a>', ((reverse(u, args=[obj.pk]), t) for t, u in links))
        return format_html('<a href="/app/?semester={}"><b>Интерфейс</b></a> · {}', obj.pk, html)

    def save_model(self, request: HttpRequest, obj: m.Semester, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        if not change:
            obj.ensure_default_pairs()

    def save_related(self, request: HttpRequest, form: Any, formsets: Any, change: bool) -> None:
        super().save_related(request, form, formsets, change)
        if not change and not form.instance.courses.exists():
            # без курсов не загрузятся группы: заводим 1–4 курс с настройками по умолчанию
            from .services.problem import add_default_courses

            add_default_courses(form.instance)

    def get_urls(self) -> list[Any]:
        w = self.admin_site.admin_view
        return [
            path("<int:pk>/solve/", w(self.solve_view), name="timetable_semester_solve"),
            path("<int:pk>/precheck/", w(self.precheck_view), name="timetable_semester_precheck"),
            path("<int:pk>/workload.xlsx", w(self.workload_export), name="timetable_semester_workload_export"),
            path("<int:pk>/workload-import/", w(self.workload_import), name="timetable_semester_workload_import"),
            path("<int:pk>/references.xlsx", w(self.refs_export), name="timetable_semester_refs_export"),
            path("<int:pk>/references-import/", w(self.refs_import), name="timetable_semester_refs_import"),
            path("<int:pk>/problem.json", w(self.problem_json), name="timetable_semester_problem_json"),
            path("<int:pk>/wishes/", w(self.wishes_view), name="timetable_semester_wishes"),
            path("draft-from-schedule/", w(self.draft_view), name="timetable_draft_from_schedule"),
            *super().get_urls(),
        ]

    def _ctx(self, request: HttpRequest, sem: m.Semester, title: str, **kw: Any) -> dict[str, Any]:
        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "semester": sem,
            "title": title,
            **kw,
        }

    def _perm(self, request: HttpRequest, change: bool = False) -> None:
        from django.core.exceptions import PermissionDenied

        ok = request.user.has_perm("timetable.change_workloaditem" if change else "timetable.view_workloaditem")
        if not ok:
            raise PermissionDenied

    def solve_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Форма «Построить расписание»: ставит расчёт в очередь и ведёт на страницу его хода."""
        from django.core.exceptions import PermissionDenied

        if not request.user.has_perm("timetable.add_solverun"):
            raise PermissionDenied
        sem = get_object_or_404(m.Semester, pk=pk)
        form = SolveForm(sem, request.POST or None)
        if request.method == "POST" and form.is_valid():
            d = form.cleaned_data
            try:
                run = enqueue(
                    sem,
                    time_limit_s=round(d["minutes"] * 60),
                    variants=d["variants"],
                    workers=d["workers"],
                    seed=d["seed"],
                    min_diff=d["min_diff"],
                    preset=d["preset"] or default_preset(),
                    reference=d["reference"],
                    name_prefix=d["name_prefix"],
                    user=request.user,
                )
            except RunConflict as e:
                messages.error(request, str(e))
                if e.active is not None:
                    return HttpResponseRedirect(reverse("admin:timetable_solverun_change", args=[e.active.pk]))
            else:
                messages.success(request, f"Расчёт №{run.pk} поставлен в очередь.")
                return HttpResponseRedirect(reverse("admin:timetable_solverun_change", args=[run.pk]))
        return render(
            request,
            "admin/timetable/solve.html",
            self._ctx(
                request,
                sem,
                f"Построить расписание: {sem}",
                form=form,
                active=active_run(sem),
                recent=sem.runs.select_related("created_by")[:5],  # type: ignore[attr-defined]
            ),
        )

    def precheck_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        self._perm(request)
        sem = get_object_or_404(m.Semester, pk=pk)
        try:
            problem = build_problem(sem)
            findings = precheck(problem)
            build_error = ""
            stats = {
                "групп": len(problem.groups),
                "преподавателей": len(problem.teachers),
                "элементов нагрузки": len(problem.items),
                "пар за цикл": sum(i.pairs_per_cycle for i in problem.items),
                "аудиторий": len(problem.active_rooms),
                "синхронных блоков": len(problem.blocks),
                "объединений": len(problem.merges),
                "закреплений": len(problem.pins),
            }
        except ProblemBuildError as e:
            findings, build_error, stats = [], str(e), {}
        manual = manual_items_status(sem)
        manual_skipped = list(
            m.WorkloadItem.objects.filter(pk__in=[pk for pk, full in manual.items() if not full])
            .select_related("discipline")
            .prefetch_related("groups")
        )
        no_teachers = m.WorkloadItem.objects.filter(semester=sem, teachers=None).count()
        no_avail = m.Teacher.objects.filter(items__semester=sem).exclude(availability__semester=sem).distinct().count()
        return render(
            request,
            "admin/timetable/precheck.html",
            self._ctx(
                request,
                sem,
                f"Проверка задачи: {sem}",
                findings=findings,
                build_error=build_error,
                stats=stats,
                errors=[f for f in findings if f.level == "error"],
                warnings=[f for f in findings if f.level == "warning"],
                hints=[f for f in findings if f.level == "info"],
                manual_skipped=manual_skipped,
                manual_fixed=sum(1 for full in manual.values() if full),
                no_teachers=no_teachers,
                no_avail=no_avail,
            ),
        )

    def workload_export(self, request: HttpRequest, pk: int) -> HttpResponse:
        self._perm(request)
        sem = get_object_or_404(m.Semester, pk=pk)
        return _xlsx_response(export_workload(sem), f"workload_{sem.pk}_{dt.date.today():%Y%m%d}.xlsx")

    def workload_import(self, request: HttpRequest, pk: int) -> HttpResponse:
        self._perm(request, change=True)
        sem = get_object_or_404(m.Semester, pk=pk)
        report = None
        form = WorkloadImportForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            report = import_workload(
                sem,
                form.cleaned_data["file"],
                dry_run=form.cleaned_data["dry_run"],
                replace=form.cleaned_data["mode"] == "replace",
            )
            if report.applied:
                messages.success(request, f"Нагрузка загружена: {report.summary()}")
                return HttpResponseRedirect(
                    reverse("admin:timetable_workloaditem_changelist") + f"?semester__id__exact={sem.pk}"
                )
        return render(
            request,
            "admin/timetable/import.html",
            self._ctx(
                request,
                sem,
                f"Импорт нагрузки: {sem}",
                form=form,
                report=report,
                template_url=reverse("admin:timetable_semester_workload_export", args=[sem.pk]),
                hint="Скачайте текущую нагрузку как шаблон, поправьте в Excel и загрузите обратно. "
                "Сначала оставьте галочку «только проверить».",
            ),
        )

    def refs_export(self, request: HttpRequest, pk: int) -> HttpResponse:
        self._perm(request)
        sem = get_object_or_404(m.Semester, pk=pk)
        return _xlsx_response(export_references(sem), f"references_{sem.pk}_{dt.date.today():%Y%m%d}.xlsx")

    def refs_import(self, request: HttpRequest, pk: int) -> HttpResponse:
        self._perm(request, change=True)
        sem = get_object_or_404(m.Semester, pk=pk)
        report = None
        form = ReferencesImportForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            report = import_references(sem, form.cleaned_data["file"], dry_run=form.cleaned_data["dry_run"])
            if report.applied:
                messages.success(request, f"Справочники загружены: {report.summary()}")
                return HttpResponseRedirect(reverse("admin:timetable_semester_change", args=[sem.pk]))
        return render(
            request,
            "admin/timetable/import.html",
            self._ctx(
                request,
                sem,
                f"Импорт справочников: {sem}",
                form=form,
                report=report,
                template_url=reverse("admin:timetable_semester_refs_export", args=[sem.pk]),
                hint="Листы: Преподаватели, Оборудование, Дисциплины, Типы аудиторий, Площадки, Аудитории, "
                "Группы, Подгруппы, Доступность, Занятость аудиторий. "
                "Можно загружать не все. Импорт только добавляет и обновляет, ничего не удаляет.",
            ),
        )

    def draft_view(self, request: HttpRequest) -> HttpResponse:
        """Черновик справочников и нагрузки из текущего расписания учебного отдела (xlsx → zip)."""
        from .importers.current_schedule import aggregate, parse_workbook
        from .importers.draft_writer import draft_zip

        self._perm(request, change=True)
        form = DraftForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            try:
                d = parse_workbook(form.cleaned_data["file"])
            except Exception as e:  # битый файл, не xlsx
                messages.error(request, f"Не удалось прочитать файл: {e}")
            else:
                items, blocks = aggregate(d)
                resp = HttpResponse(draft_zip(d, items, blocks), content_type="application/zip")
                resp["Content-Disposition"] = 'attachment; filename="schedule_draft.zip"'
                return resp
        return render(
            request,
            "admin/timetable/draft.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "form": form,
                "title": "Черновик из текущего расписания",
            },
        )

    def wishes_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Кто из преподавателей с нагрузкой в семестре уже сдал пожелания и может ли войти."""
        from accounts.models import AllowedEmail

        self._perm(request)
        sem = get_object_or_404(m.Semester, pk=pk)
        allowed = set(AllowedEmail.objects.values_list("email", flat=True))
        submitted = dict(m.TeacherAvailability.objects.filter(semester=sem).values_list("teacher_id", "submitted_at"))
        teachers = m.Teacher.objects.filter(items__semester=sem).distinct().select_related("user").order_by("name")
        rows = []
        for t in teachers:
            rows.append(
                {
                    "teacher": t,
                    "email": t.email,
                    "allowed": bool(t.email and t.email in allowed),
                    "user": t.user,
                    "submitted": submitted.get(t.pk),
                }
            )
        stats = {
            "total": len(rows),
            "no_email": sum(1 for r in rows if not r["email"]),
            "allowed": sum(1 for r in rows if r["allowed"]),
            "entered": sum(1 for r in rows if r["user"]),
            "submitted": sum(1 for r in rows if r["submitted"]),
        }
        return render(
            request,
            "admin/timetable/wishes.html",
            self._ctx(request, sem, f"Пожелания преподавателей: {sem}", rows=rows, stats=stats),
        )

    def problem_json(self, request: HttpRequest, pk: int) -> HttpResponse:
        self._perm(request)
        sem = get_object_or_404(m.Semester, pk=pk)
        import json

        try:
            data = problem_to_dict(build_problem(sem))
        except ProblemBuildError as e:
            messages.error(request, str(e))
            return HttpResponseRedirect(reverse("admin:timetable_semester_change", args=[sem.pk]))
        resp = HttpResponse(json.dumps(data, ensure_ascii=False, indent=1), content_type="application/json")
        resp["Content-Disposition"] = f'attachment; filename="problem_{sem.pk}.json"'
        return resp


class LanguageBlockForm(forms.Form):
    """Языковой блок на весь курс: все группы курса распускаются, все подгруппы — в одно время."""

    discipline = forms.ModelChoiceField(m.Discipline.objects.all(), label="Дисциплина")
    teachers = forms.ModelMultipleChoiceField(
        m.Teacher.objects.filter(active=True),
        label="Преподаватели (по одной подгруппе на каждого)",
        widget=forms.SelectMultiple(attrs={"size": 12}),
    )
    pairs_per_cycle = forms.IntegerField(min_value=1, max_value=28, initial=4, label="Пар за 2 недели")
    room_type = forms.ModelChoiceField(m.RoomType.objects.all(), label="Тип аудитории")
    replace_group_items = forms.BooleanField(
        required=False,
        initial=True,
        label="Удалить отдельные элементы этой дисциплины у групп курса",
        help_text="Например, если английский был загружен из xlsx построчно по группам",
    )


@admin.register(m.Course)
class CourseAdmin(PrefGridMixin, admin.ModelAdmin):
    pref_fields = ("prefs",)
    list_display = ["__str__", "level", "max_per_day", "min_per_day", "max_consecutive", "block_link"]
    list_filter = ["semester", "level"]

    def semester_of(self, obj: m.Course) -> m.Semester | None:
        return obj.semester

    @admin.display(description="языковой блок")
    def block_link(self, obj: m.Course) -> str:
        return format_html(
            '<a href="{}">Создать блок курса</a>', reverse("admin:timetable_course_language_block", args=[obj.pk])
        )

    def get_urls(self) -> list[Any]:
        return [
            path(
                "<int:pk>/language-block/",
                self.admin_site.admin_view(self.language_block),
                name="timetable_course_language_block",
            ),
            *super().get_urls(),
        ]

    def language_block(self, request: HttpRequest, pk: int) -> HttpResponse:
        from django.core.exceptions import PermissionDenied

        if not request.user.has_perm("timetable.add_syncblock"):
            raise PermissionDenied
        course = get_object_or_404(m.Course.objects.select_related("semester"), pk=pk)
        groups = list(course.groups.all())
        initial: dict[str, Any] = {}
        eng = m.Discipline.objects.filter(name__icontains="англ").first()
        if eng:
            initial["discipline"] = eng.pk
        regular = m.RoomType.objects.annotate(n=Count("rooms")).order_by("-n").first()
        if regular:
            initial["room_type"] = regular.pk
        form = LanguageBlockForm(request.POST or None, initial=initial)
        if request.method == "POST" and form.is_valid():
            d = form.cleaned_data
            teachers = list(d["teachers"])
            total = sum(g.size for g in groups)
            sizes = [total // len(teachers) + (1 if i < total % len(teachers) else 0) for i in range(len(teachers))]
            name = f"{d['discipline']}, {course.number} курс"
            with transaction.atomic():
                removed = 0
                if d["replace_group_items"]:
                    old = m.WorkloadItem.objects.filter(
                        semester=course.semester, discipline=d["discipline"], groups__in=groups, sync_block=None
                    ).distinct()
                    removed = old.count()
                    old.delete()
                block, _ = m.SyncBlock.objects.get_or_create(semester=course.semester, name=name)
                block.groups.set(groups)
                block.items.all().delete()
                for t, sz in zip(teachers, sizes, strict=True):
                    it = m.WorkloadItem.objects.create(
                        semester=course.semester,
                        discipline=d["discipline"],
                        pairs_per_cycle=d["pairs_per_cycle"],
                        room_type=d["room_type"],
                        headcount=sz,
                        sync_block=block,
                        label=t.name.split()[0][:30],
                    )
                    it.teachers.set([t])
            messages.success(
                request,
                f"Блок «{name}»: {len(groups)} групп, {len(teachers)} подгрупп по ~{sizes[0]} человек"
                + (f"; удалено отдельных элементов: {removed}" if removed else "")
                + ". Уточните численность и уровни подгрупп в карточке блока.",
            )
            return HttpResponseRedirect(reverse("admin:timetable_syncblock_change", args=[block.pk]))
        return render(
            request,
            "admin/timetable/language_block.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "form": form,
                "course": course,
                "groups": groups,
                "students": sum(g.size for g in groups),
                "title": f"Языковой блок: {course.number} курс, {course.semester}",
            },
        )


class SubgroupInline(admin.TabularInline):
    model = m.Subgroup
    extra = 0


@admin.register(m.Group)
class GroupAdmin(PrefGridMixin, admin.ModelAdmin):
    pref_fields = ("prefs_override",)
    list_display = ["name", "course", "size", "subgroups_list"]
    list_filter = ["course__semester", "course__number"]
    search_fields = ["name"]
    inlines = [SubgroupInline]
    list_select_related = ["course__semester"]

    def semester_of(self, obj: m.Group) -> m.Semester | None:
        return obj.course.semester

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.Group]:
        return super().get_queryset(request).prefetch_related("subgroups")

    @admin.display(description="подгруппы")
    def subgroups_list(self, obj: m.Group) -> str:
        return ", ".join(f"{s.name} ({s.size})" for s in obj.subgroups.all()) or "—"


# ====================================================================== справочники организации


class AvailabilityInline(admin.TabularInline):
    model = m.TeacherAvailability
    extra = 0
    fields = ["semester", "submitted_at", "filled"]
    readonly_fields = ["filled"]
    show_change_link = True

    @admin.display(description="заполнено слотов")
    def filled(self, obj: m.TeacherAvailability) -> int:
        return len(obj.prefs or {})


@admin.register(m.Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ["name", "email", "can_login", "active", "max_per_day", "equipment_list", "rooms_list", "load"]
    list_filter = ["active", "required_equipment"]
    actions = ["allow_login"]
    search_fields = ["name", "email"]
    inlines = [AvailabilityInline]
    autocomplete_fields = ["user"]
    filter_horizontal = ["required_equipment", "preferred_rooms"]
    fieldsets = (
        (None, {"fields": ("name", "email", "active", "user")}),
        ("Лимиты", {"fields": ("max_per_day", "max_per_day_hard")}),
        (
            "Аудитории",
            {
                "fields": ("required_equipment", "preferred_rooms"),
                "description": "Необходимое оборудование — жёстко (без него вести нельзя); "
                "предпочтительные аудитории — по возможности (мягко).",
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.Teacher]:
        return super().get_queryset(request).prefetch_related("items", "required_equipment", "preferred_rooms")

    @admin.display(description="нужно оборудование")
    def equipment_list(self, obj: m.Teacher) -> str:
        return ", ".join(e.name for e in obj.required_equipment.all()) or "—"

    @admin.display(description="предпочитает аудитории")
    def rooms_list(self, obj: m.Teacher) -> str:
        return ", ".join(r.name for r in obj.preferred_rooms.all()) or "—"

    @admin.display(description="пар за цикл (все семестры)")
    def load(self, obj: m.Teacher) -> int:
        return sum(i.pairs_per_cycle for i in obj.items.all())

    @admin.display(description="вход", boolean=True)
    def can_login(self, obj: m.Teacher) -> bool | None:
        if not obj.email:
            return None
        return obj.email in self._allowed

    def changelist_view(self, request: HttpRequest, extra_context: Any = None) -> HttpResponse:
        from accounts.models import AllowedEmail

        self._allowed = set(AllowedEmail.objects.values_list("email", flat=True))
        return super().changelist_view(request, extra_context)

    @admin.action(description="Разрешить вход по почте (добавить в белый список)")
    def allow_login(self, request: HttpRequest, queryset: QuerySet[m.Teacher]) -> None:
        from accounts.models import AllowedEmail

        added = 0
        for t in queryset.exclude(email=None):
            _, created = AllowedEmail.objects.get_or_create(
                email=t.email, defaults={"role": AllowedEmail.Role.TEACHER, "note": t.name}
            )
            added += created
        no_mail = queryset.filter(email=None).count()
        messages.success(request, f"В белый список добавлено: {added}. Остальные уже были в нём.")
        if no_mail:
            messages.warning(request, f"Без почты, пропущены: {no_mail} — впишите почту в карточку преподавателя.")


@admin.register(m.TeacherAvailability)
class TeacherAvailabilityAdmin(PrefGridMixin, admin.ModelAdmin):
    pref_fields = ("prefs",)
    list_display = ["teacher", "semester", "submitted_at", "forbidden", "filled"]
    list_filter = ["semester", ("submitted_at", admin.EmptyFieldListFilter)]
    search_fields = ["teacher__name", "teacher__email"]
    autocomplete_fields = ["teacher"]
    list_select_related = ["teacher", "semester"]

    def semester_of(self, obj: m.TeacherAvailability) -> m.Semester | None:
        return obj.semester

    @admin.display(description="«не могу»")
    def forbidden(self, obj: m.TeacherAvailability) -> int:
        return sum(1 for v in (obj.prefs or {}).values() if v == -2)

    @admin.display(description="отмечено слотов")
    def filled(self, obj: m.TeacherAvailability) -> int:
        return len(obj.prefs or {})


@admin.register(m.Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ["name", "n_disciplines"]
    search_fields = ["name"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.Department]:
        return super().get_queryset(request).annotate(_n=Count("disciplines"))

    @admin.display(description="дисциплин", ordering="_n")
    def n_disciplines(self, obj: m.Department) -> int:
        return obj._n  # type: ignore[attr-defined]


@admin.register(m.Discipline)
class DisciplineAdmin(admin.ModelAdmin):
    list_display = ["name", "department", "difficulty"]
    list_filter = ["department"]
    search_fields = ["name"]
    list_editable = ["department", "difficulty"]


@admin.register(m.RoomType)
class RoomTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "n_rooms"]
    search_fields = ["name", "code"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.RoomType]:
        return super().get_queryset(request).annotate(_n=Count("rooms"))

    @admin.display(description="аудиторий", ordering="_n")
    def n_rooms(self, obj: m.RoomType) -> int:
        return obj._n  # type: ignore[attr-defined]


@admin.register(m.Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "n_rooms"]
    search_fields = ["name", "code"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.Equipment]:
        return super().get_queryset(request).annotate(_n=Count("rooms"))

    @admin.display(description="аудиторий", ordering="_n")
    def n_rooms(self, obj: m.Equipment) -> int:
        return obj._n  # type: ignore[attr-defined]


@admin.register(m.Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ["name", "closes_at", "n_rooms", "note"]
    list_editable = ["closes_at"]
    search_fields = ["name"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.Site]:
        return super().get_queryset(request).annotate(_n=Count("rooms"))

    @admin.display(description="аудиторий", ordering="_n")
    def n_rooms(self, obj: m.Site) -> int:
        return obj._n  # type: ignore[attr-defined]


@admin.register(m.Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ["name", "site", "building", "capacity", "room_type", "equipment_list", "active", "note"]
    list_filter = ["site", "room_type", "building", "active", "equipment"]
    search_fields = ["name", "building", "note"]
    list_editable = ["site", "capacity", "active"]
    filter_horizontal = ["equipment"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.Room]:
        return super().get_queryset(request).select_related("room_type", "site").prefetch_related("equipment")

    @admin.display(description="оборудование")
    def equipment_list(self, obj: m.Room) -> str:
        return ", ".join(e.name for e in obj.equipment.all()) or "—"


@admin.register(m.RoomAvailability)
class RoomAvailabilityAdmin(admin.ModelAdmin):
    list_display = ["room", "semester", "n_blocked", "reason"]
    list_filter = ["semester", "room"]
    autocomplete_fields = ["room"]

    @admin.display(description="занято пар")
    def n_blocked(self, obj: m.RoomAvailability) -> int:
        return len(obj.blocked or [])

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        form = super().get_form(request, obj, change, **kwargs)
        days, pairs = grid_for(obj.semester if obj else None)
        form.base_fields["blocked"] = BusyGridField(
            required=False, label="Занятые слоты", widget=BusyGridWidget(days, pairs), initial=list
        )
        return form


# ====================================================================== нагрузка


class SemesterScopedChoicesMixin:
    """В формах нагрузки предлагать только группы/подгруппы/блоки/объединения нужного семестра."""

    def _semester_for(self, request: HttpRequest) -> int | None:
        obj = getattr(request, "_timetable_obj", None)
        if obj is not None and getattr(obj, "semester_id", None):
            return obj.semester_id
        sid = request.GET.get("semester") or request.GET.get("semester__id__exact")
        return int(sid) if sid and sid.isdigit() else None

    def formfield_for_manytomany(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        sid = self._semester_for(request)
        if sid and db_field.name == "groups":
            kwargs["queryset"] = m.Group.objects.filter(course__semester_id=sid)
        if sid and db_field.name == "subgroups":
            kwargs["queryset"] = m.Subgroup.objects.filter(group__course__semester_id=sid).select_related("group")
        return super().formfield_for_manytomany(db_field, request, **kwargs)  # type: ignore[misc]

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        sid = self._semester_for(request)
        if sid and db_field.name == "sync_block":
            kwargs["queryset"] = m.SyncBlock.objects.filter(semester_id=sid)
        if sid and db_field.name == "merge_group":
            kwargs["queryset"] = m.MergeGroup.objects.filter(semester_id=sid)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)  # type: ignore[misc]


def _semester_param(request: HttpRequest) -> int | None:
    sid = request.GET.get("semester__id__exact")
    return int(sid) if sid and sid.isdigit() else None


def stream_key_db(it: m.WorkloadItem) -> tuple[Any, ...] | None:
    """То же, что ``Problem.stream_key``, но по строкам БД (для фильтра и действия в админке)."""
    groups = list(it.groups.all())
    if it.sync_block_id or it.merge_group_id or len(groups) != 1 or list(it.subgroups.all()):
        return None
    return (
        it.semester_id,
        it.discipline_id,
        groups[0].course_id,
        tuple(sorted(t.pk for t in it.teachers.all())),
        it.pairs_per_cycle,
        it.week_pattern,
        it.format,
        it.teacher_in_room,
        it.kind,
        it.room_type_id,
        tuple(sorted(e.pk for e in it.required_equipment.all())),
    )


def stream_clusters(qs: QuerySet[m.WorkloadItem]) -> list[list[m.WorkloadItem]]:
    by_key: dict[tuple[Any, ...], list[m.WorkloadItem]] = {}
    for it in qs.prefetch_related("groups", "subgroups", "teachers", "required_equipment"):
        key = stream_key_db(it)
        if key is not None:
            by_key.setdefault(key, []).append(it)
    return [its for its in by_key.values() if len({it.groups.all()[0].pk for it in its}) >= 2]


class AltCandidateFilter(admin.SimpleListFilter):
    """Кафедра кино: единственная дисциплина преподавателя, 1 пара в неделю → «через неделю»."""

    title = "подсказки"
    parameter_name = "hint"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [
            ("alt", "кандидаты «через неделю»"),
            ("booking", "кандидаты на бронь аудитории"),
            ("stream", "кандидаты на поток"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> QuerySet[m.WorkloadItem]:
        if self.value() == "stream":
            return queryset.filter(pk__in=[it.pk for cl in stream_clusters(queryset) for it in cl])
        if self.value() not in ("alt", "booking"):
            return queryset
        base = m.WorkloadItem.objects.prefetch_related("teachers")
        sid = _semester_param(request)
        if sid:
            base = base.filter(semester_id=sid)
        by_teacher: dict[tuple[int, int], list[m.WorkloadItem]] = {}
        items = list(base)
        for it in items:
            for t in it.teachers.all():
                by_teacher.setdefault((it.semester_id, t.pk), []).append(it)
        picked: set[int] = set()
        for its in by_teacher.values():
            if self.value() == "alt":
                if len({i.discipline_id for i in its}) == 1 and all(
                    i.pairs_per_cycle == 2 and i.week_pattern == "auto" and not i.sync_block_id for i in its
                ):
                    picked |= {i.pk for i in its}
            elif any(i.format == "in_person" for i in its):
                picked |= {i.pk for i in its if i.format == "online" and not i.teacher_in_room}
        return queryset.filter(pk__in=picked)


class PinInline(admin.TabularInline):
    model = m.Pin
    extra = 0
    autocomplete_fields = ["room"]


class MassCreateForm(forms.Form):
    semester = forms.ModelChoiceField(m.Semester.objects.all(), label="Семестр")
    discipline = forms.ModelChoiceField(m.Discipline.objects.all(), label="Дисциплина")
    kind = forms.ChoiceField(choices=m.WorkloadItem.Kind.choices, label="Вид", initial="practice")
    teachers = forms.ModelMultipleChoiceField(
        m.Teacher.objects.filter(active=True), label="Преподаватели", widget=forms.SelectMultiple(attrs={"size": 8})
    )
    groups = forms.ModelMultipleChoiceField(
        m.Group.objects.select_related("course__semester"),
        label="Группы",
        help_text="По одному элементу на каждую группу",
        widget=forms.SelectMultiple(attrs={"size": 12}),
    )
    pairs_per_cycle = forms.IntegerField(min_value=1, max_value=28, label="Пар за 2 недели", initial=2)
    format = forms.ChoiceField(choices=m.WorkloadItem.Format.choices, label="Формат", initial="in_person")
    room_type = forms.ModelChoiceField(m.RoomType.objects.all(), label="Тип аудитории", required=False)
    week_pattern = forms.ChoiceField(
        choices=m.WorkloadItem.WeekPattern.choices, label="Схема недель", initial="auto", required=False
    )
    teacher_in_room = forms.BooleanField(
        label="Онлайн, но преподавателю бронируется аудитория (дистант с площадки)", required=False
    )
    as_stream = forms.BooleanField(label="Одним потоком (один элемент на все группы)", required=False)
    as_merge = forms.BooleanField(label="Разрешить решателю объединять (допустимое объединение)", required=False)

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("format") == "in_person" and not data.get("room_type"):
            self.add_error("room_type", "Очному занятию нужен тип аудитории")
        if data.get("teacher_in_room") and data.get("format") != "online":
            self.add_error("teacher_in_room", "Бронь аудитории преподавателю — только для онлайн-занятий")
        if data.get("teacher_in_room") and not data.get("room_type"):
            self.add_error("room_type", "Для брони укажите тип аудитории")
        if data.get("week_pattern") == "alt_week" and (data.get("pairs_per_cycle") or 0) % 2:
            self.add_error("pairs_per_cycle", "«Через неделю» — чётное число пар")
        sem = data.get("semester")
        bad = [g.name for g in data.get("groups", []) if sem and g.course.semester_id != sem.pk]
        if bad:
            self.add_error("groups", f"Группы не из выбранного семестра: {', '.join(bad)}")
        if data.get("as_stream") and data.get("as_merge"):
            self.add_error("as_merge", "Поток и объединение — взаимоисключающие варианты")
        return data


@admin.register(m.WorkloadItem)
class WorkloadItemAdmin(SemesterScopedChoicesMixin, admin.ModelAdmin):
    list_display = [
        "discipline",
        "kind",
        "participants",
        "teachers_list",
        "pairs_per_cycle",
        "format",
        "room_type",
        "week_pattern_short",
        "teacher_in_room",
        "may_end_early",
        "planning",
        "sync_block",
        "merge_group",
        "label",
    ]
    list_display_links = ["discipline"]
    list_editable = ["pairs_per_cycle"]
    list_filter = [
        "semester",
        AltCandidateFilter,
        "discipline__department",
        "week_pattern",
        "teacher_in_room",
        "may_end_early",
        "planning",
        "kind",
        "format",
        "room_type",
        "groups__course__number",
        "double",
        ("sync_block", admin.EmptyFieldListFilter),
        ("merge_group", admin.EmptyFieldListFilter),
    ]
    search_fields = ["discipline__name", "teachers__name", "groups__name", "subgroups__group__name", "label", "comment"]
    filter_horizontal = ["teachers", "groups", "subgroups", "alt_room_types", "required_equipment"]
    autocomplete_fields = ["discipline"]
    list_per_page = 100
    actions = [
        "duplicate",
        "set_alt_week",
        "set_every_week",
        "book_room",
        "unbook_room",
        "set_manual",
        "set_solver",
        "set_end_early",
        "unset_end_early",
        "allow_stream",
        "make_stream",
        "export_selected",
    ]
    inlines = [PinInline]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "semester",
                    "discipline",
                    "kind",
                    "teachers",
                    "pairs_per_cycle",
                    "week_pattern",
                    "format",
                    "teacher_in_room",
                    "room_type",
                    "required_equipment",
                    "planning",
                    "may_end_early",
                )
            },
        ),
        ("Участники", {"fields": ("groups", "subgroups", "headcount")}),
        ("Связи", {"fields": ("sync_block", "merge_group")}),
        (
            "Дополнительно",
            {
                "classes": ("collapse",),
                "fields": ("double", "alt_room_types", "priority", "label", "comment"),
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.WorkloadItem]:
        return (
            super()
            .get_queryset(request)
            .select_related("discipline", "room_type", "sync_block", "merge_group", "semester")
            .prefetch_related("teachers", "groups", "subgroups__group")
            .distinct()
        )

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        request._timetable_obj = obj  # type: ignore[attr-defined]
        return super().get_form(request, obj, change, **kwargs)

    @admin.display(description="недели", ordering="week_pattern")
    def week_pattern_short(self, obj: m.WorkloadItem) -> str:
        return "через неделю" if obj.week_pattern == "alt_week" else "обычная"

    @admin.display(description="участники")
    def participants(self, obj: m.WorkloadItem) -> str:
        return obj.participants_display()

    @admin.display(description="преподаватели")
    def teachers_list(self, obj: m.WorkloadItem) -> str:
        return ", ".join(t.name for t in obj.teachers.all())

    def save_related(self, request: HttpRequest, form: Any, formsets: Any, change: bool) -> None:
        super().save_related(request, form, formsets, change)
        for problem in form.instance.clean_relations():
            messages.warning(request, f"{form.instance}: {problem}")

    @admin.action(description="Копировать выбранные элементы")
    def duplicate(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        n = 0
        with transaction.atomic():
            for it in queryset:
                teachers, groups, subgroups = list(it.teachers.all()), list(it.groups.all()), list(it.subgroups.all())
                alts = list(it.alt_room_types.all())
                it.pk = None
                it.id = None
                it.comment = (it.comment + " (копия)").strip()[:300]
                it.save()
                it.teachers.set(teachers)
                it.groups.set(groups)
                it.subgroups.set(subgroups)
                it.alt_room_types.set(alts)
                n += 1
        messages.success(request, f"Скопировано элементов: {n}. Копии помечены «(копия)» в комментарии.")

    @admin.action(description="Схема «через неделю» (2 пары подряд раз в две недели)")
    def set_alt_week(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        odd = [str(it) for it in queryset if it.pairs_per_cycle % 2]
        blocked = queryset.exclude(sync_block=None).count()
        n = queryset.filter(sync_block=None).exclude(pk__in=[it.pk for it in queryset if it.pairs_per_cycle % 2])
        updated = n.update(week_pattern="alt_week")
        messages.success(request, f"«Через неделю»: {updated} элементов")
        if odd:
            messages.warning(request, f"Пропущены (нечётное число пар): {'; '.join(odd[:5])}")
        if blocked:
            messages.warning(request, f"Пропущены элементы синхронных блоков: {blocked}")

    @admin.action(description="Схема «обычная» (каждую неделю)")
    def set_every_week(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        messages.success(request, f"Обычная схема: {queryset.update(week_pattern='auto')} элементов")

    @admin.action(description="Можно закончить раньше, к закрытию площадки")
    def set_end_early(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        n = queryset.update(may_end_early=True)
        messages.success(request, f"«Можно закончить раньше»: {n} элементов — им разрешена последняя пара площадки")

    @admin.action(description="Снять «можно закончить раньше»")
    def unset_end_early(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        n = queryset.update(may_end_early=False)
        messages.success(request, f"Снято у {n} элементов")

    @admin.action(description="Онлайн: забронировать аудиторию преподавателю")
    def book_room(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        online = queryset.filter(format="online")
        default_type = m.RoomType.objects.annotate(n=Count("rooms")).order_by("-n").first()
        no_type = online.filter(room_type=None)
        if no_type.exists() and default_type:
            no_type.update(room_type=default_type)
            messages.info(request, f"Где тип аудитории не был указан, поставлен «{default_type}»")
        n = online.update(teacher_in_room=True)
        messages.success(request, f"Бронь аудитории преподавателю: {n} онлайн-элементов")
        skipped = queryset.exclude(format="online").count()
        if skipped:
            messages.warning(request, f"Пропущены очные элементы: {skipped}")

    @admin.action(description="Онлайн: снять бронь аудитории")
    def unbook_room(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        messages.success(request, f"Бронь снята: {queryset.update(teacher_in_room=False)} элементов")

    @admin.action(description="Ставить вручную (элективы, кружки)")
    def set_manual(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        messages.success(
            request,
            f"Вручную: {queryset.update(planning='manual')} элементов. Решатель учтёт их, "
            f"когда все пары будут закреплены (вкладка «Закрепления» в карточке)",
        )

    @admin.action(description="Ставить решателем")
    def set_solver(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        messages.success(request, f"Решатель: {queryset.update(planning='auto')} элементов")

    @admin.action(description="Поток: разрешить решателю ставить вместе (допустимое объединение)")
    def allow_stream(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        clusters = stream_clusters(queryset)
        with transaction.atomic():
            for cl in clusters:
                names = " + ".join(sorted(it.groups.all()[0].name for it in cl))
                mg = m.MergeGroup.objects.create(
                    semester_id=cl[0].semester_id, name=f"Поток: {cl[0].discipline} — {names}"[:150]
                )
                m.WorkloadItem.objects.filter(pk__in=[it.pk for it in cl]).update(merge_group=mg)
        n = sum(len(cl) for cl in clusters)
        messages.success(
            request,
            f"Разрешено потоков: {len(clusters)} (элементов: {n}). Решатель объединит "
            f"группы, если это выгодно; в табель часы идут по каждой группе.",
        )
        skipped = queryset.count() - n
        if skipped:
            messages.warning(
                request,
                f"Не вошли ни в один поток: {skipped} (нужны одна дисциплина, курс, "
                f"преподаватель, часы и схема, разные группы)",
            )

    @admin.action(description="Поток: всегда вместе (один элемент на все группы)")
    def make_stream(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> None:
        clusters = stream_clusters(queryset)
        with transaction.atomic():
            for cl in clusters:
                keep, rest = cl[0], cl[1:]
                keep.groups.set([it.groups.all()[0] for it in cl])
                keep.comment = (keep.comment + " (поток)").strip()[:300]
                keep.save(update_fields=["comment"])
                m.WorkloadItem.objects.filter(pk__in=[it.pk for it in rest]).delete()
        messages.success(request, f"Сделано потоков: {len(clusters)}. В табель часы идут по каждой группе.")

    @admin.action(description="Выгрузить выбранное в xlsx (весь семестр)")
    def export_selected(self, request: HttpRequest, queryset: QuerySet[m.WorkloadItem]) -> HttpResponse | None:
        sems = set(queryset.values_list("semester_id", flat=True))
        if len(sems) != 1:
            messages.error(request, "Выберите элементы одного семестра")
            return None
        sem = m.Semester.objects.get(pk=sems.pop())
        return _xlsx_response(export_workload(sem), f"workload_{sem.pk}.xlsx")

    def get_urls(self) -> list[Any]:
        return [
            path("mass-create/", self.admin_site.admin_view(self.mass_create), name="timetable_workload_mass"),
            *super().get_urls(),
        ]

    def changelist_view(self, request: HttpRequest, extra_context: dict[str, Any] | None = None) -> Any:
        sid = request.GET.get("semester__id__exact")
        extra = {"mass_url": reverse("admin:timetable_workload_mass")}
        if sid and sid.isdigit():
            extra["semester_tools"] = [
                ("Импорт xlsx", reverse("admin:timetable_semester_workload_import", args=[sid])),
                ("Экспорт xlsx", reverse("admin:timetable_semester_workload_export", args=[sid])),
                ("Проверить задачу", reverse("admin:timetable_semester_precheck", args=[sid])),
            ]
        return super().changelist_view(request, {**(extra_context or {}), **extra})

    def mass_create(self, request: HttpRequest) -> HttpResponse:
        if not request.user.has_perm("timetable.add_workloaditem"):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied
        initial = {}
        if request.GET.get("semester", "").isdigit():
            initial["semester"] = int(request.GET["semester"])
        form = MassCreateForm(request.POST or None, initial=initial)
        if request.method == "POST" and form.is_valid():
            d = form.cleaned_data
            created = []
            with transaction.atomic():
                merge = None
                if d["as_merge"]:
                    merge = m.MergeGroup.objects.create(
                        semester=d["semester"],
                        name=f"{d['discipline']} — {', '.join(g.name for g in d['groups'])}"[:150],
                    )
                batches = [list(d["groups"])] if d["as_stream"] else [[g] for g in d["groups"]]
                for gs in batches:
                    it = m.WorkloadItem.objects.create(
                        semester=d["semester"],
                        discipline=d["discipline"],
                        kind=d["kind"],
                        pairs_per_cycle=d["pairs_per_cycle"],
                        format=d["format"],
                        room_type=d["room_type"] if (d["format"] == "in_person" or d["teacher_in_room"]) else None,
                        week_pattern=d["week_pattern"] or "auto",
                        teacher_in_room=d["teacher_in_room"],
                        merge_group=merge,
                    )
                    it.teachers.set(d["teachers"])
                    it.groups.set(gs)
                    created.append(it)
            messages.success(request, f"Создано элементов нагрузки: {len(created)}")
            return HttpResponseRedirect(
                reverse("admin:timetable_workloaditem_changelist") + f"?semester__id__exact={d['semester'].pk}"
            )
        return render(
            request,
            "admin/timetable/mass_create.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "form": form,
                "title": "Массовое создание нагрузки",
            },
        )


class BlockItemInline(admin.TabularInline):
    model = m.WorkloadItem
    fk_name = "sync_block"
    fields = ["discipline", "label", "headcount", "pairs_per_cycle", "room_type", "teachers_list"]
    readonly_fields = ["teachers_list"]
    extra = 0
    show_change_link = True
    verbose_name = "занятие блока"
    verbose_name_plural = "занятия блока (языковые подгруппы)"

    @admin.display(description="преподаватели")
    def teachers_list(self, obj: m.WorkloadItem) -> str:
        return ", ".join(t.name for t in obj.teachers.all()) if obj.pk else "— сохраните, затем откройте элемент"


@admin.register(m.SyncBlock)
class SyncBlockAdmin(admin.ModelAdmin):
    list_display = ["name", "semester", "groups_list", "n_items", "pairs"]
    list_filter = ["semester"]
    search_fields = ["name"]
    filter_horizontal = ["groups"]
    inlines = [BlockItemInline]

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.SyncBlock]:
        return super().get_queryset(request).prefetch_related("groups", "items")

    @admin.display(description="группы")
    def groups_list(self, obj: m.SyncBlock) -> str:
        return ", ".join(g.name for g in obj.groups.all())

    @admin.display(description="занятий")
    def n_items(self, obj: m.SyncBlock) -> int:
        return len(obj.items.all())

    @admin.display(description="пар за цикл")
    def pairs(self, obj: m.SyncBlock) -> str:
        values = sorted({i.pairs_per_cycle for i in obj.items.all()})
        if len(values) > 1:
            return format_html('<b style="color:#c00">разное: {}</b>', ", ".join(map(str, values)))
        return str(values[0]) if values else "—"

    def formfield_for_manytomany(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        obj_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
        if db_field.name == "groups" and obj_id:
            sem_id = m.SyncBlock.objects.filter(pk=obj_id).values_list("semester_id", flat=True).first()
            kwargs["queryset"] = m.Group.objects.filter(course__semester_id=sem_id)
        return super().formfield_for_manytomany(db_field, request, **kwargs)


class MergeItemInline(admin.TabularInline):
    model = m.WorkloadItem
    fk_name = "merge_group"
    fields = ["discipline", "pairs_per_cycle", "format", "room_type"]
    readonly_fields = ["discipline", "pairs_per_cycle", "format", "room_type"]
    extra = 0
    can_delete = False
    show_change_link = True
    max_num = 0
    verbose_name_plural = "элементы (назначаются в карточке элемента)"


@admin.register(m.MergeGroup)
class MergeGroupAdmin(admin.ModelAdmin):
    list_display = ["name", "semester", "n_items"]
    list_filter = ["semester"]
    search_fields = ["name"]
    inlines = [MergeItemInline]

    def get_queryset(self, request: HttpRequest) -> QuerySet[m.MergeGroup]:
        return super().get_queryset(request).annotate(_n=Count("items"))

    @admin.display(description="элементов", ordering="_n")
    def n_items(self, obj: m.MergeGroup) -> int:
        return obj._n  # type: ignore[attr-defined]


@admin.register(m.Pin)
class PinAdmin(admin.ModelAdmin):
    list_display = ["item", "week", "day", "pair", "room"]
    list_filter = ["item__semester", "week", "day"]
    autocomplete_fields = ["room"]
    raw_id_fields = ["item"]


# ====================================================================== пресеты весов


def _preset_extra_fields() -> dict[str, forms.Field]:
    fields: dict[str, forms.Field] = {
        f"w_{c.value}": forms.IntegerField(
            label=LABELS[c], min_value=0, max_value=100_000, initial=DEFAULT_WEIGHTS.get(c, 0)
        )
        for c in Crit
    }
    for v, label in ((-1, "«−1 нежелательно»"), (1, "«+1 удобно»"), (2, "«+2 очень хочу»")):
        fields[f"c_{v}"] = forms.IntegerField(
            label=f"Цена {label}",
            initial=DEFAULT_PREF_COST[v],
            help_text="Положительная — штраф, отрицательная — бонус",
        )
    return fields


class _WeightPresetBase(forms.ModelForm):
    """Веса редактируются числовыми полями с подписями критериев, а не сырым JSON."""

    class Meta:
        model = m.WeightPreset
        fields = ["name", "mode", "is_default", "note", "lex_levels"]

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            for k, v in (self.instance.weights or {}).items():
                if f"w_{k}" in self.fields:
                    self.fields[f"w_{k}"].initial = v
            for k, v in (self.instance.pref_cost or {}).items():
                if f"c_{k}" in self.fields:
                    self.fields[f"c_{k}"].initial = v
        self.fields["lex_levels"].required = False
        self.fields["lex_levels"].help_text = (
            "Только для лексикографического режима: список уровней, каждый — список кодов критериев. "
            "Пусто — порядок по умолчанию. Коды: " + ", ".join(c.value for c in Crit)
        )

    def save(self, commit: bool = True) -> m.WeightPreset:
        obj = super().save(commit=False)
        obj.weights = {c.value: self.cleaned_data[f"w_{c.value}"] for c in Crit}
        obj.pref_cost = {str(v): self.cleaned_data[f"c_{v}"] for v in (-1, 1, 2)}
        obj.lex_levels = self.cleaned_data.get("lex_levels") or []
        if commit:
            obj.save()
        return obj

    def clean_lex_levels(self) -> Any:
        levels = self.cleaned_data.get("lex_levels") or []
        if not isinstance(levels, list) or not all(isinstance(lv, list) for lv in levels):
            raise forms.ValidationError('Ожидается список списков, например [["fmt_in_to_online"], ["teacher_pref"]]')
        bad = [c for lv in levels for c in lv if c not in Crit._value2member_map_]
        if bad:
            raise forms.ValidationError(f"Неизвестные критерии: {', '.join(map(str, bad))}")
        return levels


# поля объявлены на классе (а не в __init__), чтобы админка знала о них при сборке формы
WeightPresetForm = type("WeightPresetForm", (_WeightPresetBase,), _preset_extra_fields())


@admin.register(m.WeightPreset)
class WeightPresetAdmin(admin.ModelAdmin):
    form = WeightPresetForm
    list_display = ["name", "mode", "is_default", "note"]

    def get_fieldsets(self, request: HttpRequest, obj: Any = None) -> Any:
        return (
            (None, {"fields": ("name", "mode", "is_default", "note")}),
            (
                "Веса критериев",
                {
                    "fields": tuple(f"w_{c.value}" for c in Crit),
                    "description": "Рекомендуемый порядок: очно→онлайн ≫ «−1» у преподавателя ≈ "
                    "онлайн→очно > окна > прочее",
                },
            ),
            ("Шкала предпочтений", {"fields": ("c_-1", "c_1", "c_2")}),
            ("Лексикографический режим", {"classes": ("collapse",), "fields": ("lex_levels",)}),
        )

    def save_model(self, request: HttpRequest, obj: m.WeightPreset, form: Any, change: bool) -> None:
        with transaction.atomic():
            if obj.is_default:
                m.WeightPreset.objects.exclude(pk=obj.pk).update(is_default=False)
            super().save_model(request, obj, form, change)


# ====================================================================== варианты


class AssignmentInline(admin.TabularInline):
    model = m.ScheduleAssignment
    fields = ["day", "pair", "week", "item", "room", "teacher_remote"]
    readonly_fields = ["day", "pair", "week", "item", "room"]
    extra = 0
    can_delete = False
    max_num = 0
    show_change_link = False


@admin.register(m.ScheduleVariant)
class ScheduleVariantAdmin(admin.ModelAdmin):
    list_display = ["name", "semester", "status", "total", "solver_status", "wall_time_s", "created_at", "views"]
    list_filter = ["semester", "status"]
    readonly_fields = [
        "semester",
        "solver",
        "solver_status",
        "params",
        "total",
        "objective",
        "wall_time_s",
        "created_at",
        "created_by",
        "breakdown_table",
        "run",
    ]
    fields = [
        "name",
        "status",
        "note",
        "semester",
        "total",
        "breakdown_table",
        "solver",
        "solver_status",
        "objective",
        "wall_time_s",
        "params",
        "run",
        "created_by",
        "created_at",
    ]
    inlines = [AssignmentInline]
    actions = ["approve", "revalidate"]

    @admin.display(description="смотреть")
    def views(self, obj: m.ScheduleVariant) -> str:
        return format_html(
            '<a href="/app/?semester={}&amp;variant={}"><b>в интерфейсе</b></a> · '
            '<a href="{}">расписание</a> · <a href="{}">свободные аудитории</a> · <a href="{}">часы</a>'
            ' · <a href="{}">Excel ⇣</a>',
            obj.semester_id,
            obj.pk,
            reverse("admin:timetable_variant_grid", args=[obj.pk]),
            reverse("admin:timetable_variant_free_rooms") + f"?variant={obj.pk}",
            reverse("admin:timetable_variant_hours", args=[obj.pk]),
            reverse("admin:timetable_variant_export", args=[obj.pk]),
        )

    def get_urls(self) -> list[Any]:
        w = self.admin_site.admin_view
        return [
            path("<int:pk>/grid/", w(self.grid_view), name="timetable_variant_grid"),
            path("<int:pk>/hours/", w(self.hours_view), name="timetable_variant_hours"),
            path("<int:pk>/schedule.xlsx", w(self.export_view), name="timetable_variant_export"),
            path("<int:pk>/calendar.ics", w(self.ics_view), name="timetable_variant_ics"),
            path("free-rooms/", w(self.free_rooms_view), name="timetable_variant_free_rooms"),
            *super().get_urls(),
        ]

    def _ctx(self, request: HttpRequest, title: str, **kw: Any) -> dict[str, Any]:
        return {**self.admin_site.each_context(request), "opts": self.model._meta, "title": title, **kw}

    def ics_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Календарь .ics выбранной группы / преподавателя / аудитории из этого варианта."""
        from .services import views as sv
        from .services.calendar import variant_ics

        self._require_view(request)
        v = get_object_or_404(m.ScheduleVariant.objects.select_related("semester"), pk=pk)
        by = request.GET.get("by", "group")
        if by not in sv.BY_CHOICES:
            by = "group"
        key = request.GET.get("key", "")
        p, _ = load_variant(v)
        names = dict(sv.entities(p, by))
        if key not in names:
            raise Http404("Не выбрано, для кого календарь")
        text = variant_ics(v, by, key, f"Расписание: {names[key]}")
        resp = HttpResponse(text, content_type="text/calendar; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="raspisanie_{by}_{key}.ics"'
        return resp

    def export_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Готовое расписание в Excel: листы по группам, преподавателям и аудиториям."""
        from .export.schedule_xlsx import schedule_xlsx

        self._require_view(request)
        v = get_object_or_404(m.ScheduleVariant.objects.select_related("semester"), pk=pk)
        p, s = load_variant(v)
        status = "утверждён" if v.status == m.ScheduleVariant.Status.APPROVED else "черновик, не утверждён"
        data = schedule_xlsx(
            p,
            s,
            title=f"Расписание: {v.semester.name} — {v.name} ({status})",
            notes=[f"Выгружено {timezone.localtime():%d.%m.%Y %H:%M}. Штраф варианта: {v.total}."],
        )
        return _xlsx_response(data, f"raspisanie_{v.semester.pk}_v{v.pk}.xlsx")

    def _require_view(self, request: HttpRequest) -> None:
        from django.core.exceptions import PermissionDenied

        if not request.user.has_perm("timetable.view_schedulevariant"):
            raise PermissionDenied

    def grid_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        from scheduler.domain import Week as DWeek

        from .services import views as sv

        self._require_view(request)
        v = get_object_or_404(m.ScheduleVariant, pk=pk)
        problem, sched = load_variant(v)
        by = request.GET.get("by") if request.GET.get("by") in sv.BY_CHOICES else "group"
        options = sv.entities(problem, by)
        key = request.GET.get("key") or (options[0][0] if options else "")
        week = DWeek.EVEN if request.GET.get("week") == "even" else DWeek.ODD
        rows = sv.grid(problem, sched, by, key, week) if key else []
        current = dict(options).get(key, "")
        return render(
            request,
            "admin/timetable/schedule_grid.html",
            self._ctx(
                request,
                f"{v.name}: {sv.BY_CHOICES[by]} {current}",
                variant=v,
                by=by,
                by_choices=sv.BY_CHOICES,
                options=options,
                key=key,
                week=week.value,
                rows=rows,
                days=sv.day_headers(problem),
            ),
        )

    def hours_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        from scheduler.reports import teacher_hours

        self._require_view(request)
        v = get_object_or_404(m.ScheduleVariant, pk=pk)
        problem, sched = load_variant(v)
        rows = teacher_hours(problem, sched)
        if request.GET.get("format") == "xlsx":
            from openpyxl import Workbook

            from .xlsx.common import finish_sheet, write_header

            wb = Workbook()
            ws = wb.active
            ws.title = "Часы"
            write_header(
                ws,
                [
                    "Преподаватель",
                    "Дисциплина",
                    "Группы",
                    "Пар за 2 недели",
                    "Часов к табелю за 2 недели",
                    "Фактически пар за 2 недели (поток = 1)",
                ],
                [30, 34, 28, 12, 14, 16],
            )
            for r in rows:
                for disc, groups, pairs, hours in r.lines:
                    ws.append([r.teacher, disc, groups, pairs, hours, None])
                ws.append([r.teacher, "ИТОГО", "", None, r.hours_tabel, r.pairs_actual])
            finish_sheet(ws)
            import io

            buf = io.BytesIO()
            wb.save(buf)
            return _xlsx_response(buf.getvalue(), f"hours_variant_{v.pk}.xlsx")
        return render(
            request,
            "admin/timetable/teacher_hours.html",
            self._ctx(
                request,
                f"Часы преподавателей: {v.name}",
                variant=v,
                rows=rows,
                hours_per_pair=problem.policy.hours_per_pair,
            ),
        )

    def free_rooms_view(self, request: HttpRequest) -> HttpResponse:
        from .services import views as sv

        self._require_view(request)
        variants = m.ScheduleVariant.objects.select_related("semester").order_by("-status", "-created_at")
        vid = request.GET.get("variant")
        v = (
            variants.filter(pk=vid).first()
            if vid and vid.isdigit()
            else (variants.filter(status="approved").first() or variants.first())
        )
        if v is None:
            messages.info(request, "Вариантов расписания пока нет — свободные аудитории считать не по чему")
            return HttpResponseRedirect(reverse("admin:timetable_schedulevariant_changelist"))
        problem, sched = load_variant(v)
        days, pairs = problem.grid.days, [x.number for x in problem.grid.pairs]

        def num(name: str, default: int) -> int:
            raw = request.GET.get(name, "")
            return int(raw) if raw.isdigit() else default

        day = num("day", days[0])
        p_from, p_to = num("pair_from", pairs[0]), num("pair_to", pairs[-1])
        if p_from > p_to:
            p_from, p_to = p_to, p_from
        week = request.GET.get("week", "both")
        eq = set(request.GET.getlist("equipment"))
        results = sv.free_rooms(
            problem,
            sched,
            day,
            p_from,
            p_to,
            sv.WEEK_CHOICES.get(week, sv.WEEK_CHOICES["both"]),
            min_capacity=num("min_capacity", 0),
            room_type=request.GET.get("room_type") or None,
            equipment=eq or None,
            include_partial=request.GET.get("partial", "1") == "1",
        )
        return render(
            request,
            "admin/timetable/free_rooms.html",
            self._ctx(
                request,
                "Свободные аудитории",
                variant=v,
                variants=variants,
                results=results,
                days=[(d, DAY_NAMES[d]) for d in days],
                pairs=pairs,
                day=day,
                pair_from=p_from,
                pair_to=p_to,
                week=week,
                room_types=problem.room_types,
                room_type=request.GET.get("room_type", ""),
                equipment=m.Equipment.objects.all(),
                eq=eq,
                min_capacity=request.GET.get("min_capacity", ""),
                partial=request.GET.get("partial", "1"),
            ),
        )

    @admin.display(description="штраф по критериям")
    def breakdown_table(self, obj: m.ScheduleVariant) -> str:
        rows = [
            (LABELS.get(Crit(k), k) if k in Crit._value2member_map_ else k, v) for k, v in (obj.breakdown or {}).items()
        ]
        if not rows:
            return "—"
        return format_html("<table>{}</table>", format_html_join("", "<tr><td>{}</td><td>{}</td></tr>", rows))

    @admin.action(description="Утвердить (остальные варианты семестра — в черновики)")
    def approve(self, request: HttpRequest, queryset: QuerySet[m.ScheduleVariant]) -> None:
        if queryset.count() != 1:
            messages.error(request, "Утвердить можно ровно один вариант")
            return
        v = queryset.get()
        try:
            approve_variant(v)
        except ApproveError as e:
            messages.error(request, str(e))
            return
        messages.success(request, f"Утверждён «{v.name}»")

    @admin.action(description="Перепроверить независимым валидатором")
    def revalidate(self, request: HttpRequest, queryset: QuerySet[m.ScheduleVariant]) -> None:
        for v in queryset:
            problem, sched = load_variant(v)
            rep = validate(problem, sched)
            level = messages.SUCCESS if rep.ok else messages.ERROR
            messages.add_message(request, level, f"«{v.name}»: жёстких нарушений {len(rep.hard)}, штраф {rep.total}")


# ====================================================================== запуски расчёта (этап 5)


@admin.register(m.SolveRun)
class SolveRunAdmin(admin.ModelAdmin):
    """История и ход фоновых расчётов. Запуск — кнопкой «Построить расписание» у семестра."""

    list_display = ["number", "semester", "status_label", "created_at", "created_by", "short_progress", "result"]
    list_filter = ["status", "semester"]
    list_select_related = ["semester", "created_by"]

    @admin.display(description="№", ordering="pk")
    def number(self, obj: m.SolveRun) -> str:
        return f"№{obj.pk}"

    @admin.display(description="состояние", ordering="status")
    def status_label(self, obj: m.SolveRun) -> str:
        return obj.get_status_display() + (" (остановка запрошена)" if obj.is_active and obj.cancel_requested else "")

    @admin.display(description="ход")
    def short_progress(self, obj: m.SolveRun) -> str:
        p = obj.progress or {}
        if obj.status == m.SolveRun.Status.RUNNING and p:
            est = f", штраф ≈ {p['objective']:.0f}" if p.get("objective") is not None else ""
            return f"вариант {p.get('variant')} из {p.get('of')}{est}"
        if obj.elapsed_s is not None and not obj.is_active:
            return f"{obj.elapsed_s / 60:.1f} мин"
        return "—"

    @admin.display(description="варианты")
    def result(self, obj: m.SolveRun) -> str:
        return format_html_join(
            ", ",
            '<a href="{}">{}</a>',
            ((reverse("admin:timetable_variant_grid", args=[v.pk]), v.name) for v in obj.variants.all()),
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # запуск — из семестра: «Построить расписание»

    def has_delete_permission(self, request: HttpRequest, obj: m.SolveRun | None = None) -> bool:
        return super().has_delete_permission(request, obj) and (obj is None or not obj.is_active)

    def get_urls(self) -> list[Any]:
        w = self.admin_site.admin_view
        return [
            path("<int:pk>/cancel/", w(self.cancel_view), name="timetable_solverun_cancel"),
            *super().get_urls(),
        ]

    def change_view(
        self, request: HttpRequest, object_id: str, form_url: str = "", extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        """Страница хода расчёта (вместо формы редактирования): обновляется сама, пока идёт расчёт."""
        from django.core.exceptions import PermissionDenied

        run = get_object_or_404(
            m.SolveRun.objects.select_related("semester", "preset", "reference", "created_by"), pk=object_id
        )
        if not self.has_view_permission(request, run):
            raise PermissionDenied
        now = timezone.now()
        queue_pos = None
        if run.status == m.SolveRun.Status.QUEUED:
            queued = m.SolveRun.objects.filter(status=m.SolveRun.Status.QUEUED)
            queue_pos = queued.filter(created_at__lt=run.created_at).count()
        p = run.progress or {}
        budget = run.time_limit_s * run.variants_requested
        return render(
            request,
            "admin/timetable/solverun.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": str(run),
                "run": run,
                "p": p,
                "queue_pos": queue_pos,
                "queued_long": queue_pos is not None and (now - run.created_at).total_seconds() > 30,
                "silent_s": (now - run.heartbeat_at).total_seconds()
                if run.status == m.SolveRun.Status.RUNNING and run.heartbeat_at
                else None,
                "elapsed_min": (run.elapsed_s or 0) / 60,
                "budget_min": budget / 60,
                "percent": min(99, round(100 * (run.elapsed_s or 0) / budget)) if budget and run.is_active else None,
                "variants": run.variants.all(),
                "can_cancel": run.is_active and self.has_change_permission(request, run),
                "refresh": run.is_active,
            },
        )

    def cancel_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        from django.core.exceptions import PermissionDenied

        run = get_object_or_404(m.SolveRun, pk=pk)
        if request.method != "POST" or not self.has_change_permission(request, run):
            raise PermissionDenied
        run = request_cancel(run)
        if run.status == m.SolveRun.Status.CANCELLED:
            messages.success(request, "Расчёт снят с очереди.")
        elif run.is_active:
            messages.info(request, "Остановка запрошена: через несколько секунд расчёт сохранит найденное.")
        return HttpResponseRedirect(reverse("admin:timetable_solverun_change", args=[pk]))

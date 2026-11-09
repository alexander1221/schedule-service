from django.db import migrations, models


def keep_latest_approved(apps, schema_editor):
    Variant = apps.get_model("timetable", "ScheduleVariant")
    approved = Variant.objects.using(schema_editor.connection.alias).filter(status="approved")
    seen = set()
    for variant in approved.order_by("semester_id", "-created_at", "-pk").iterator():
        if variant.semester_id in seen:
            Variant.objects.using(schema_editor.connection.alias).filter(pk=variant.pk).update(status="draft")
        seen.add(variant.semester_id)


class Migration(migrations.Migration):
    dependencies = [("timetable", "0007_v080_solve_run")]

    operations = [
        migrations.RunPython(keep_latest_approved, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="schedulevariant",
            constraint=models.UniqueConstraint(
                fields=("semester",),
                condition=models.Q(status="approved"),
                name="uniq_approved_variant_per_semester",
            ),
        ),
    ]

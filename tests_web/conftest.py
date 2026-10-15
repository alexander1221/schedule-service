import pytest

from scheduler.generator import GenParams, generate
from timetable.services.problem import import_problem


@pytest.fixture
def synthetic(db):
    """Небольшой синтетический семестр в БД: (семестр, карта id, посаженное расписание, исходная задача)."""
    g = generate(GenParams(seed=3, n_groups=6, n_teachers=14, courses=2, n_blocks=1, n_merges=0, n_pins=1))
    sem, ids = import_problem(g.problem, "Тестовый семестр")
    return sem, ids, g.planted, g.problem


@pytest.fixture
def admin_client_ru(db, client):
    from accounts.models import User

    user = User.objects.create_superuser("admin@college.example", "pass-12345-word")
    client.force_login(user)
    return client

import pytest


def pytest_addoption(parser):
    parser.addoption("--slow", action="store_true", help="запускать медленные тесты (решатель на полной синтетике)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--slow"):
        return
    skip = pytest.mark.skip(reason="медленный тест: запустите с --slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: медленный тест (решатель на синтетике масштаба колледжа)")

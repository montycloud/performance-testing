from mcperf.locust.base_user import MontyCloudUser
from mcperf.locust.scenario import _as_classes


class _A(MontyCloudUser):
    pass


class _B(MontyCloudUser):
    pass


def test_as_classes_wraps_a_bare_class():
    assert _as_classes(_A) == [_A]


def test_as_classes_passes_through_a_list():
    assert _as_classes([_A, _B]) == [_A, _B]
    assert _as_classes((_A, _B)) == [_A, _B]

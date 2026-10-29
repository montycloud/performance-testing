"""Locust glue shared by every scenario."""

from mcperf.locust.base_user import MontyCloudUser
from mcperf.locust.scenario import register_scenario

__all__ = ["MontyCloudUser", "register_scenario"]

from pathlib import Path

import pytest

from hrsync import rules

DEFAULT = Path(rules.__file__).with_name("rules.default.json")


@pytest.fixture
def r():
    return rules.load(DEFAULT)


def test_attributes_for_known(r):
    assert r.attributes_for("Sales_Team_Lead", "Toronto, ON", "Sales") == {
        "isManager": True, "country": "CAN", "state": None,
        "customCategory": "ON", "department": "Sales"}


def test_unknown_location_or_profile(r):
    assert r.attributes_for("Sales_Team_Lead", "Vancouver, BC", "Sales") is None
    assert r.missing("Sales_Team_Lead", "Vancouver, BC") == "location"
    assert r.attributes_for("Nope", "Toronto, ON", "Sales") is None
    assert r.missing("Nope", "Toronto, ON") == "profile"
    assert r.missing("Sales_Rep", "Toronto, ON") is None


def test_warehouse_category_overrides_location(r):
    a = r.attributes_for("Warehouse_Associate", "Minneapolis, MN", "Operations")
    assert a["customCategory"] == "WAREHOUSE" and a["state"] == "MN" and a["isManager"] is False


def test_expected_campaigns(r):
    a = r.attributes_for("Sales_Team_Lead", "Toronto, ON", "Sales")
    assert r.expected_campaigns(a) == {
        "Harassment Prevention (Ontario)", "Manager Fundamentals", "Data Privacy Essentials"}
    us = r.attributes_for("Warehouse_Supervisor", "Austin, TX", "Operations")
    assert r.expected_campaigns(us) == {
        "Harassment Prevention (US Managers)", "Manager Fundamentals",
        "Data Privacy Essentials", "Workplace Safety (Warehouse)"}
    emp = r.attributes_for("Sales_Rep", "New York, NY", "Sales")
    assert r.expected_campaigns(emp) == {
        "Harassment Prevention (US Employees)", "Data Privacy Essentials"}

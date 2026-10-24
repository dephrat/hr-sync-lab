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


def test_warehouse_keeps_location_province_and_gets_department(r):
    a = r.attributes_for("Warehouse_Associate", "Toronto, ON", "Operations")
    assert a["customCategory"] == "ON" and a["department"] == "Operations" and a["isManager"] is False

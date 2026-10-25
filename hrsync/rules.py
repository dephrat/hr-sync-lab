"""Rules table: Workday profile/location -> Ethena learner attributes.

Which courses those attributes lead to is decided in Ethena (campaign audiences), not here."""
import json
import os
import tempfile
from pathlib import Path

LearnerAttrs = dict


class Rules:
    def __init__(self, data: dict):
        self.locations = data["locations"]
        self.profiles = data["profiles"]

    def missing(self, job_profile_id: str, location_descriptor: str) -> str | None:
        if job_profile_id not in self.profiles:
            return "profile"
        if location_descriptor not in self.locations:
            return "location"
        return None

    def attributes_for(self, job_profile_id: str, location_descriptor: str,
                       department: str) -> LearnerAttrs | None:
        if self.missing(job_profile_id, location_descriptor):
            return None
        prof, loc = self.profiles[job_profile_id], self.locations[location_descriptor]
        return {"isManager": prof["isManager"], "country": loc["country"],
                "state": loc.get("state"),
                "customCategory": loc.get("customCategory"),
                "department": department}


def load(path) -> Rules:
    return Rules(json.loads(Path(path).read_text()))


CHOICES = {
    "countries": [{"value": "CAN", "label": "Canada"}, {"value": "USA", "label": "United States"}],
    "regions": {
        "CAN": [{"value": "ON", "label": "Ontario"}, {"value": "BC", "label": "British Columbia"},
                {"value": "AB", "label": "Alberta"}, {"value": "QC", "label": "Quebec"}],
        "USA": [{"value": "MN", "label": "Minnesota"}, {"value": "NY", "label": "New York"},
                {"value": "TX", "label": "Texas"}, {"value": "CA", "label": "California"},
                {"value": "WA", "label": "Washington"}],
    },
}


def office_row(data: dict, office: str) -> dict:
    """Canadian provinces live in customCategory (that is what the Ontario campaign audience
    matches); US states live in state."""
    loc = data["locations"].get(office)
    if not loc:
        return {"office": office, "country": None, "region": None}
    return {"office": office, "country": loc.get("country"),
            "region": loc.get("state") or loc.get("customCategory") or NO_REGION}


def job_row(data: dict, job: str, label: str) -> dict:
    prof = data["profiles"].get(job)
    return {"job": job, "label": label,
            "isManager": None if prof is None else prof.get("isManager")}


# An office can deliberately translate to a country with no province or state: Ethena then
# gets the country only, and decides by itself what that earns.
NO_REGION = "NONE"


def set_office(data: dict, office: str, country, region) -> None:
    if country is None:
        data["locations"].pop(office, None)
        return
    entry = {"country": country}
    if region not in (None, NO_REGION):
        entry["state" if country == "USA" else "customCategory"] = region
    data["locations"][office] = entry


def set_job(data: dict, job: str, is_manager) -> None:
    if is_manager is None:
        data["profiles"].pop(job, None)
        return
    data["profiles"][job] = {"isManager": is_manager}


def save(path, data: dict) -> None:
    """Write atomically: temp file in the same directory, then rename over the target."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rules-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

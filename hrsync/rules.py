"""Rules table: Workday profile/location -> Ethena learner attributes -> expected courses."""
import json
from pathlib import Path

LearnerAttrs = dict


class Rules:
    def __init__(self, data: dict):
        self.locations = data["locations"]
        self.profiles = data["profiles"]
        self.courses = data["courses"]

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
                "customCategory": prof.get("customCategory", loc.get("customCategory")),
                "department": department}

    def expected_courses(self, attrs: LearnerAttrs) -> set[str]:
        return {c["courseName"] for c in self.courses
                if all(attrs.get(k) == v for k, v in c["when"].items())}


def load(path) -> Rules:
    return Rules(json.loads(Path(path).read_text()))

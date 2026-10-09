"""Immutable, administrator-only development comparisons; never production releases."""

from .motion_annotations import require
from .synthetic_temporal_contract import IDENTIFIER, validate_document


class MotionLabelPreviews:
    def __init__(self, service):
        self.service = service

    def load(self, preview_id):
        require(bool(IDENTIFIER.fullmatch(preview_id)), "Invalid preview ID")
        value = self.service.store.read_json(f"{self.service.root}/previews/{preview_id}.json")[0]
        require(
            value["schema"] == "imu.motion_preview.v1" and value["preview_id"] == preview_id,
            "Invalid preview manifest",
        )
        return value

    @staticmethod
    def metadata(value):
        return {
            k: value[k]
            for k in ("preview_id", "title", "models", "activity_rules_version", "created_at_utc")
        }

    def previews(self):
        return {
            "previews": [
                self.metadata(self.load(item.key.rsplit("/", 1)[-1][:-5]))
                for item in self.service.store.list(self.service.root + "/previews/")
                if item.key.endswith(".json")
            ]
        }

    def listing(self, preview_id):
        value = self.load(preview_id)
        cases = []
        held_out = self.service.holdout_groups()
        for record in value["records"]:
            mid = record["motion_sha256"]
            feature = self.service.feature(mid)
            if feature.get("group_key") in held_out:
                continue
            cases.append(
                {
                    "motion_sha256": mid,
                    "status": "preview",
                    "entry": {
                        "candidate": record["candidate"],
                        "source_dataset": feature["source_dataset"],
                        "source_member": feature["source_member"],
                    },
                }
            )
        return {**self.metadata(value), "cases": cases, "total": len(cases)}

    def case(self, preview_id, mid):
        self.service.check_holdout(mid)
        value = self.load(preview_id)
        record = next((r for r in value["records"] if r["motion_sha256"] == mid), None)
        if record is None:
            raise FileNotFoundError(mid)
        candidate = record["candidate"]
        variant = self.service.variant(mid, candidate["candidate_id"], candidate["version_id"])
        require(variant is not None, "Preview variant is no longer available")
        documents = record["documents"]
        require(set(documents) == {"human", "baseline", "candidate"}, "Missing comparison")
        for document in documents.values():
            validate_document(document, expected_candidate=variant["candidate"])
            require(document["clock"] == variant["clock"], "Preview clock differs")
        detail = self.service.case(mid, variant)
        return {
            **detail,
            "status": "preview",
            "editor_template": documents["candidate"],
            "suggestion": None,
            "has_suggestion": False,
            "draft": None,
            "preview": {
                **self.metadata(value),
                "documents": documents,
                "human_revision": record["human_revision"],
                "read_only": True,
            },
        }

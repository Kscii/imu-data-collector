from types import SimpleNamespace

from imu_data_collector.storage import LocalFilesystemStore
from imu_data_collector.synthetic_labels import SyntheticLabelRegistry


def test_catalog_adds_definitions_without_mutating_real_or_motion_records(tmp_path):
    store = LocalFilesystemStore(tmp_path)
    real = SimpleNamespace(version="1.0.0+r13",
        fall=[SimpleNamespace(code="syncope_fall", name="既有真实名称", active=True)],
        non_fall=[SimpleNamespace(code="standing", name="自定义站立名称", active=False)])
    registry = SyntheticLabelRegistry(store, SimpleNamespace(current=lambda: (real, 13)))
    before = store.read_json(registry.KEY)
    catalog = registry.catalog()
    assert store.read_json(registry.KEY) == before
    assert catalog["definition_conflicts"] == []
    assert len(catalog["temporal"]["primary"]) == 24
    assert len(catalog["temporal"]["training_profile"]["codes"]) == 20
    shared = {c["code"]: c for c in catalog["concepts"] if c["scope"] == "shared"}
    assert shared["standing"]["name"] == "自定义站立名称"
    assert shared["standing"]["active"] is False
    assert shared["syncope_fall"]["name"] == "既有真实名称"
    assert real.version == "1.0.0+r13"

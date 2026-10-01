from __future__ import annotations

import sqlite3

import pytest

from app.core.errors import ConflictError, ValidationError
from app.database import database_path, get_connection, transaction
from app.germplasm.service import GermplasmService
from tests.test_germplasm_workflow import create_stored_lot


def split_payload(service: GermplasmService, lot_id: int, weights: list[float], key: str, suffix: str = "1") -> dict:
    return {
        "source_lot_id": lot_id,
        "targets": [
            {"lot_no": f"LOT-CHILD-{suffix}-{index:02d}", "weight_grams": weight}
            for index, weight in enumerate(weights, start=1)
        ],
        "idempotency_key": key,
        "actor": "保管员",
        "reason": "交换备份分装",
    }


def test_split_deducts_once_and_creates_children(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "010")
        event = service.lineage.split(split_payload(service, lot["id"], [100, 80.5], "split-010-0001", "010"))
        assert event["event_type"] == "split"
        assert event["replayed"] is False
        output_components = [c for c in event["components"] if c["component_role"] == "output"]
        assert [round(c["weight_grams"], 6) for c in output_components] == [100, 80.5]
        source = service.repository.require_lot(lot["id"])
        assert round(source["available_weight_grams"], 6) == 319.5
        assert source["version"] == lot["version"] + 1
        for component in output_components:
            child = component["lot"]
            assert child["parent_lot_id"] == lot["id"]
            assert child["status"] == "pending"
            assert round(child["available_weight_grams"], 6) == round(component["weight_grams"], 6)
        conservation = service.lineage.conservation_check(lot["id"])
        assert conservation["balanced"] is True
        assert conservation["anomalies"] == []
        view = service.lineage.lineage_view(lot["id"])
        assert len(view["descendants"]) == 1
        assert view["ancestors"] == []


def test_split_is_atomic_when_total_exceeds_available(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "011")
        before = service.repository.require_lot(lot["id"])
        with pytest.raises(ConflictError) as excinfo:
            service.lineage.split(split_payload(service, lot["id"], [300, 201], "split-011-0001", "011"))
        assert "可用重量不足" in excinfo.value.message
        after = service.repository.require_lot(lot["id"])
        assert after["available_weight_grams"] == before["available_weight_grams"]
        assert after["version"] == before["version"]
        assert connection.execute("SELECT COUNT(*) FROM lot_lineage_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM lot_lineage_components").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM seed_lots WHERE lot_no LIKE 'LOT-CHILD-011-%'").fetchone()[0] == 0


def test_split_duplicate_target_number_rolls_back(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "012")
        payload = split_payload(service, lot["id"], [10, 10], "split-012-0001", "012")
        payload["targets"][1]["lot_no"] = payload["targets"][0]["lot_no"]
        with pytest.raises(ValidationError):
            service.lineage.split(payload)
        assert connection.execute("SELECT COUNT(*) FROM lot_lineage_events").fetchone()[0] == 0


def test_split_replay_returns_same_lineage_without_second_deduction(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "013")
        payload = split_payload(service, lot["id"], [50, 50], "split-013-0001", "013")
        first = service.lineage.split(payload)
        second = service.lineage.split(payload)
        assert second["id"] == first["id"]
        assert second["replayed"] is True
        source = service.repository.require_lot(lot["id"])
        assert round(source["available_weight_grams"], 6) == 400
        assert connection.execute("SELECT COUNT(*) FROM lot_lineage_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM lot_movements WHERE movement_type='分装'").fetchone()[0] == 1


def test_replay_key_cannot_cross_split_and_merge(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, first, _ = create_stored_lot(service, "031")
        second = _make_second_lot(service, accession, "LOT-031-B", 100)
        service.lineage.split(split_payload(service, first["id"], [50], "key-031-shared", "031"))
        with pytest.raises(ConflictError, match="分装"):
            service.lineage.merge({
                "inputs": [{"lot_id": first["id"], "weight_grams": 50}, {"lot_id": second["id"], "weight_grams": 50}],
                "result": {"lot_no": "LOT-031-MERGED"}, "idempotency_key": "key-031-shared", "actor": "保管员",
            })


def test_split_blocked_under_hold(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "014")
        service.inventory.impose_hold({
            "lot_id": lot["id"], "hold_type": "质量", "reason": "等待复检结论", "actor": "审核员",
        })
        with pytest.raises(ConflictError):
            service.lineage.split(split_payload(service, lot["id"], [10], "split-014-0001", "014"))


def _make_second_lot(service: GermplasmService, accession: dict, lot_no: str, weight: float) -> dict:
    lot = service.inventory.create_lot({
        "lot_no": lot_no, "accession_id": accession["id"], "parent_lot_id": None,
        "harvest_year": 2025, "initial_weight_grams": weight, "moisture_percent": 7.5,
        "treatment": "清选干燥", "sealed_on": "2026-09-02", "created_by": "登记员",
    })
    return service.repository.lot_detail(lot["id"])


def test_merge_combines_compatible_lots_and_conserves_weight(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, first, _ = create_stored_lot(service, "020")
        second = _make_second_lot(service, accession, "LOT-020-B", 200)
        event = service.lineage.merge({
            "inputs": [
                {"lot_id": first["id"], "weight_grams": 100},
                {"lot_id": second["id"], "weight_grams": 200},
            ],
            "result": {"lot_no": "LOT-020-MERGED", "created_by": "保管员"},
            "idempotency_key": "merge-020-0001",
            "actor": "保管员",
            "reason": "余量合并",
        })
        assert event["event_type"] == "merge"
        result_id = next(
            c["lot_id"] for c in event["components"] if c["component_role"] == "output"
        )
        result = service.repository.require_lot(result_id)
        assert round(result["available_weight_grams"], 6) == 300
        assert service.repository.require_lot(first["id"])["available_weight_grams"] == 400
        assert service.repository.require_lot(second["id"])["available_weight_grams"] == 0
        assert service.repository.require_lot(second["id"])["status"] == "depleted"
        for lot_id in (first["id"], second["id"], result_id):
            assert service.lineage.conservation_check(lot_id)["balanced"] is True
        view = service.lineage.lineage_view(result_id)
        assert view["ancestors"] and view["descendants"] == []
        assert service.inventory.reconcile(first["id"])["available_matches_ledger"] is True


def test_merge_rejects_cross_accession(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, first, _ = create_stored_lot(service, "021")
        other_accession, _, _ = create_stored_lot(service, "022")
        other = _make_second_lot(service, other_accession, "LOT-022-A", 100)
        with pytest.raises(ValidationError, match="同一种质资源"):
            service.lineage.merge({
                "inputs": [{"lot_id": first["id"], "weight_grams": 10}, {"lot_id": other["id"], "weight_grams": 10}],
                "result": {"lot_no": "LOT-MERGE-X"},
                "idempotency_key": "merge-021-0001", "actor": "保管员",
            })


def test_merge_rejects_incompatible_treatment_year_and_viability(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, first, _ = create_stored_lot(service, "023")
        different_year = service.inventory.create_lot({
            "lot_no": "LOT-023-Y", "accession_id": accession["id"], "parent_lot_id": None,
            "harvest_year": 2024, "initial_weight_grams": 100, "moisture_percent": 7.5,
            "treatment": "清选干燥", "sealed_on": "2026-09-02", "created_by": "登记员",
        })["id"]
        with pytest.raises(ValidationError, match="收获年份"):
            service.lineage.merge({
                "inputs": [{"lot_id": first["id"], "weight_grams": 10}, {"lot_id": different_year, "weight_grams": 10}],
                "result": {"lot_no": "LOT-MERGE-Y"}, "idempotency_key": "merge-023-y", "actor": "保管员",
            })

        different_treatment = service.inventory.create_lot({
            "lot_no": "LOT-023-T", "accession_id": accession["id"], "parent_lot_id": None,
            "harvest_year": 2025, "initial_weight_grams": 100, "moisture_percent": 7.5,
            "treatment": "熏蒸", "sealed_on": "2026-09-02", "created_by": "登记员",
        })["id"]
        with pytest.raises(ValidationError, match="处理条件"):
            service.lineage.merge({
                "inputs": [{"lot_id": first["id"], "weight_grams": 10}, {"lot_id": different_treatment, "weight_grams": 10}],
                "result": {"lot_no": "LOT-MERGE-T"}, "idempotency_key": "merge-023-t", "actor": "保管员",
            })

        protocol = service.viability.create_protocol({
            "protocol_code": "PR-RICE", "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
            "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "正常种苗", "created_by": "检测员",
        })
        same_band = service.inventory.create_lot({
            "lot_no": "LOT-023-V1", "accession_id": accession["id"], "parent_lot_id": None,
            "harvest_year": 2025, "initial_weight_grams": 100, "moisture_percent": 7.5,
            "treatment": "清选干燥", "sealed_on": "2026-09-02", "created_by": "登记员",
        })["id"]
        other_band = service.inventory.create_lot({
            "lot_no": "LOT-023-V2", "accession_id": accession["id"], "parent_lot_id": None,
            "harvest_year": 2025, "initial_weight_grams": 100, "moisture_percent": 7.5,
            "treatment": "清选干燥", "sealed_on": "2026-09-02", "created_by": "登记员",
        })["id"]
        for lot_id, germination in ((same_band, 90.0), (other_band, 40.0)):
            connection.execute(
                "INSERT INTO viability_tests(test_no,lot_id,protocol_id,test_type,sampled_grams,scheduled_for,"
                "status,germination_percent,vigor_index,requested_by,performed_by,completed_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'completed',?,80,'检测员','检测员','2026-09-20T00:00:00','2026-09-20T00:00:00','2026-09-20T00:00:00')",
                (f"VT-{lot_id}", lot_id, protocol["id"], "入库初检", 5, "2026-09-10", germination),
            )
        with pytest.raises(ValidationError, match="活力"):
            service.lineage.merge({
                "inputs": [{"lot_id": same_band, "weight_grams": 10}, {"lot_id": other_band, "weight_grams": 10}],
                "result": {"lot_no": "LOT-MERGE-V"}, "idempotency_key": "merge-023-v", "actor": "保管员",
            })


def test_merge_hold_blocks_and_partial_failure_rolls_back(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, first, _ = create_stored_lot(service, "024")
        second = _make_second_lot(service, accession, "LOT-024-B", 200)
        service.inventory.impose_hold({
            "lot_id": second["id"], "hold_type": "质量", "reason": "待复核", "actor": "审核员",
        })
        with pytest.raises(ConflictError, match="冻结"):
            service.lineage.merge({
                "inputs": [{"lot_id": first["id"], "weight_grams": 100}, {"lot_id": second["id"], "weight_grams": 200}],
                "result": {"lot_no": "LOT-024-MERGED"}, "idempotency_key": "merge-024-hold", "actor": "保管员",
            })
        assert connection.execute("SELECT COUNT(*) FROM lot_lineage_events").fetchone()[0] == 0
        assert service.repository.require_lot(first["id"])["available_weight_grams"] == 500

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession2, first2, _ = create_stored_lot(service, "029")
        second2 = _make_second_lot(service, accession2, "LOT-029-B", 200)
        # 第二个输入重量超过可用量：整次合并回滚，第一个输入也不能被扣减。
        with pytest.raises(ConflictError, match="可用重量不足"):
            service.lineage.merge({
                "inputs": [{"lot_id": first2["id"], "weight_grams": 100}, {"lot_id": second2["id"], "weight_grams": 201}],
                "result": {"lot_no": "LOT-029-MERGED"}, "idempotency_key": "merge-029-short", "actor": "保管员",
            })
        assert connection.execute("SELECT COUNT(*) FROM lot_lineage_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM seed_lots WHERE lot_no='LOT-029-MERGED'").fetchone()[0] == 0
        assert service.repository.require_lot(first2["id"])["available_weight_grams"] == 500
        assert service.repository.require_lot(second2["id"])["available_weight_grams"] == 200


def test_split_then_merge_traces_up_and_down(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, root, _ = create_stored_lot(service, "025")
        split_event = service.lineage.split(split_payload(service, root["id"], [120, 120], "split-025-0001", "025"))
        child_ids = sorted(
            c["lot_id"] for c in split_event["components"] if c["component_role"] == "output"
        )
        extra = _make_second_lot(service, accession, "LOT-025-X", 60)
        merge_event = service.lineage.merge({
            "inputs": [{"lot_id": child_ids[0], "weight_grams": 120}, {"lot_id": extra["id"], "weight_grams": 60}],
            "result": {"lot_no": "LOT-025-MERGED"}, "idempotency_key": "merge-025-0001", "actor": "保管员",
        })
        merged_id = next(c["lot_id"] for c in merge_event["components"] if c["component_role"] == "output")
        upward = service.lineage.lineage_view(merged_id)
        upward_event_ids = [event["id"] for event in upward["ancestors"]]
        assert merge_event["id"] in upward_event_ids and split_event["id"] in upward_event_ids
        downward = service.lineage.lineage_view(root["id"])
        downward_event_ids = [event["id"] for event in downward["descendants"]]
        assert split_event["id"] in downward_event_ids and merge_event["id"] in downward_event_ids
        untouched_child = service.repository.require_lot(child_ids[1])
        assert untouched_child["available_weight_grams"] == 120


def test_lineage_ledger_is_immutable_and_history_lots_survive(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "026")
        event = service.lineage.split(split_payload(service, lot["id"], [500], "split-026-0001", "026"))
        source = service.repository.require_lot(lot["id"])
        assert source["status"] == "depleted"
        detail = service.repository.lot_detail(lot["id"])
        assert any(m["movement_type"] == "分装" for m in detail["movements"])

        for statement in (
            "UPDATE lot_lineage_events SET reason='篡改' WHERE id=?",
            "DELETE FROM lot_lineage_events WHERE id=?",
            "UPDATE lot_lineage_components SET weight_grams=1 WHERE event_id=?",
            "DELETE FROM lot_lineage_components WHERE event_id=?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (event["id"],))

        # 已耗尽的历史批次被谱系事件引用，不能删除，仍可追溯。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM seed_lots WHERE id=?", (lot["id"],))
        assert service.lineage.lineage_view(lot["id"])["lot"]["id"] == lot["id"]


def test_conservation_check_flags_corrupted_weight(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "027")
        service.lineage.split(split_payload(service, lot["id"], [100], "split-027-0001", "027"))
        connection.execute("UPDATE seed_lots SET available_weight_grams=available_weight_grams+40 WHERE id=?", (lot["id"],))
        report = GermplasmService(connection).lineage.conservation_check(lot["id"])
        assert report["balanced"] is False
        assert any(item["type"] == "available_weight_drift" for item in report["anomalies"])


def test_stale_version_update_cannot_double_deduct(client, tmp_path):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "028")
        lot_id, stale_version = lot["id"], lot["version"] + 1  # 入库后版本已经 +1

    other = sqlite3.connect(database_path(), timeout=15)
    other.execute("PRAGMA foreign_keys=ON")
    other.execute("BEGIN IMMEDIATE")
    other.execute(
        "UPDATE seed_lots SET available_weight_grams=ROUND(available_weight_grams-?,6),version=version+1,"
        "updated_at='2026-10-01T00:00:00' WHERE id=? AND version=? AND available_weight_grams+?>=0",
        (100, lot_id, stale_version - 1, 1e-6),
    )
    other.commit()
    other.close()

    with transaction(immediate=True) as connection:
        # 旧版本守卫条件不再命中任何行：扣减不会重复发生。
        cursor = connection.execute(
            "UPDATE seed_lots SET available_weight_grams=ROUND(available_weight_grams-?,6),version=version+1 "
            "WHERE id=? AND version=? AND available_weight_grams+?>=0",
            (100, lot_id, stale_version - 1, 1e-6),
        )
        assert cursor.rowcount == 0
        service = GermplasmService(connection)
        assert service.repository.require_lot(lot_id)["available_weight_grams"] == 400


def test_lineage_api_endpoints(client, admin):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service, "030")
        lot_id = lot["id"]

    split_response = client.post(
        "/api/germplasm/lineage/splits",
        headers=admin["headers"],
        json={
            "source_lot_id": lot_id,
            "targets": [{"lot_no": "LOT-HTTP-CHILD-1", "weight_grams": 120}],
            "idempotency_key": "split-http-0001",
            "actor": "保管员",
        },
    )
    assert split_response.status_code == 201, split_response.text
    event_id = split_response.json()["id"]
    event_response = client.get(f"/api/germplasm/lineage/events/{event_id}", headers=admin["headers"])
    assert event_response.status_code == 200
    lineage_response = client.get(f"/api/germplasm/lots/{lot_id}/lineage", headers=admin["headers"])
    assert lineage_response.status_code == 200
    assert lineage_response.json()["conservation"]["balanced"] is True
    conservation_response = client.get(f"/api/germplasm/lots/{lot_id}/conservation", headers=admin["headers"])
    assert conservation_response.json()["balanced"] is True

    assert client.post(
        "/api/germplasm/lineage/splits",
        json={
            "source_lot_id": lot_id,
            "targets": [{"lot_no": "LOT-NOAUTH", "weight_grams": 1}],
            "idempotency_key": "split-noauth-001",
            "actor": "保管员",
        },
    ).status_code == 401
    assert client.get(f"/api/germplasm/lots/{lot_id}/lineage").status_code == 401

    invalid = client.post(
        "/api/germplasm/lineage/splits",
        headers=admin["headers"],
        json={"source_lot_id": lot_id, "targets": [], "idempotency_key": "x", "actor": "保管员"},
    )
    assert invalid.status_code == 422

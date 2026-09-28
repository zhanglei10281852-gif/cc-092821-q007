from __future__ import annotations

from datetime import date

from app.database import get_connection, transaction
from app.germplasm.service import GermplasmService


def create_accepted_accession(service: GermplasmService, suffix: str = "001") -> dict:
    source = service.accessions.create_source({
        "source_code": f"SRC-{suffix}", "provider_name": "省级采集队", "country_code": "CN",
        "locality": "河谷试验站", "collected_on": "2025-10-02", "permit_reference": "P-88",
        "restrictions": {},
    })
    accession = service.accessions.create_accession({
        "accession_no": f"ACC-{suffix}", "scientific_name": "Oryza sativa", "crop_name": "水稻",
        "cultivar_name": "地方材料", "source_id": source["id"], "acquisition_type": "采集",
        "received_on": "2026-09-01", "passport": {"latitude": 30.1}, "created_by": "登记员",
    })
    return service.accessions.transition(accession["id"], {
        "target_status": "accepted", "reason": "资料与检疫证明齐全", "expected_version": 1, "actor": "审核员",
    })


def create_stored_lot(service: GermplasmService, suffix: str = "001") -> tuple[dict, dict, dict]:
    accession = create_accepted_accession(service, suffix)
    location = service.inventory.create_location({
        "location_code": f"COLD-{suffix}", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
        "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
    })
    lot = service.inventory.create_lot({
        "lot_no": f"LOT-{suffix}", "accession_id": accession["id"], "parent_lot_id": None,
        "harvest_year": 2025, "initial_weight_grams": 500, "moisture_percent": 7.5,
        "treatment": "清选干燥", "sealed_on": "2026-09-02", "created_by": "登记员",
    })
    placed = service.inventory.place_lot({
        "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 500,
        "container_code": f"BOX-{suffix}", "idempotency_key": f"place-{suffix}-0001", "actor": "保管员",
    })
    return accession, service.repository.lot_detail(lot["id"]), placed["placement"]


def test_accession_intake_and_version_conflict(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession = create_accepted_accession(service)
        assert accession["status"] == "accepted"
        assert [item["event_type"] for item in accession["events"]] == ["created", "status_changed"]
        try:
            service.accessions.update_accession(accession["id"], {
                "crop_name": "稻", "expected_version": 1, "actor": "登记员",
            })
        except ConflictError as exc:
            assert exc.context["current_version"] == 2
        else:
            raise AssertionError("旧版本更新应被拒绝")


def test_inventory_idempotency_and_capacity(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service)
        replay = service.inventory.place_lot({
            "lot_id": lot["id"], "location_id": placement["location_id"], "weight_grams": 500,
            "container_code": "BOX-001", "idempotency_key": "place-001-0001", "actor": "保管员",
        })
        assert replay["replayed"] is True
        too_small = service.inventory.create_location({
            "location_code": "SMALL-001", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_grams": 100, "temperature_c": -18, "humidity_percent": 30,
        })
        try:
            service.inventory.move_placement(placement["id"], {
                "target_location_id": too_small["id"], "expected_version": 1,
                "idempotency_key": "move-001-0001", "actor": "保管员", "reason": "库位整理",
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("容量不足的移库应被拒绝")


def test_hold_blocks_withdrawal_until_release(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service)
        hold = service.inventory.impose_hold({
            "lot_id": lot["id"], "hold_type": "质量", "reason": "等待复核", "actor": "审核员",
        })
        try:
            service.inventory.withdraw({
                "lot_id": lot["id"], "quantity_grams": 10, "movement_type": "领用",
                "idempotency_key": "withdraw-001-a", "actor": "保管员", "reason": "试验",
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("冻结批次不应允许领用")
        service.inventory.release_hold(hold["id"], "审核员", "复核通过")
        result = service.inventory.withdraw({
            "lot_id": lot["id"], "quantity_grams": 10, "movement_type": "领用",
            "idempotency_key": "withdraw-001-b", "actor": "保管员", "reason": "试验",
        })
        assert result["lot"]["available_weight_grams"] == 490


def test_viability_completion_creates_schedule(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service)
        protocol = service.viability.create_protocol({
            "protocol_code": "RICE-GER", "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
            "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽发育完整",
            "created_by": "技术负责人",
        })
        service.viability.create_policy({
            "crop_name": "水稻", "risk_level": "medium", "interval_months": 12, "warning_days": 30,
            "minimum_germination_percent": 75, "effective_from": "2026-01-01", "effective_to": None,
            "created_by": "技术负责人",
        })
        test = service.viability.schedule_test({
            "test_no": "VT-001", "lot_id": lot["id"], "protocol_id": protocol["id"], "test_type": "周期复检",
            "sampled_grams": 5, "scheduled_for": "2026-09-25", "requested_by": "检测员",
            "idempotency_key": "schedule-vt-001",
        })
        running = service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
        assert running["status"] == "running"
        for replicate, normal in [(1, 80), (2, 82)]:
            service.viability.add_count(test["id"], {
                "replicate_no": replicate, "seeds_tested": 100, "normal_count": normal,
                "abnormal_count": 10, "dead_count": 100 - normal - 10, "fresh_count": 0,
                "observation_day": 14, "observed_by": "检测员",
            })
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        assert completed["germination_percent"] == 81
        due = service.viability.due_schedules(date(2028, 1, 1))
        assert len(due) == 1
        assert due[0]["due_on"].startswith("2027-")


def test_environment_reading_is_idempotent_and_alerts(client):
    from datetime import UTC, datetime

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        location = service.inventory.create_location({
            "location_code": "ENV-001", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        payload = {
            "location_id": location["id"], "observed_at": datetime(2026, 9, 25, 8, 0, tzinfo=UTC),
            "temperature_c": -5, "humidity_percent": 31, "source_key": "sensor-001-0800",
        }
        first = service.quality.add_reading(payload)
        second = service.quality.add_reading(payload)
        assert first["replayed"] is False and len(first["alerts"]) == 1
        assert second["replayed"] is True


def test_distribution_approval_allocates_eligible_lot(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, _, _ = create_stored_lot(service)
        request = service.distribution.create_request({
            "request_no": "DIST-001", "requester": "作物研究所", "purpose": "抗旱鉴定",
            "items": [{"accession_id": accession["id"], "quantity_grams": 20}],
        })
        submitted = service.distribution.submit(request["id"], 1)
        approved = service.distribution.decide(request["id"], {
            "approve": True, "expected_version": submitted["version"], "actor": "资源审核员", "reason": "材料充足",
        })
        assert approved["status"] == "approved"
        assert approved["items"][0]["allocated_lot_id"] is not None

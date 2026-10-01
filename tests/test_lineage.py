from __future__ import annotations

import sqlite3

import pytest

from app.core.errors import ConflictError
from app.database import transaction
from app.germplasm.service import GermplasmService
from tests.test_germplasm_workflow import create_stored_lot


def make_lot(service: GermplasmService, suffix: str, weight: float = 500, *, treatment: str = "清选干燥", year: int = 2025):
    accession, lot, _ = create_stored_lot(service, suffix)
    return accession, service.repository.require_lot(lot["id"])


def split(service: GermplasmService, lot, targets, key, *, expected_version=None, reason="交换备份分装"):
    return service.lineage.split_lot({
        "source_lot_id": lot["id"],
        "expected_version": lot["version"] if expected_version is None else expected_version,
        "targets": targets,
        "business_key": key,
        "actor": "保管员",
        "reason": reason,
    })


def complete_viability(service: GermplasmService, protocol, lot_id, test_no, normals, *, sampled_grams=5, key=None):
    test = service.viability.schedule_test({
        "test_no": test_no, "lot_id": lot_id, "protocol_id": protocol["id"], "test_type": "入库初检",
        "sampled_grams": sampled_grams, "scheduled_for": "2026-09-25", "requested_by": "检测员",
        "idempotency_key": key or f"sch-{test_no}",
    })
    service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
    for replicate, normal in enumerate(normals, start=1):
        service.viability.add_count(test["id"], {
            "replicate_no": replicate, "seeds_tested": 100, "normal_count": normal,
            "abnormal_count": 0, "dead_count": 100 - normal, "observation_day": 14, "observed_by": "检测员",
        })
    return service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})


def rice_protocol(service: GermplasmService):
    return service.viability.create_protocol({
        "protocol_code": "RICE-LINEAGE", "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
        "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽完整", "created_by": "技术负责人",
    })


# --------------------------------------------------------------- 不可变账本

def test_lineage_tables_reject_update_and_delete(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L01")
        result = split(service, lot, [{"lot_no": "SUB-L01-A", "weight_grams": 200}], "line-key-0001")
        event_id = result["event"]["id"]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE lineage_events SET actor='改写者' WHERE id=?", (event_id,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM lineage_events WHERE id=?", (event_id,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE lineage_ledger_entries SET weight_grams=999 WHERE event_id=?", (event_id,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM lineage_ledger_entries WHERE event_id=?", (event_id,))


def test_history_lot_cannot_be_deleted_but_stays_traceable(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L02")
        result = split(service, lot, [{"lot_no": "SUB-L02-A", "weight_grams": 500}], "line-key-0002")
        child_id = result["children"][0]["id"]
        # 已用于分装（形成历史谱系）的来源批次不能删除
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM seed_lots WHERE id=?", (lot["id"],))
        # 子批次被领用耗尽后仍不能删除，且仍可向上溯源
        service.inventory.withdraw({
            "lot_id": child_id, "quantity_grams": 500, "movement_type": "领用",
            "idempotency_key": "use-child-l02", "actor": "保管员", "reason": "全部发放",
        })
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM seed_lots WHERE id=?", (child_id,))
        lineage = service.lineage.lot_lineage(child_id)
        assert [item["lot_no"] for item in lineage["ancestors"]] == ["LOT-L02"]


# --------------------------------------------------------------- 分装

def test_split_deducts_once_for_multiple_targets_and_is_conserved(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L03")
        result = split(service, lot, [
            {"lot_no": "SUB-L03-A", "weight_grams": 120.5},
            {"lot_no": "SUB-L03-B", "weight_grams": 80},
            {"lot_no": "SUB-L03-C", "weight_grams": 99},
        ], "line-key-0003")
        assert result["replayed"] is False
        source = service.repository.require_lot(lot["id"])
        assert source["available_weight_grams"] == pytest.approx(200.5)
        assert source["version"] == lot["version"] + 1
        assert [item["initial_weight_grams"] for item in result["children"]] == [120.5, 80.0, 99.0]
        entries = result["event"]["entries"]
        assert [(e["role"], e["lot_no"]) for e in entries] == [
            ("source", "LOT-L03"), ("product", "SUB-L03-A"), ("product", "SUB-L03-B"), ("product", "SUB-L03-C"),
        ]
        report = service.lineage.conservation_check(lot["id"])
        assert report["conserved"] is True
        assert report["movements_outside_ledger"] == []


def test_split_is_atomic_when_any_target_fails(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L04")
        before = service.repository.require_lot(lot["id"])["available_weight_grams"]
        # 第二个子批次编号重复 -> 整次回滚，来源重量不变、子批次不存在
        with pytest.raises(ConflictError):
            split(service, lot, [
                {"lot_no": "SUB-L04-A", "weight_grams": 100},
                {"lot_no": "SUB-L04-A", "weight_grams": 50},
            ], "line-key-0004-dup")
        # 超重同样回滚
        with pytest.raises(ConflictError):
            split(service, lot, [
                {"lot_no": "SUB-L04-X", "weight_grams": 400},
                {"lot_no": "SUB-L04-Y", "weight_grams": 200},
            ], "line-key-0004-heavy")
        after = service.repository.require_lot(lot["id"])
        assert after["available_weight_grams"] == before
        assert after["version"] == lot["version"]
        assert connection.execute("SELECT COUNT(*) FROM lineage_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM seed_lots WHERE lot_no LIKE 'SUB-L04-%'").fetchone()[0] == 0


def test_split_version_conflict_never_changes_weight(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L05")
        with pytest.raises(ConflictError) as excinfo:
            split(service, lot, [{"lot_no": "SUB-L05-A", "weight_grams": 100}],
                  "line-key-0005", expected_version=lot["version"] + 5)
        assert excinfo.value.context["current_version"] == lot["version"]
        current = service.repository.require_lot(lot["id"])
        assert current["available_weight_grams"] == lot["available_weight_grams"]
        assert current["version"] == lot["version"]


def test_split_rejects_held_and_depleted_lot(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L06")
        hold = service.inventory.impose_hold({
            "lot_id": lot["id"], "hold_type": "质量", "reason": "待复核", "actor": "审核员",
        })
        with pytest.raises(ConflictError):
            split(service, lot, [{"lot_no": "SUB-L06-A", "weight_grams": 10}], "line-key-0006")
        service.inventory.release_hold(hold["id"], "审核员", "复核通过")
        service.inventory.withdraw({
            "lot_id": lot["id"], "quantity_grams": 500, "movement_type": "报废",
            "idempotency_key": "deplete-l06", "actor": "保管员", "reason": "全部报废",
        })
        depleted = service.repository.require_lot(lot["id"])
        assert depleted["status"] == "depleted"
        with pytest.raises(ConflictError):
            split(service, depleted, [{"lot_no": "SUB-L06-B", "weight_grams": 1}], "line-key-0006b")


def test_split_business_key_retry_returns_original_lineage(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L07")
        payload = {"targets": [{"lot_no": "SUB-L07-A", "weight_grams": 100}]}
        first = split(service, lot, payload["targets"], "line-key-0007")
        current = service.repository.require_lot(lot["id"])
        # 重试携带错误的版本号也不影响：直接返回原谱系，不二次扣减
        retry = split(service, current, [{"lot_no": "SUB-L07-A", "weight_grams": 100}],
                      "line-key-0007", expected_version=999)
        assert retry["replayed"] is True
        assert retry["event"]["id"] == first["event"]["id"]
        assert service.repository.require_lot(lot["id"])["available_weight_grams"] == 400
        # 同键即使携带不同载荷也只返回原谱系，不会产生第二个事件或第二次扣减
        retry_other_payload = split(service, current, [{"lot_no": "SUB-L99-Z", "weight_grams": 1}],
                                    "line-key-0007", expected_version=999)
        assert retry_other_payload["replayed"] is True
        assert retry_other_payload["event"]["id"] == first["event"]["id"]
        assert connection.execute("SELECT COUNT(*) FROM lineage_events").fetchone()[0] == 1
        assert service.repository.require_lot(lot["id"])["available_weight_grams"] == 400


# --------------------------------------------------------------- 合并

def _two_compatible_children(service: GermplasmService, suffix: str):
    _, parent = make_lot(service, suffix)
    result = split(service, parent, [
        {"lot_no": f"SUB-{suffix}-A", "weight_grams": 200},
        {"lot_no": f"SUB-{suffix}-B", "weight_grams": 200},
    ], f"line-split-{suffix}")
    protocol = rice_protocol(service)
    children = []
    for child, normals, test_no in [
        (result["children"][0], [90, 92], f"VT-{suffix}-A"),
        (result["children"][1], [76, 78], f"VT-{suffix}-B"),
    ]:
        complete_viability(service, protocol, child["id"], test_no, normals)
        children.append(service.repository.require_lot(child["id"]))
    return parent, children


def test_merge_requires_matching_resource_treatment_year_hold_viability(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, children = _two_compatible_children(service, "L08")
        # 跨资源：另建一个资源批次
        other_acc, other_lot = make_lot(service, "L08X")
        with pytest.raises(ConflictError, match="同一资源"):
            service.lineage.merge_lots({
                "sources": [
                    {"lot_id": children[0]["id"], "weight_grams": 50, "expected_version": children[0]["version"]},
                    {"lot_id": other_lot["id"], "weight_grams": 50, "expected_version": other_lot["version"]},
                ],
                "target_lot_no": "MERGE-X", "business_key": "merge-x-resource", "actor": "保管员",
            })
        # 冻结
        hold = service.inventory.impose_hold({
            "lot_id": children[1]["id"], "hold_type": "争议", "reason": "数量争议", "actor": "审核员",
        })
        with pytest.raises(ConflictError, match="冻结"):
            service.lineage.merge_lots({
                "sources": [
                    {"lot_id": children[0]["id"], "weight_grams": 50, "expected_version": children[0]["version"]},
                    {"lot_id": children[1]["id"], "weight_grams": 50, "expected_version": children[1]["version"]},
                ],
                "target_lot_no": "MERGE-H", "business_key": "merge-x-hold", "actor": "保管员",
            })
        service.inventory.release_hold(hold["id"], "审核员", "争议解除")
        # 活力档位相差两级（high 3 与 low 1）拒绝：把 B 最近活力做到低档，A 仍为高档
        _, low_children = _two_compatible_children(service, "L08L")
        protocol = service.repository.require_protocol(
            connection.execute("SELECT id FROM viability_protocols WHERE protocol_code='RICE-LINEAGE'").fetchone()[0]
        )
        complete_viability(service, protocol, low_children[1]["id"], "VT-L08L-B2", [55, 57], key="sch-low-b")
        refreshed_low = service.repository.require_lot(low_children[1]["id"])
        with pytest.raises(ConflictError, match="活力档位"):
            service.lineage.merge_lots({
                "sources": [
                    {"lot_id": low_children[0]["id"], "weight_grams": 30, "expected_version": low_children[0]["version"]},
                    {"lot_id": refreshed_low["id"], "weight_grams": 30, "expected_version": refreshed_low["version"]},
                ],
                "target_lot_no": "MERGE-V", "business_key": "merge-x-viab", "actor": "保管员",
            })
        # 处理条件 / 收获年份不一致：直接篡改属性验证（账本之外的字段仍由领域逻辑校验）
        connection.execute("UPDATE seed_lots SET treatment='不同处理' WHERE id=?", (children[1]["id"],))
        with pytest.raises(ConflictError, match="处理条件"):
            service.lineage.merge_lots({
                "sources": [
                    {"lot_id": children[0]["id"], "weight_grams": 30, "expected_version": children[0]["version"]},
                    {"lot_id": children[1]["id"], "weight_grams": 30, "expected_version": children[1]["version"] + 1},
                ],
                "target_lot_no": "MERGE-T", "business_key": "merge-x-treatment", "actor": "保管员",
            })


def test_merge_rolls_back_when_any_source_fails(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, children = _two_compatible_children(service, "L09")
        available_before = [service.repository.require_lot(c["id"])["available_weight_grams"] for c in children]
        # 第二个来源版本号过期；第一个来源也不得被扣减
        with pytest.raises(ConflictError):
            service.lineage.merge_lots({
                "sources": [
                    {"lot_id": children[0]["id"], "weight_grams": 100, "expected_version": children[0]["version"]},
                    {"lot_id": children[1]["id"], "weight_grams": 100, "expected_version": children[1]["version"] + 1},
                ],
                "target_lot_no": "MERGE-L09", "business_key": "merge-l09-fail", "actor": "保管员", "reason": "余量合并",
            })
        for child, before in zip(children, available_before):
            assert service.repository.require_lot(child["id"])["available_weight_grams"] == before
        assert connection.execute("SELECT COUNT(*) FROM seed_lots WHERE lot_no='MERGE-L09'").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM lineage_events WHERE business_key IN ('merge-l09-fail','merge-l09-heavy')"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM lineage_events WHERE event_type='合并'"
        ).fetchone()[0] == 0
        # 超重回滚
        with pytest.raises(ConflictError):
            service.lineage.merge_lots({
                "sources": [
                    {"lot_id": children[0]["id"], "weight_grams": 10_000, "expected_version": children[0]["version"]},
                    {"lot_id": children[1]["id"], "weight_grams": 10, "expected_version": children[1]["version"]},
                ],
                "target_lot_no": "MERGE-L09B", "business_key": "merge-l09-heavy", "actor": "保管员",
            })
        for child, before in zip(children, available_before):
            assert service.repository.require_lot(child["id"])["available_weight_grams"] == before


def test_merge_success_and_business_key_replay(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, children = _two_compatible_children(service, "L10")
        result = service.lineage.merge_lots({
            "sources": [
                {"lot_id": children[0]["id"], "weight_grams": 120, "expected_version": children[0]["version"]},
                {"lot_id": children[1]["id"], "weight_grams": 80, "expected_version": children[1]["version"]},
            ],
            "target_lot_no": "MERGE-L10", "business_key": "merge-l10-ok", "actor": "保管员", "reason": "回库余量合并",
        })
        assert result["replayed"] is False
        assert result["target"]["initial_weight_grams"] == 200
        assert result["event"]["compatibility"]["harvest_year"] == 2025
        assert {v["lot_no"] for v in result["event"]["compatibility"]["viability"]} == {"SUB-L10-A", "SUB-L10-B"}
        weights = {(e["role"], e["lot_no"]): e["weight_grams"] for e in result["event"]["entries"]}
        assert weights[("source", "SUB-L10-A")] == 120
        assert weights[("source", "SUB-L10-B")] == 80
        assert weights[("product", "MERGE-L10")] == 200
        # 重放：即便版本号已经推进，也返回原谱系，重量只减一次
        replay = service.lineage.merge_lots({
            "sources": [
                {"lot_id": children[0]["id"], "weight_grams": 120, "expected_version": 999},
                {"lot_id": children[1]["id"], "weight_grams": 80, "expected_version": 999},
            ],
            "target_lot_no": "MERGE-L10", "business_key": "merge-l10-ok", "actor": "保管员",
        })
        assert replay["replayed"] is True
        assert replay["event"]["id"] == result["event"]["id"]
        merged_child_a = service.repository.require_lot(children[0]["id"])
        assert merged_child_a["available_weight_grams"] == pytest.approx(200 - 5 - 120)


def test_merge_without_viability_result_is_rejected(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, parent = make_lot(service, "L11")
        result = split(service, parent, [
            {"lot_no": "SUB-L11-A", "weight_grams": 100},
            {"lot_no": "SUB-L11-B", "weight_grams": 100},
        ], "line-split-L11")
        with pytest.raises(ConflictError, match="活力检测结果"):
            service.lineage.merge_lots({
                "sources": [
                    {"lot_id": result["children"][0]["id"], "weight_grams": 50, "expected_version": 1},
                    {"lot_id": result["children"][1]["id"], "weight_grams": 50, "expected_version": 1},
                ],
                "target_lot_no": "MERGE-L11", "business_key": "merge-l11-noviab", "actor": "保管员",
            })


# --------------------------------------------------------------- 溯源与守恒

def test_lineage_ancestors_and_descendants_with_diamond_weights(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, children = _two_compatible_children(service, "L12")
        merged = service.lineage.merge_lots({
            "sources": [
                {"lot_id": children[0]["id"], "weight_grams": 100, "expected_version": children[0]["version"]},
                {"lot_id": children[1]["id"], "weight_grams": 60, "expected_version": children[1]["version"]},
            ],
            "target_lot_no": "MERGE-L12", "business_key": "merge-l12", "actor": "保管员", "reason": "合并",
        })
        merge_id = merged["target"]["id"]
        upward = service.lineage.lot_lineage(merge_id)
        up = {(item["lot_no"], item["depth"]): item["edge_weight_grams"] for item in upward["ancestors"]}
        assert up[("SUB-L12-A", 1)] == 100
        assert up[("SUB-L12-B", 1)] == 60
        # 向上越过合并时按投入占比分摊到祖父：两条路径各 100g/60g（来源被完整取出），合计 160
        assert up[("LOT-L12", 2)] == pytest.approx(160.0)
        assert upward["direct_inbound_grams"] == 160

        parent_id = connection.execute("SELECT id FROM seed_lots WHERE lot_no='LOT-L12'").fetchone()[0]
        downward = service.lineage.lot_lineage(parent_id)
        down = {(item["lot_no"], item["depth"]): item["edge_weight_grams"] for item in downward["descendants"]}
        assert down[("SUB-L12-A", 1)] == 200
        assert down[("SUB-L12-B", 1)] == 200
        # 合并批次只带走两个来源各自实际投入量，合计 160，而不是 400
        assert down[("MERGE-L12", 2)] == pytest.approx(160.0)


def test_conservation_check_flags_tampered_movement(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L13")
        service.inventory.withdraw({
            "lot_id": lot["id"], "quantity_grams": 30, "movement_type": "领用",
            "idempotency_key": "use-l13", "actor": "保管员", "reason": "领用",
        })
        assert service.lineage.conservation_check()["conserved"] is True
        # 直接篡改普通流水（绕过领域逻辑）后，守恒检查必须指出异常
        row = connection.execute(
            "SELECT id FROM lot_movements WHERE idempotency_key='use-l13'"
        ).fetchone()
        connection.execute("UPDATE lot_movements SET quantity_grams=-300 WHERE id=?", (row[0],))
        # 注：普通流水表允许维护，异常由守恒检查暴露
        report = service.lineage.conservation_check(lot["id"])
        assert report["conserved"] is False
        assert report["lot_anomalies"][0]["problems"]


def test_withdrawal_registers_lineage_consumption_and_replay_deducts_once(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L14")
        payload = {
            "lot_id": lot["id"], "quantity_grams": 12, "movement_type": "取样",
            "idempotency_key": "sample-l14", "actor": "检测员", "reason": "抽样",
        }
        first = service.inventory.withdraw(payload)
        assert first["replayed"] is False
        assert first["lineage_event"]["event_type"] == "取样"
        assert first["lineage_event"]["entries"][0]["balance_after_grams"] == 488
        replay = service.inventory.withdraw(payload)
        assert replay["replayed"] is True
        assert service.repository.require_lot(lot["id"])["available_weight_grams"] == 488
        assert connection.execute(
            "SELECT COUNT(*) FROM lineage_events WHERE business_key='sample-l14'"
        ).fetchone()[0] == 1
        lineage = service.lineage.lot_lineage(lot["id"])
        assert lineage["consumed_grams"] == 12
        assert lineage["consumptions"][0]["event_type"] == "取样"
        assert lineage["consumptions"][0]["balance_after_grams"] == 488
        report = service.lineage.conservation_check()
        assert report["conserved"] is True


def test_viability_sampling_flows_into_lineage_ledger(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot = make_lot(service, "L15")
        protocol = rice_protocol(service)
        complete_viability(service, protocol, lot["id"], "VT-L15", [80, 82], sampled_grams=8)
        event = connection.execute(
            "SELECT * FROM lineage_events WHERE business_key=?",
            (f"viability-sample-{connection.execute('SELECT id FROM viability_tests WHERE test_no=?', ('VT-L15',)).fetchone()[0]}",),
        ).fetchone()
        assert event is not None
        entry = connection.execute(
            "SELECT * FROM lineage_ledger_entries WHERE event_id=?", (event["id"],)
        ).fetchone()
        assert entry["movement_type"] == "取样"
        assert entry["weight_grams"] == 8
        assert service.lineage.conservation_check()["conserved"] is True


def test_global_conservation_reports_healthy_when_balanced(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, children = _two_compatible_children(service, "L16")
        service.lineage.merge_lots({
            "sources": [
                {"lot_id": children[0]["id"], "weight_grams": 40, "expected_version": children[0]["version"]},
                {"lot_id": children[1]["id"], "weight_grams": 40, "expected_version": children[1]["version"]},
            ],
            "target_lot_no": "MERGE-L16", "business_key": "merge-l16", "actor": "保管员", "reason": "合并",
        })
        report = service.lineage.conservation_check()
        assert report["conserved"] is True
        assert report["event_anomalies"] == []
        assert report["lots_checked"] >= 4

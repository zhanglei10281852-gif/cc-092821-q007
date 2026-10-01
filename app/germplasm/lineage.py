"""不可变批次谱系账本：分装（split）与合并（merge）。

一次谱系操作在同一事务内完成：
- 分装：按多个目标重量一次性扣减来源批次可用量，并为每个目标建立子批次；
- 合并：校验资源、处理、收获年份、质量冻结与最近活力结果兼容后，扣减多个输入
  批次并建立一个结果批次。
任一目标失败（重量不足、编号冲突、不兼容等）都会抛出让外层事务整体回滚。
谱系事件与组件只追加、不可修改或删除（数据库触发器强制），业务键重试返回原谱系。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.germplasm.repository import GermplasmRepository, record, records

WEIGHT_EPSILON = 1e-6


class LineageService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    # ------------------------------------------------------------------ split
    def split(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.lineage_event_by_key(data["idempotency_key"])
        if previous:
            if previous["event_type"] != "split":
                raise ConflictError("该业务键已用于合并操作，不能作为分装重试")
            return self.event_detail(int(previous["id"]), replayed=True)

        source = self.repository.require_lot(int(data["source_lot_id"]))
        targets = self._validate_targets(data.get("targets", []))
        total = round(sum(float(item["weight_grams"]) for item in targets), 6)

        if source["status"] in {"depleted", "disposed"}:
            raise ConflictError("来源批次已经耗尽或报废，不能分装")
        if self.repository.active_holds(int(source["id"])):
            raise ConflictError("来源批次存在未解除的冻结，不能分装")
        if total > float(source["available_weight_grams"]) + WEIGHT_EPSILON:
            raise ConflictError(
                "来源批次可用重量不足，无法一次性完成分装",
                context={
                    "required_grams": total,
                    "available_grams": source["available_weight_grams"],
                },
            )

        timestamp = to_storage(self.clock.now())
        try:
            event_id = self._insert_event(
                event_type="split",
                event_key=data["idempotency_key"],
                source_lot_id=int(source["id"]),
                result_lot_id=None,
                total=total,
                snapshot={"source_lot_no": source["lot_no"]},
                actor=data["actor"],
                reason=data.get("reason", ""),
                timestamp=timestamp,
            )
            self._insert_component(event_id, "input", int(source["id"]), total, 0)

            # 一次性原子扣减：版本不匹配或扣成负数都判定为版本冲突，由外层回滚。
            updated = self.connection.execute(
                "UPDATE seed_lots SET available_weight_grams=ROUND(available_weight_grams-?,6),"
                "status=CASE WHEN ROUND(available_weight_grams-?,6)<=0 THEN 'depleted' ELSE status END,"
                "version=version+1,updated_at=? WHERE id=? AND version=? AND available_weight_grams+?>=0",
                (total, total, timestamp, source["id"], source["version"], WEIGHT_EPSILON),
            )
            if updated.rowcount != 1:
                raise ConflictError("来源批次版本冲突，分装已取消", context={"current_version": source["version"]})

            for ordinal, target in enumerate(targets, start=1):
                child = self._create_child_lot(source, target, data["actor"], timestamp)
                self._insert_component(event_id, "output", int(child["id"]), float(target["weight_grams"]), ordinal)

            self._insert_movement(
                lot_id=int(source["id"]), movement_type="分装", event_id=event_id, quantity=-total,
                key=f"{data['idempotency_key']}:source", actor=data["actor"],
                reason=f"分装为 {len(targets)} 个子批次：{data.get('reason', '')}".rstrip("："),
                timestamp=timestamp,
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "不可" in message:
                raise ConflictError(f"谱系账本不可变，操作被拒绝：{message}") from exc
            raise ConflictError("分装目标批次编号冲突或谱系状态异常，整次操作回滚") from exc

        return self.event_detail(event_id, replayed=False)

    # ------------------------------------------------------------------ merge
    def merge(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.lineage_event_by_key(data["idempotency_key"])
        if previous:
            if previous["event_type"] != "merge":
                raise ConflictError("该业务键已用于分装操作，不能作为合并重试")
            return self.event_detail(int(previous["id"]), replayed=True)

        inputs = self._validate_inputs(data.get("inputs", []))
        result = self._validate_result(data.get("result"))
        result_weight = round(sum(float(item["weight_grams"]) for item in inputs), 6)

        lots = [self.repository.require_lot(int(item["lot_id"])) for item in inputs]
        self._validate_merge_compatibility(lots)
        for item, lot in zip(inputs, lots):
            if float(item["weight_grams"]) > float(lot["available_weight_grams"]) + WEIGHT_EPSILON:
                raise ConflictError(
                    f"输入批次 {lot['lot_no']} 可用重量不足，合并已取消",
                    context={
                        "required_grams": item["weight_grams"],
                        "available_grams": lot["available_weight_grams"],
                    },
                )

        timestamp = to_storage(self.clock.now())
        try:
            result_lot = self._create_result_lot(lots[0], result, result_weight, timestamp)
            event_id = self._insert_event(
                event_type="merge",
                event_key=data["idempotency_key"],
                source_lot_id=None,
                result_lot_id=int(result_lot["id"]),
                total=result_weight,
                snapshot={"result_lot_no": result_lot["lot_no"]},
                actor=data["actor"],
                reason=data.get("reason", ""),
                timestamp=timestamp,
            )
            self._insert_component(event_id, "output", int(result_lot["id"]), result_weight, 0)

            for ordinal, (item, lot) in enumerate(zip(inputs, lots), start=1):
                weight = float(item["weight_grams"])
                # 乐观版本锁：并发冲突时整次合并回滚，重量不会被扣两次。
                updated = self.connection.execute(
                    "UPDATE seed_lots SET available_weight_grams=ROUND(available_weight_grams-?,6),"
                    "status=CASE WHEN ROUND(available_weight_grams-?,6)<=0 THEN 'depleted' ELSE status END,"
                    "version=version+1,updated_at=? WHERE id=? AND version=? AND available_weight_grams+?>=0",
                    (weight, weight, timestamp, lot["id"], lot["version"], WEIGHT_EPSILON),
                )
                if updated.rowcount != 1:
                    raise ConflictError(
                        f"输入批次 {lot['lot_no']} 版本冲突，合并已取消",
                        context={"current_version": lot["version"]},
                    )
                self._insert_component(event_id, "input", int(lot["id"]), weight, ordinal)
                self._insert_movement(
                    lot_id=int(lot["id"]), movement_type="合并", event_id=event_id, quantity=-weight,
                    key=f"{data['idempotency_key']}:in:{lot['id']}", actor=data["actor"],
                    reason=f"余量合并到 {result_lot['lot_no']}：{data.get('reason', '')}".rstrip("："),
                    timestamp=timestamp,
                )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "不可" in message:
                raise ConflictError(f"谱系账本不可变，操作被拒绝：{message}") from exc
            raise ConflictError("合并结果批次编号冲突或谱系状态异常，整次操作回滚") from exc

        return self.event_detail(event_id, replayed=False)

    def _validate_merge_compatibility(self, lots: list[dict[str, Any]]) -> None:
        accession_ids = {int(lot["accession_id"]) for lot in lots}
        if len(accession_ids) != 1:
            raise ValidationError("只允许合并同一种质资源的批次，跨资源合并被拒绝")
        harvest_years = {int(lot["harvest_year"]) for lot in lots}
        if len(harvest_years) != 1:
            raise ValidationError("收获年份不一致的批次不能合并")
        treatments = {(lot["treatment"] or "").strip() for lot in lots}
        if len(treatments) != 1:
            raise ValidationError("处理条件不一致的批次不能合并")
        for lot in lots:
            if lot["status"] in {"depleted", "disposed"}:
                raise ConflictError(f"批次 {lot['lot_no']} 已经耗尽或报废，不能参与合并")
            if self.repository.active_holds(int(lot["id"])):
                raise ConflictError(f"批次 {lot['lot_no']} 存在未解除的质量冻结，不能参与合并")

        viability_bands: set[str] = set()
        details = []
        for lot in lots:
            latest = self.repository.latest_completed_viability(int(lot["id"]))
            if latest is None or latest["germination_percent"] is None:
                band = "untested"
                details.append({"lot_no": lot["lot_no"], "germination_percent": None, "vigor_index": None})
            else:
                germination = float(latest["germination_percent"])
                band = "high" if germination < 70 else ("medium" if germination < 85 else "low")
                details.append({
                    "lot_no": lot["lot_no"],
                    "germination_percent": latest["germination_percent"],
                    "vigor_index": latest["vigor_index"],
                    "viability_band": band,
                })
            viability_bands.add(band)
        if len(viability_bands) != 1:
            raise ValidationError(
                "各批次最近一次活力结果不在同一风险档（未检测与已检测、高/中/低活力不可混并）",
                context={"lots": details},
            )

    # ---------------------------------------------------------------- queries
    def event_detail(self, event_id: int, *, replayed: bool = False) -> dict[str, Any]:
        event = record(self.connection.execute(
            "SELECT * FROM lot_lineage_events WHERE id=?", (event_id,)
        ).fetchone())
        if event is None:
            raise NotFoundError("谱系事件")
        event["components"] = self.repository.lineage_components(event_id)
        for component in event["components"]:
            component["lot"] = self.repository.require_lot(int(component["lot_id"]))
        event["replayed"] = replayed
        return event

    def lineage_view(self, lot_id: int) -> dict[str, Any]:
        lot = self.repository.require_lot(lot_id)
        ancestors = self.repository.lineage_ancestors(lot_id)
        descendants = self.repository.lineage_descendants(lot_id)
        for event in ancestors + descendants:
            for component in event["components"]:
                component["lot"] = self.repository.require_lot(int(component["lot_id"]))
        return {
            "lot": lot,
            "ancestors": ancestors,
            "descendants": descendants,
            "conservation": self.conservation_check(lot_id),
        }

    def conservation_check(self, lot_id: int) -> dict[str, Any]:
        """用谱系重量守恒与库存流水核对，指出异常事件与流水。"""
        lot = self.repository.require_lot(lot_id)
        anomalies: list[dict[str, Any]] = []

        all_events = self.repository.lineage_ancestors(lot_id) + self.repository.lineage_descendants(lot_id)
        seen: set[int] = set()
        for event in all_events:
            if event["id"] in seen:
                continue
            seen.add(event["id"])
            inputs = [c for c in event["components"] if c["component_role"] == "input"]
            outputs = [c for c in event["components"] if c["component_role"] == "output"]
            input_total = round(sum(float(c["weight_grams"]) for c in inputs), 6)
            output_total = round(sum(float(c["weight_grams"]) for c in outputs), 6)
            if inputs and outputs and abs(input_total - output_total) > WEIGHT_EPSILON:
                anomalies.append({
                    "type": "lineage_event_imbalance",
                    "event_id": event["id"],
                    "event_type": event["event_type"],
                    "input_grams": input_total,
                    "output_grams": output_total,
                    "difference_grams": round(output_total - input_total, 6),
                })

        input_consumed = float(self.connection.execute(
            "SELECT COALESCE(SUM(weight_grams),0) FROM lot_lineage_components "
            "WHERE lot_id=? AND component_role='input'",
            (lot_id,),
        ).fetchone()[0])
        output_rows = records(self.connection.execute(
            "SELECT id AS component_id,weight_grams,event_id FROM lot_lineage_components "
            "WHERE lot_id=? AND component_role='output'",
            (lot_id,),
        ).fetchall())
        # 谱系创建的批次，其输出组件重量必须等于建批初始重量。
        for row in output_rows:
            if abs(float(row["weight_grams"]) - float(lot["initial_weight_grams"])) > WEIGHT_EPSILON:
                anomalies.append({
                    "type": "lineage_origin_weight_mismatch",
                    "event_id": row["event_id"],
                    "component_id": row["component_id"],
                    "initial_grams": lot["initial_weight_grams"],
                    "output_grams": row["weight_grams"],
                })

        non_lineage_delta = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity_grams),0) FROM lot_movements "
            "WHERE lot_id=? AND movement_type IN ('取样','领用','报废','归还','盘点调整')",
            (lot_id,),
        ).fetchone()[0])
        lineage_movement_delta = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity_grams),0) FROM lot_movements "
            "WHERE lot_id=? AND movement_type IN ('分装','合并')",
            (lot_id,),
        ).fetchone()[0])

        recorded = round(float(lot["available_weight_grams"]), 6)
        # 谱系口径：初始重量 - 作为谱系输入被扣减的重量 + 非谱系流水（领用/取样/归还等）。
        expected = round(float(lot["initial_weight_grams"]) - input_consumed + non_lineage_delta, 6)
        ledger_expected = round(
            float(lot["initial_weight_grams"]) + non_lineage_delta + lineage_movement_delta, 6
        )
        if abs(recorded - expected) > WEIGHT_EPSILON:
            anomalies.append({
                "type": "available_weight_drift",
                "recorded_available_grams": recorded,
                "lineage_expected_grams": expected,
                "difference_grams": round(recorded - expected, 6),
            })
        # 谱系输入重量必须与库存流水中的分装/合并扣减一一对应。
        if abs(lineage_movement_delta + input_consumed) > WEIGHT_EPSILON:
            anomalies.append({
                "type": "lineage_movement_mismatch",
                "lineage_input_grams": round(input_consumed, 6),
                "lineage_movement_delta_grams": round(lineage_movement_delta, 6),
                "difference_grams": round(lineage_movement_delta + input_consumed, 6),
            })
        if abs(recorded - ledger_expected) > WEIGHT_EPSILON:
            anomalies.append({
                "type": "movement_ledger_mismatch",
                "recorded_available_grams": recorded,
                "movement_expected_grams": ledger_expected,
                "difference_grams": round(recorded - ledger_expected, 6),
            })

        dangling = records(self.connection.execute(
            "SELECT m.id,m.movement_type,m.quantity_grams FROM lot_movements m "
            "WHERE m.lot_id=? AND m.movement_type IN ('分装','合并') AND m.lineage_event_id IS NULL",
            (lot_id,),
        ).fetchall())
        for item in dangling:
            anomalies.append({"type": "movement_without_lineage_event", **item})

        return {
            "lot_id": lot_id,
            "recorded_available_grams": recorded,
            "lineage_expected_grams": expected,
            "movement_expected_grams": ledger_expected,
            "balanced": not anomalies,
            "anomalies": anomalies,
        }

    # ------------------------------------------------------------- internals
    def _validate_targets(self, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not targets:
            raise ValidationError("分装至少需要一个目标批次")
        normalized: list[dict[str, Any]] = []
        seen_numbers: set[str] = set()
        for target in targets:
            lot_no = str(target["lot_no"]).strip().upper()
            weight = round(float(target["weight_grams"]), 6)
            if weight <= 0:
                raise ValidationError("分装目标重量必须大于零")
            if lot_no in seen_numbers:
                raise ValidationError(f"分装目标批次编号重复：{lot_no}")
            seen_numbers.add(lot_no)
            normalized.append({
                "lot_no": lot_no,
                "weight_grams": weight,
                "moisture_percent": target.get("moisture_percent"),
                "sealed_on": target.get("sealed_on"),
            })
        return normalized

    def _validate_inputs(self, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(inputs) < 2:
            raise ValidationError("合并至少需要两个输入批次")
        normalized: list[dict[str, Any]] = []
        seen_lots: set[int] = set()
        for item in inputs:
            lot_id = int(item["lot_id"])
            weight = round(float(item["weight_grams"]), 6)
            if weight <= 0:
                raise ValidationError("合并输入重量必须大于零")
            if lot_id in seen_lots:
                raise ValidationError("同一批次在一次合并中不能重复出现")
            seen_lots.add(lot_id)
            normalized.append({"lot_id": lot_id, "weight_grams": weight})
        return normalized

    def _validate_result(self, result: dict[str, Any] | None) -> dict[str, Any]:
        if not result or not str(result.get("lot_no", "")).strip():
            raise ValidationError("合并必须提供结果批次编号")
        lot_no = str(result["lot_no"]).strip().upper()
        if len(lot_no) < 3 or len(lot_no) > 60:
            raise ValidationError("结果批次编号长度需在 3 至 60 个字符之间")
        created_by = str(result.get("created_by", "保管员")).strip() or "保管员"
        return {
            "lot_no": lot_no,
            "moisture_percent": result.get("moisture_percent"),
            "sealed_on": result.get("sealed_on"),
            "created_by": created_by,
        }

    def _insert_event(
        self, *, event_type: str, event_key: str, source_lot_id: int | None,
        result_lot_id: int | None, total: float, snapshot: dict[str, Any],
        actor: str, reason: str, timestamp: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO lot_lineage_events(event_key,event_type,source_lot_id,result_lot_id,total_weight_grams,"
            "snapshot_json,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                event_key, event_type, source_lot_id, result_lot_id, total,
                json.dumps(snapshot, ensure_ascii=False), actor, reason, timestamp,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_component(
        self, event_id: int, role: str, lot_id: int, weight: float, ordinal: int,
    ) -> None:
        self.connection.execute(
            "INSERT INTO lot_lineage_components(event_id,component_role,lot_id,weight_grams,ordinal) VALUES(?,?,?,?,?)",
            (event_id, role, lot_id, weight, ordinal),
        )

    def _insert_movement(
        self, *, lot_id: int, movement_type: str, event_id: int, quantity: float, key: str,
        actor: str, reason: str, timestamp: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,lineage_event_id,idempotency_key,"
            "actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (lot_id, movement_type, quantity, event_id, key, actor, reason, timestamp),
        )

    def _create_child_lot(
        self, source: dict[str, Any], target: dict[str, Any], actor: str, timestamp: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO seed_lots(lot_no,accession_id,parent_lot_id,harvest_year,initial_weight_grams,"
            "available_weight_grams,moisture_percent,treatment,sealed_on,status,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (
                target["lot_no"], source["accession_id"], source["id"], source["harvest_year"],
                target["weight_grams"], target["weight_grams"],
                target.get("moisture_percent") if target.get("moisture_percent") is not None else source["moisture_percent"],
                source["treatment"], target.get("sealed_on") or source["sealed_on"],
                actor, timestamp, timestamp,
            ),
        )
        return self.repository.require_lot(int(cursor.lastrowid))

    def _create_result_lot(
        self, first: dict[str, Any], result: dict[str, Any], weight: float, timestamp: str,
    ) -> dict[str, Any]:
        # parent_lot_id 只能记录单一父代，多输入合并的完整来源由谱系组件账本承载。
        cursor = self.connection.execute(
            "INSERT INTO seed_lots(lot_no,accession_id,parent_lot_id,harvest_year,initial_weight_grams,"
            "available_weight_grams,moisture_percent,treatment,sealed_on,status,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (
                str(result["lot_no"]).strip().upper(), first["accession_id"], first["id"], first["harvest_year"],
                weight, weight, result.get("moisture_percent"), first["treatment"], result.get("sealed_on"),
                str(result.get("created_by", "保管员")), timestamp, timestamp,
            ),
        )
        return self.repository.require_lot(int(cursor.lastrowid))

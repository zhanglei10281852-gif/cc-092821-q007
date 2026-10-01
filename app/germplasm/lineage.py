from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import transaction as db_transaction
from app.germplasm.repository import GermplasmRepository, record, records

# 最近活力结果的兼容档位：相邻档位可以合并，跌破 50% 一律拒绝
GERMINATION_BANDS = ((85.0, "high"), (70.0, "medium"), (50.0, "low"))
CONSUMPTION_TYPES = {"取样", "领用", "报废"}
MAX_GRAPH_DEPTH = 100
WEIGHT_EPSILON = 1e-6


class LineageService:
    """不可变批次谱系账本：分装、合并、消耗登记、上下溯源与守恒检查。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    # ------------------------------------------------------------------ 分装

    def split_lot(self, data: dict[str, Any]) -> dict[str, Any]:
        key = str(data["business_key"])
        replay = self.event_by_key(key)
        if replay:
            return {"event": replay, "replayed": True}
        # 保存点保证：任一目标失败时，整次分装（含来源扣减）全部回滚
        with db_transaction():
            targets = self._validate_targets(data.get("targets"))
            source = self.repository.require_lot(int(data["source_lot_id"]))
            expected_version = int(data["expected_version"])
            if source["status"] in {"depleted", "disposed"}:
                raise ConflictError("耗尽或报废批次不能分装")
            holds = self.repository.active_holds(int(source["id"]))
            if holds:
                raise ConflictError("批次存在未解除的冻结，不能分装", context={"holds": [item["id"] for item in holds]})
            total = round(sum(item["weight_grams"] for item in targets), 6)
            if total > float(source["available_weight_grams"]) + WEIGHT_EPSILON:
                raise ConflictError(
                    "分装总重量超过来源批次可用重量",
                    context={"requested_grams": total, "available_grams": source["available_weight_grams"]},
                )
            for item in targets:
                if self.connection.execute("SELECT 1 FROM seed_lots WHERE lot_no=?", (item["lot_no"],)).fetchone():
                    raise ConflictError("子批次编号已经存在", context={"lot_no": item["lot_no"]})
            timestamp = to_storage(self.clock.now())
            remaining = round(float(source["available_weight_grams"]) - total, 6)
            status = "depleted" if remaining <= WEIGHT_EPSILON else source["status"]
            # 条件更新同时校验版本与余额：版本冲突或余额被并发占用时 rowcount=0，重量不会变化
            updated = self.connection.execute(
                "UPDATE seed_lots SET available_weight_grams=?,status=?,version=version+1,updated_at=? "
                "WHERE id=? AND version=? AND available_weight_grams>=?",
                (remaining, status, timestamp, source["id"], expected_version, total - WEIGHT_EPSILON),
            )
            if updated.rowcount != 1:
                current = self.repository.require_lot(int(source["id"]))
                raise ConflictError(
                    "来源批次版本冲突或可用重量不足，分装已整体取消",
                    context={"current_version": current["version"], "available_grams": current["available_weight_grams"]},
                )
            event_id = self._insert_event("分装", key, data, timestamp)
            self.connection.execute(
                "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,idempotency_key,actor,reason,created_at) "
                "VALUES(?, '分装', ?, ?, ?, ?, ?)",
                (source["id"], -total, self._movement_key(key, int(source["id"])), data["actor"], data.get("reason", ""), timestamp),
            )
            self._insert_entry(event_id, 0, int(source["id"]), "source", None, "分装", total, remaining,
                              self._movement_key(key, int(source["id"])), None, data, timestamp)
            children: list[dict[str, Any]] = []
            for seq, item in enumerate(targets, start=1):
                cursor = self.connection.execute(
                    "INSERT INTO seed_lots(lot_no,accession_id,parent_lot_id,harvest_year,initial_weight_grams,"
                    "available_weight_grams,moisture_percent,treatment,sealed_on,status,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                    (
                        item["lot_no"], source["accession_id"], source["id"], source["harvest_year"],
                        item["weight_grams"], item["weight_grams"],
                        item.get("moisture_percent", source["moisture_percent"]),
                        source["treatment"], item.get("sealed_on"), data["actor"], timestamp, timestamp,
                    ),
                )
                child_id = int(cursor.lastrowid)
                self._insert_entry(event_id, seq, child_id, "product", int(source["id"]), "分装",
                                   item["weight_grams"], item["weight_grams"], None, None, data, timestamp)
                children.append(self.repository.require_lot(child_id))
            return {"event": self.require_event(event_id), "children": children, "replayed": False}

    # ------------------------------------------------------------------ 合并

    def merge_lots(self, data: dict[str, Any]) -> dict[str, Any]:
        key = str(data["business_key"])
        replay = self.event_by_key(key)
        if replay:
            return {"event": replay, "replayed": True}
        # 保存点保证：任一来源扣减失败时，此前来源的扣减与全部写入整批回滚
        with db_transaction():
            sources = self._validate_merge_sources(data.get("sources"))
            target_lot_no = str(data["target_lot_no"]).strip().upper()
            if not target_lot_no:
                raise ValidationError("合并目标批次编号不能为空")
            if self.connection.execute("SELECT 1 FROM seed_lots WHERE lot_no=?", (target_lot_no,)).fetchone():
                raise ConflictError("合并目标批次编号已经存在", context={"lot_no": target_lot_no})
            lots = [self.repository.require_lot(int(item["lot_id"])) for item in sources]
            compatibility = self._check_compatibility(lots)
            timestamp = to_storage(self.clock.now())
            # 先对每个来源做条件扣减；任一来源版本冲突或余量不足，整次合并回滚
            for item, lot in zip(sources, lots):
                weight = round(float(item["weight_grams"]), 6)
                updated = self.connection.execute(
                    "UPDATE seed_lots SET available_weight_grams=available_weight_grams-?,"
                    "status=CASE WHEN available_weight_grams-?<=0 THEN 'depleted' ELSE status END,"
                    "version=version+1,updated_at=? WHERE id=? AND version=? AND available_weight_grams>=?",
                    (weight, weight, timestamp, lot["id"], int(item["expected_version"]), weight - WEIGHT_EPSILON),
                )
                if updated.rowcount != 1:
                    current = self.repository.require_lot(int(lot["id"]))
                    raise ConflictError(
                        f"来源批次 {current['lot_no']} 版本冲突或可用重量不足，合并已整体取消",
                        context={"lot_id": current["id"], "current_version": current["version"],
                                 "available_grams": current["available_weight_grams"]},
                    )
            total = round(sum(float(item["weight_grams"]) for item in sources), 6)
            primary = min(lots, key=lambda item: int(item["id"]))
            cursor = self.connection.execute(
                "INSERT INTO seed_lots(lot_no,accession_id,parent_lot_id,harvest_year,initial_weight_grams,"
                "available_weight_grams,moisture_percent,treatment,sealed_on,status,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    target_lot_no, primary["accession_id"], primary["id"], primary["harvest_year"],
                    total, total, data.get("moisture_percent", primary["moisture_percent"]),
                    primary["treatment"], None, data["actor"], timestamp, timestamp,
                ),
            )
            target_id = int(cursor.lastrowid)
            event_id = self._insert_event("合并", key, data, timestamp, compatibility=compatibility)
            for seq, (item, lot) in enumerate(zip(sources, lots), start=0):
                weight = round(float(item["weight_grams"]), 6)
                remaining = self.repository.require_lot(int(lot["id"]))["available_weight_grams"]
                movement_key = self._movement_key(key, int(lot["id"]))
                self.connection.execute(
                    "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,idempotency_key,actor,reason,created_at) "
                    "VALUES(?, '合并', ?, ?, ?, ?, ?)",
                    (lot["id"], -weight, movement_key, data["actor"], data.get("reason", ""), timestamp),
                )
                self._insert_entry(event_id, seq, int(lot["id"]), "source", None, "合并", weight,
                                   float(remaining), movement_key, None, data, timestamp)
            self._insert_entry(event_id, len(sources), target_id, "product", int(primary["id"]), "合并",
                               total, total, None, None, data, timestamp)
            return {"event": self.require_event(event_id), "target": self.repository.require_lot(target_id), "replayed": False}

    # ------------------------------------------------- 消耗（领用/取样/报废）

    def record_consumption(
        self,
        *,
        lot_id: int,
        weight_grams: float,
        movement_type: str,
        business_key: str,
        actor: str,
        reason: str,
        balance_after_grams: float,
        placement_id: int | None = None,
    ) -> dict[str, Any]:
        if movement_type not in CONSUMPTION_TYPES:
            raise ValidationError("谱系消耗事件类型无效")
        replay = self.event_by_key(business_key)
        if replay:
            return replay
        timestamp = to_storage(self.clock.now())
        event_id = self._insert_event(
            movement_type, business_key,
            {"actor": actor, "reason": reason}, timestamp,
        )
        self._insert_entry(event_id, 0, lot_id, "consumption", None, movement_type,
                           round(float(weight_grams), 6), round(float(balance_after_grams), 6),
                           business_key, placement_id,
                           {"actor": actor, "reason": reason}, timestamp)
        return self.require_event(event_id)

    # ------------------------------------------------------------------ 查询

    def event_by_key(self, business_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM lineage_events WHERE business_key=?", (business_key,)
        ).fetchone()
        return self._event_detail(record(row)) if row else None

    def require_event(self, event_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM lineage_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise NotFoundError("谱系事件不存在")
        return self._event_detail(record(row))

    def lot_lineage(self, lot_id: int) -> dict[str, Any]:
        lot = self.repository.require_lot(lot_id)
        # 向上溯源：跨合并按来源占比分摊，跨分装沿路径取较小值，避免钻石结构重复计重
        ancestor_rows = self.connection.execute(
            """
            WITH RECURSIVE up(depth,lot_id,event_id,weight_grams) AS (
                SELECT 0, ?, NULL, 0.0
                UNION ALL
                SELECT up.depth + 1, s.lot_id, p.event_id,
                       CASE WHEN up.depth = 0 THEN
                                CASE ev.event_type WHEN '合并' THEN s.weight_grams ELSE p.weight_grams END
                            WHEN ev.event_type = '合并' THEN
                                ROUND(s.weight_grams * up.weight_grams / p.weight_grams, 6)
                            ELSE MIN(up.weight_grams, p.weight_grams)
                       END
                FROM up
                JOIN lineage_ledger_entries p ON p.lot_id = up.lot_id AND p.role = 'product'
                JOIN lineage_ledger_entries s ON s.event_id = p.event_id AND s.role = 'source'
                JOIN lineage_events ev ON ev.id = p.event_id
                WHERE up.depth < ?
            )
            SELECT depth,lot_id,event_id,weight_grams FROM up WHERE depth > 0
            """,
            (lot_id, MAX_GRAPH_DEPTH),
        ).fetchall()
        # 向下汇总去向：合并来源只带走其实际投入量，跨分装沿路径取较小值
        descendant_rows = self.connection.execute(
            """
            WITH RECURSIVE down(depth,lot_id,event_id,weight_grams) AS (
                SELECT 0, ?, NULL, 0.0
                UNION ALL
                SELECT down.depth + 1, p.lot_id, s.event_id,
                       CASE WHEN down.depth = 0 THEN
                                CASE ev.event_type WHEN '合并' THEN s.weight_grams ELSE p.weight_grams END
                            WHEN ev.event_type = '合并' THEN MIN(down.weight_grams, s.weight_grams)
                            ELSE MIN(down.weight_grams, p.weight_grams)
                       END
                FROM down
                JOIN lineage_ledger_entries s ON s.lot_id = down.lot_id AND s.role = 'source'
                JOIN lineage_ledger_entries p ON p.event_id = s.event_id AND p.role = 'product'
                JOIN lineage_events ev ON ev.id = s.event_id
                WHERE down.depth < ?
            )
            SELECT depth,lot_id,event_id,weight_grams FROM down WHERE depth > 0
            """,
            (lot_id, MAX_GRAPH_DEPTH),
        ).fetchall()

        def assemble(rows: Any) -> list[dict[str, Any]]:
            nodes: dict[int, dict[str, Any]] = {}
            for row in rows:
                node_lot = self.repository.require_lot(int(row["lot_id"]))
                key = int(row["lot_id"])
                depth = int(row["depth"])
                weight = round(float(row["weight_grams"]), 6)
                if key not in nodes:
                    nodes[key] = {
                        "lot_id": node_lot["id"],
                        "lot_no": node_lot["lot_no"],
                        "accession_id": node_lot["accession_id"],
                        "status": node_lot["status"],
                        "depth": depth,
                        "via_event_id": row["event_id"],
                        "edge_weight_grams": weight,
                        "path_count": 1,
                    }
                else:
                    # 同一批次可经多条路径到达（如钻石结构），重量按路径累加
                    nodes[key]["edge_weight_grams"] = round(nodes[key]["edge_weight_grams"] + weight, 6)
                    nodes[key]["path_count"] += 1
                    if depth < nodes[key]["depth"]:
                        nodes[key]["depth"] = depth
                        nodes[key]["via_event_id"] = row["event_id"]
            return sorted(nodes.values(), key=lambda item: (item["depth"], item["lot_no"]))

        ancestors = assemble(ancestor_rows)
        descendants = assemble(descendant_rows)
        consumptions = records(self.connection.execute(
            "SELECT ev.id AS event_id, ev.event_type, ev.business_key, e.weight_grams, "
            "e.balance_after_grams, e.actor, e.reason, e.occurred_at "
            "FROM lineage_ledger_entries e JOIN lineage_events ev ON ev.id=e.event_id "
            "WHERE e.lot_id=? AND e.role='consumption' ORDER BY ev.id",
            (lot_id,),
        ).fetchall())
        return {
            "lot": {key: lot[key] for key in (
                "id", "lot_no", "accession_id", "parent_lot_id", "harvest_year", "treatment",
                "initial_weight_grams", "available_weight_grams", "status", "version",
            )},
            "ancestors": ancestors,
            "descendants": descendants,
            "consumptions": consumptions,
            "direct_outbound_grams": round(
                sum(item["edge_weight_grams"] for item in descendants if item["depth"] == 1), 6
            ),
            "consumed_grams": round(sum(float(item["weight_grams"]) for item in consumptions), 6),
            "direct_inbound_grams": round(
                sum(item["edge_weight_grams"] for item in ancestors if item["depth"] == 1), 6
            ),
        }

    def conservation_check(self, lot_id: int | None = None) -> dict[str, Any]:
        where = "WHERE l.id=?" if lot_id is not None else ""
        params: tuple[Any, ...] = (lot_id,) if lot_id is not None else ()
        if lot_id is not None:
            self.repository.require_lot(lot_id)
        rows = self.connection.execute(
            f"""
            SELECT l.id AS lot_id, l.lot_no, l.initial_weight_grams, l.available_weight_grams,
                   COALESCE(SUM(CASE WHEN e.role='source' THEN e.weight_grams ELSE 0 END),0) AS split_out,
                   COALESCE(SUM(CASE WHEN e.role='consumption' THEN e.weight_grams ELSE 0 END),0) AS consumed,
                   COALESCE(SUM(CASE WHEN e.role='product' THEN e.weight_grams ELSE 0 END),0) AS produced
            FROM seed_lots l LEFT JOIN lineage_ledger_entries e ON e.lot_id=l.id
            {where}
            GROUP BY l.id
            """,
            params,
        ).fetchall()
        anomalies: list[dict[str, Any]] = []
        for row in rows:
            initial = float(row["initial_weight_grams"])
            ledger_out = round(float(row["split_out"]) + float(row["consumed"]), 6)
            expected = round(initial - ledger_out, 6)
            recorded = round(float(row["available_weight_grams"]), 6)
            produced = round(float(row["produced"]), 6)
            problems: list[str] = []
            if abs(expected - recorded) > WEIGHT_EPSILON:
                problems.append("账面可用重量与谱系收支不守恒")
            if produced > WEIGHT_EPSILON and abs(produced - initial) > WEIGHT_EPSILON:
                problems.append("衍生批次初始重量与谱系入账重量不一致")
            movement_total = float(self.connection.execute(
                "SELECT COALESCE(SUM(quantity_grams),0) FROM lot_movements WHERE lot_id=? "
                "AND movement_type IN ('取样','领用','报废','归还','盘点调整','分装','合并')",
                (row["lot_id"],),
            ).fetchone()[0])
            movements_expected = round(initial + movement_total, 6)
            if abs(movements_expected - recorded) > WEIGHT_EPSILON:
                problems.append("普通库存流水合计与账面可用重量不一致")
            if problems:
                anomalies.append({
                    "lot_id": row["lot_id"], "lot_no": row["lot_no"],
                    "recorded_available_grams": recorded,
                    "ledger_expected_grams": expected,
                    "movements_expected_grams": movements_expected,
                    "initial_grams": initial, "split_out_grams": round(float(row["split_out"]), 6),
                    "consumed_grams": round(float(row["consumed"]), 6),
                    "produced_grams": produced,
                    "problems": problems,
                })
        event_mismatches = self.connection.execute(
            """
            SELECT ev.id AS event_id, ev.event_type, ev.business_key,
                   SUM(CASE WHEN e.role='source' THEN e.weight_grams ELSE 0 END) AS source_weight,
                   SUM(CASE WHEN e.role='product' THEN e.weight_grams ELSE 0 END) AS product_weight
            FROM lineage_events ev JOIN lineage_ledger_entries e ON e.event_id=ev.id
            WHERE ev.event_type IN ('分装','合并')
            GROUP BY ev.id
            HAVING ABS(source_weight-product_weight) > ?
            """,
            (WEIGHT_EPSILON,),
        ).fetchall()
        missing_movements = self.connection.execute(
            """
            SELECT m.id AS movement_id, m.lot_id, m.movement_type, m.quantity_grams, m.idempotency_key
            FROM lot_movements m
            WHERE m.movement_type IN ('取样','领用','报废','分装','合并')
              AND NOT EXISTS (SELECT 1 FROM lineage_ledger_entries e WHERE e.idempotency_key=m.idempotency_key)
            """
            + (" AND m.lot_id=?" if lot_id is not None else ""),
            params,
        ).fetchall()
        return {
            "scope_lot_id": lot_id,
            "lots_checked": len(rows),
            "conserved": not anomalies and not event_mismatches and not missing_movements,
            "lot_anomalies": anomalies,
            "event_anomalies": records(event_mismatches),
            "movements_outside_ledger": records(missing_movements),
        }

    # ------------------------------------------------------------------ 内部

    def _check_compatibility(self, lots: list[dict[str, Any]]) -> dict[str, Any]:
        if len(lots) < 2:
            raise ValidationError("合并至少需要两个来源批次")
        accession_ids = {int(lot["accession_id"]) for lot in lots}
        if len(accession_ids) != 1:
            raise ConflictError("来源批次不属于同一资源，禁止跨资源合并", context={
                "accession_ids": sorted(accession_ids)
            })
        treatments = {lot["treatment"] for lot in lots}
        if len(treatments) != 1:
            raise ConflictError("来源批次处理条件不一致，不能合并", context={"treatments": sorted(treatments)})
        years = {int(lot["harvest_year"]) for lot in lots}
        if len(years) != 1:
            raise ConflictError("来源批次收获年份不一致，不能合并", context={"harvest_years": sorted(years)})
        for lot in lots:
            if lot["status"] in {"depleted", "disposed"}:
                raise ConflictError(f"批次 {lot['lot_no']} 已耗尽或报废，不能参与合并")
            holds = self.repository.active_holds(int(lot["id"]))
            if holds:
                raise ConflictError(f"批次 {lot['lot_no']} 存在未解除的冻结，不能合并",
                                    context={"lot_id": lot["id"], "holds": [item["id"] for item in holds]})
        viability: list[dict[str, Any]] = []
        bands: list[int] = []
        for lot in lots:
            latest = self.connection.execute(
                "SELECT id,test_no,germination_percent,completed_at FROM viability_tests "
                "WHERE lot_id=? AND status='completed' ORDER BY completed_at DESC,id DESC LIMIT 1",
                (lot["id"],),
            ).fetchone()
            if not latest:
                raise ConflictError(f"批次 {lot['lot_no']} 没有已完成的活力检测结果，无法校验活力兼容性")
            germination = float(latest["germination_percent"])
            band = self.germination_band(germination)
            if band == 0:
                raise ConflictError(
                    f"批次 {lot['lot_no']} 最近活力 {germination:.2f}% 已低于安全下限，不能合并",
                    context={"lot_id": lot["id"], "germination_percent": germination},
                )
            bands.append(band)
            viability.append({
                "lot_id": lot["id"], "lot_no": lot["lot_no"], "test_id": latest["id"],
                "test_no": latest["test_no"], "germination_percent": germination, "band": band,
            })
        if max(bands) - min(bands) > 1:
            raise ConflictError("来源批次最近活力档位相差超过一级，不能合并", context={"viability": viability})
        accession = self.repository.require_accession(int(lots[0]["accession_id"]))
        return {
            "accession_id": accession["id"], "accession_no": accession["accession_no"],
            "treatment": lots[0]["treatment"], "harvest_year": int(lots[0]["harvest_year"]),
            "viability": viability,
        }

    @staticmethod
    def germination_band(germination: float) -> int:
        if germination >= GERMINATION_BANDS[0][0]:
            return 3
        if germination >= GERMINATION_BANDS[1][0]:
            return 2
        if germination >= GERMINATION_BANDS[2][0]:
            return 1
        return 0

    def _validate_targets(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("分装至少需要一个目标容器")
        if len(raw) > 100:
            raise ValidationError("一次分装的目标容器不能超过 100 个")
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            lot_no = str(item["lot_no"]).strip().upper()
            weight = round(float(item["weight_grams"]), 6)
            if weight <= 0:
                raise ValidationError("分装目标重量必须大于 0")
            if lot_no in seen:
                raise ConflictError("分装请求中存在重复的子批次编号", context={"lot_no": lot_no})
            seen.add(lot_no)
            targets.append({
                "lot_no": lot_no, "weight_grams": weight,
                "moisture_percent": item.get("moisture_percent"),
                "sealed_on": item.get("sealed_on"),
            })
        return targets

    def _validate_merge_sources(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or len(raw) < 2:
            raise ValidationError("合并至少需要两个来源批次")
        if len(raw) > 100:
            raise ValidationError("一次合并的来源批次不能超过 100 个")
        sources: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in raw:
            lot_id = int(item["lot_id"])
            weight = round(float(item["weight_grams"]), 6)
            if weight <= 0:
                raise ValidationError("合并来源重量必须大于 0")
            if lot_id in seen:
                raise ConflictError("合并请求中存在重复的来源批次", context={"lot_id": lot_id})
            seen.add(lot_id)
            sources.append({"lot_id": lot_id, "weight_grams": weight,
                            "expected_version": int(item["expected_version"])})
        return sources

    def _insert_event(
        self, event_type: str, key: str, data: dict[str, Any], timestamp: str,
        compatibility: dict[str, Any] | None = None,
    ) -> int:
        try:
            cursor = self.connection.execute(
                "INSERT INTO lineage_events(event_type,business_key,actor,reason,compatibility_json,occurred_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    event_type, key, str(data.get("actor", ""))[:100], str(data.get("reason", ""))[:500],
                    json.dumps(compatibility or {}, ensure_ascii=False), timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 并发下业务键被抢先提交：不重复记账，交由重放读取原谱系
            existing = self.event_by_key(key)
            if existing:
                raise ConflictError("谱系业务键冲突，已存在原谱系事件", context={"event_id": existing["id"]}) from exc
            raise ConflictError("谱系事件写入失败") from exc
        return int(cursor.lastrowid)

    def _insert_entry(
        self, event_id: int, seq: int, lot_id: int, role: str, other_lot_id: int | None,
        movement_type: str, weight_grams: float, balance_after_grams: float,
        idempotency_key: str | None, placement_id: int | None,
        data: dict[str, Any], timestamp: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO lineage_ledger_entries(event_id,seq,lot_id,role,other_lot_id,movement_type,weight_grams,"
            "placement_id,balance_after_grams,idempotency_key,actor,reason,occurred_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, seq, lot_id, role, other_lot_id, movement_type, round(weight_grams, 6),
                placement_id, round(balance_after_grams, 6), idempotency_key,
                str(data.get("actor", ""))[:100], str(data.get("reason", ""))[:500], timestamp,
            ),
        )

    @staticmethod
    def _movement_key(business_key: str, lot_id: int) -> str:
        return f"{business_key}:source:{lot_id}"

    def _event_detail(self, event: dict[str, Any] | None) -> dict[str, Any] | None:
        if event is None:
            return None
        event["compatibility"] = json.loads(event.pop("compatibility_json") or "{}")
        event["entries"] = records(self.connection.execute(
            "SELECT e.*, l.lot_no, o.lot_no AS other_lot_no FROM lineage_ledger_entries e "
            "JOIN seed_lots l ON l.id=e.lot_id LEFT JOIN seed_lots o ON o.id=e.other_lot_id "
            "WHERE e.event_id=? ORDER BY e.seq",
            (event["id"],),
        ).fetchall())
        return event

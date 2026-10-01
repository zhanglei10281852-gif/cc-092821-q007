from __future__ import annotations


def test_http_intake_and_inventory_flow(client, admin):
    headers = admin["headers"]
    source = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": "HTTP-SRC-1", "provider_name": "合作站", "country_code": "CN", "locality": "北方站",
        "restrictions": {},
    })
    assert source.status_code == 201, source.text
    accession = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": "HTTP-ACC-1", "scientific_name": "Triticum aestivum", "crop_name": "小麦",
        "cultivar_name": "地方材料", "source_id": source.json()["id"], "acquisition_type": "交换",
        "received_on": "2026-09-20", "passport": {}, "created_by": "登记员",
    })
    assert accession.status_code == 201, accession.text
    accepted = client.post(f"/api/germplasm/accessions/{accession.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    assert accepted.status_code == 200, accepted.text
    location = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": "HTTP-L1", "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": 3000, "temperature_c": 4, "humidity_percent": 35,
    })
    assert location.status_code == 201, location.text
    lot = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": "HTTP-LOT-1", "accession_id": accepted.json()["id"], "harvest_year": 2025,
        "initial_weight_grams": 800, "moisture_percent": 8, "treatment": "清选", "created_by": "登记员",
    })
    assert lot.status_code == 201, lot.text
    placed = client.post("/api/germplasm/placements", headers=headers, json={
        "lot_id": lot.json()["id"], "location_id": location.json()["id"], "weight_grams": 800,
        "container_code": "HTTP-BOX-1", "idempotency_key": "http-place-0001", "actor": "保管员",
    })
    assert placed.status_code == 201, placed.text
    detail = client.get(f"/api/germplasm/lots/{lot.json()['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "stored"
    assert detail.json()["placements"][0]["container_code"] == "HTTP-BOX-1"


def test_api_rejects_unauthenticated_business_request(client):
    response = client.get("/api/germplasm/dashboard")
    assert response.status_code == 401


def test_api_validation_error_has_structured_body(client, admin):
    response = client.post("/api/germplasm/locations", headers=admin["headers"], json={
        "location_code": "BAD", "facility": "库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": -1, "temperature_c": 4, "humidity_percent": 35,
    })
    assert response.status_code == 422
    assert response.json()["detail"]


def test_api_lineage_split_replay_and_conservation(client, admin):
    headers = admin["headers"]
    source = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": "LN-SRC-1", "provider_name": "交换站", "country_code": "CN", "restrictions": {},
    })
    accession = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": "LN-ACC-1", "scientific_name": "Zea mays", "crop_name": "玉米",
        "source_id": source.json()["id"], "acquisition_type": "交换",
        "received_on": "2026-09-20", "created_by": "登记员",
    })
    client.post(f"/api/germplasm/accessions/{accession.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    lot = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": "LN-LOT-1", "accession_id": accession.json()["id"], "harvest_year": 2025,
        "initial_weight_grams": 500, "treatment": "干燥", "created_by": "登记员",
    }).json()
    split_body = {
        "source_lot_id": lot["id"], "expected_version": 1,
        "targets": [
            {"lot_no": "LN-SUB-A", "weight_grams": 200},
            {"lot_no": "LN-SUB-B", "weight_grams": 180},
        ],
        "business_key": "api-split-0001", "actor": "保管员", "reason": "交换备份",
    }
    first = client.post("/api/germplasm/lineage/splits", headers=headers, json=split_body)
    assert first.status_code == 201, first.text
    assert [c["lot_no"] for c in first.json()["children"]] == ["LN-SUB-A", "LN-SUB-B"]
    # 业务键重试返回原谱系，重量只扣一次
    replay = client.post("/api/germplasm/lineage/splits", headers=headers, json=split_body)
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["event"]["id"] == first.json()["event"]["id"]
    detail = client.get(f"/api/germplasm/lots/{lot['id']}", headers=headers).json()
    assert detail["available_weight_grams"] == 120
    # 版本冲突不会扣减
    conflict = client.post("/api/germplasm/lineage/splits", headers=headers, json={
        "source_lot_id": lot["id"], "expected_version": 99,
        "targets": [{"lot_no": "LN-SUB-C", "weight_grams": 10}],
        "business_key": "api-split-conflict", "actor": "保管员",
    })
    assert conflict.status_code == 409
    # 向上溯源
    lineage = client.get(f"/api/germplasm/lots/{first.json()['children'][0]['id']}/lineage", headers=headers)
    assert lineage.status_code == 200
    assert [a["lot_no"] for a in lineage.json()["ancestors"]] == ["LN-LOT-1"]
    assert lineage.json()["direct_inbound_grams"] == 200
    # 守恒检查
    conservation = client.get("/api/germplasm/lineage/conservation", headers=headers)
    assert conservation.status_code == 200
    assert conservation.json()["conserved"] is True


def test_api_lineage_merge_rejects_cross_resource(client, admin):
    headers = admin["headers"]

    def accepted_accession(no, crop, src_no):
        source = client.post("/api/germplasm/sources", headers=headers, json={
            "source_code": src_no, "provider_name": "站", "country_code": "CN", "restrictions": {},
        })
        acc = client.post("/api/germplasm/accessions", headers=headers, json={
            "accession_no": no, "scientific_name": "Specimen", "crop_name": crop,
            "source_id": source.json()["id"], "acquisition_type": "采集",
            "received_on": "2026-09-20", "created_by": "登记员",
        })
        client.post(f"/api/germplasm/accessions/{acc.json()['id']}/transition", headers=headers, json={
            "target_status": "accepted", "reason": "ok", "expected_version": 1, "actor": "审核员",
        })
        return acc.json()["id"]

    def new_lot(lot_no, accession_id):
        return client.post("/api/germplasm/lots", headers=headers, json={
            "lot_no": lot_no, "accession_id": accession_id, "harvest_year": 2025,
            "initial_weight_grams": 100, "treatment": "干燥", "created_by": "登记员",
        }).json()

    first = new_lot("XM-LOT-1", accepted_accession("XM-ACC-1", "水稻", "XM-S-1"))
    second = new_lot("XM-LOT-2", accepted_accession("XM-ACC-2", "小麦", "XM-S-2"))
    response = client.post("/api/germplasm/lineage/merges", headers=headers, json={
        "sources": [
            {"lot_id": first["id"], "weight_grams": 50, "expected_version": 1},
            {"lot_id": second["id"], "weight_grams": 50, "expected_version": 1},
        ],
        "target_lot_no": "XM-MERGE", "business_key": "api-merge-cross", "actor": "保管员",
    })
    assert response.status_code == 409
    assert "同一资源" in response.json()["error"]["message"]


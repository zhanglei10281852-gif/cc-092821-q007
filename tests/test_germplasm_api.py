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

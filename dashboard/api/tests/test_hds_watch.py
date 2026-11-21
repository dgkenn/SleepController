"""Apple Watch live heart rate over Wi-Fi via the Health Data Server watch app."""


def test_a_heart_rate_reading_is_ingested(auth_client):
    r = auth_client.put("/hds", json={"data": "heartRate:61"})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True


def test_other_data_types_are_ignored(auth_client):
    assert auth_client.put("/hds", json={"data": "calories:12"}).json()["ignored"] == "calories"


def test_a_malformed_reading_is_a_400(auth_client):
    assert auth_client.put("/hds", json={"data": "nonsense"}).status_code == 400
    assert auth_client.put("/hds", json={"data": "heartRate:abc"}).status_code == 400


def test_the_reading_is_stored_as_the_apple_watch(auth_client):
    from app.db import get_repo
    auth_client.put("/hds", json={"data": "heartRate:63"})
    row = get_repo().conn.execute(
        "SELECT hr, source FROM sensor_samples ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == 63 and row[1] == "apple_watch"

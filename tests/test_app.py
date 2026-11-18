"""端到端流程 + 整趟替换 + 预览/确认一致性 + 并发确认测试。"""
import io
import re
import sqlite3
import threading
import time

import pytest

from app import create_app, storage
from app.parser import parse_handling_csv, parse_temperature_csv

TEMP_CSV = (
    "timestamp_utc,celsius\n"
    "2026-11-18T08:00:00Z,5.0\n"
    "2026-11-18T08:05:00Z,6.0\n"
    "2026-11-18T08:10:00Z,9.0\n"
    "2026-11-18T08:15:00Z,7.0\n"
    "2026-11-18T08:40:00Z,6.0\n"
    "2026-11-18T08:45:00Z,4.0\n"
).encode()

HANDLING_CSV = (
    "box,loaded_at,unloaded_at\n"
    "BOX-1,2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
    "BOX-2,2026-11-18T08:05:00Z,2026-11-18T08:45:00Z\n"
).encode()


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "test.db")
    app.config.update(TESTING=True)
    return app


@pytest.fixture(autouse=True)
def _app_context(app):
    with app.app_context():
        yield


@pytest.fixture
def client(app):
    return app.test_client()


def _upload(client, temp=TEMP_CSV, handling=HANDLING_CSV, limit="8.0", name="测试趟次"):
    return client.post(
        "/trips",
        data={
            "name": name, "temp_limit": limit,
            "temp_file": (io.BytesIO(temp), "temp.csv"),
            "handling_file": (io.BytesIO(handling), "handling.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _trip_id_of(page):
    return page.request.path.split("/")[-2]


def _preview_token(client, trip_id):
    """像浏览器一样打开预览页，取出确认表单绑定的草稿令牌。"""
    page = client.get(f"/trips/{trip_id}/preview")
    assert page.status_code == 200
    m = re.search(rb'name="draft_id" value="([0-9a-f]+)"', page.data)
    assert m, "预览页确认表单缺少草稿令牌"
    return m.group(1).decode()


def _confirm(client, trip_id, token=None, **kwargs):
    """按预览页所见确认：令牌取自当前预览页（或显式指定，模拟旧页面）。"""
    if token is None:
        token = _preview_token(client, trip_id)
    return client.post(f"/trips/{trip_id}/confirm", data={"draft_id": token}, **kwargs)


def _revise(client, trip_id, limit="8.0", temp=None, handling=None, name="测试趟次"):
    data = {"name": name, "temp_limit": limit}
    if temp is not None:
        data["temp_file"] = (io.BytesIO(temp), "temp.csv")
    if handling is not None:
        data["handling_file"] = (io.BytesIO(handling), "handling.csv")
    return client.post(
        f"/trips/{trip_id}/revise", data=data,
        content_type="multipart/form-data", follow_redirects=True,
    )


def test_full_flow_preview_then_confirm(client):
    page = _upload(client)
    assert page.status_code == 200
    assert "修订预览".encode() in page.data
    assert b"BOX-1" in page.data and b"BOX-2" in page.data

    # 确认前不存在正式结论
    trips = storage.list_trips()
    assert trips == []

    trip_id = _trip_id_of(page)
    resp = _confirm(client, trip_id, follow_redirects=True)
    assert resp.status_code == 200
    assert b"rev 1" in resp.data

    trip = storage.get_trip(trip_id)
    assert trip["revision"] == 1
    by_box = {r["box"]: r for r in trip["results"]}
    # BOX-1: 超温 5 分钟；未知 = 缺口 15 分钟 + 08:45 后 15 分钟
    assert by_box["BOX-1"]["hot_seconds"] == 300
    assert by_box["BOX-1"]["unknown_seconds"] == 1800
    # BOX-2 在车 08:05-08:45：未知仅缺口 15 分钟
    assert by_box["BOX-2"]["unknown_seconds"] == 900
    # 草稿已清除
    assert storage.get_draft(trip_id) is None


def test_bad_csv_flashes_error_and_no_trip(client):
    bad = b"timestamp_utc,celsius\nnot-a-time,5\n"
    page = _upload(client, temp=bad)
    assert "时间格式无法解析".encode() in page.data
    assert storage.list_trips() == []


def test_revision_replaces_whole_trip_and_drops_deleted_box(client):
    page = _upload(client)
    trip_id = _trip_id_of(page)
    _confirm(client, trip_id)

    # 修订：装卸文件删掉 BOX-2，只留 BOX-1，并收紧温度上限
    new_handling = (
        "box,loaded_at,unloaded_at\n"
        "BOX-1,2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
    ).encode()
    resp = _revise(client, trip_id, limit="4.5", handling=new_handling)
    assert resp.status_code == 200 and "修订预览".encode() in resp.data
    _confirm(client, trip_id, follow_redirects=True)

    trip = storage.get_trip(trip_id)
    assert trip["revision"] == 2
    boxes = {r["box"] for r in trip["results"]}
    assert boxes == {"BOX-1"}  # 删除的箱号没有留下旧结论
    with storage.db_cursor() as conn:
        leftover = conn.execute(
            "SELECT COUNT(*) AS n FROM trip_result WHERE trip_id=? AND box='BOX-2'",
            (trip_id,),
        ).fetchone()["n"]
        leftover_rides = conn.execute(
            "SELECT COUNT(*) AS n FROM trip_ride WHERE trip_id=? AND box='BOX-2'",
            (trip_id,),
        ).fetchone()["n"]
    assert leftover == 0 and leftover_rides == 0
    # 上限 4.5 后 BOX-1 出现更多超温段
    r = trip["results"][0]
    assert r["hot_seconds"] > 300


def test_concurrent_confirmation_only_one_succeeds(app, client):
    page = _upload(client)
    trip_id = _trip_id_of(page)
    _confirm(client, trip_id)

    # 基于 rev 1 造两个相同 expected_revision 的草稿（模拟两个会话同时点确认）
    readings = parse_temperature_csv(TEMP_CSV)
    rides = parse_handling_csv(HANDLING_CSV)
    from app.engine import analyze_all
    results = analyze_all(readings, rides, 8.0)
    storage.upsert_draft(trip_id, 1, "测试趟次", 8.0, "", "", readings, rides, results)

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        with app.app_context():
            try:
                storage.commit_revision(trip_id, 1, "测试趟次", 8.0, readings, rides, results)
                outcomes.append("ok")
            except storage.RevisionConflict:
                outcomes.append("conflict")
            except Exception as exc:  # noqa: BLE001
                outcomes.append(f"error:{exc!r}")

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    assert storage.get_trip(trip_id)["revision"] == 2


def test_stale_draft_confirmation_rejected(client):
    page = _upload(client)
    trip_id = _trip_id_of(page)
    _confirm(client, trip_id)  # rev 1

    # 基线已过期的草稿在保存时就被明确拒绝（不会写下即过期的草稿）
    readings = parse_temperature_csv(TEMP_CSV)
    rides = parse_handling_csv(HANDLING_CSV)
    from app.engine import analyze_all
    with pytest.raises(storage.RevisionConflict):
        storage.upsert_draft(
            trip_id, 0, "旧", 8.0, "", "", readings, rides,
            analyze_all(readings, rides, 8.0),
        )

    # 路由层：没有草稿可确认，正式版保持原状
    resp = client.post(f"/trips/{trip_id}/confirm", data={"draft_id": "x" * 32},
                       follow_redirects=True)
    assert storage.get_trip(trip_id)["revision"] == 1
    assert resp.status_code == 200


def test_query_always_sees_complete_version(app, client):
    page = _upload(client)
    trip_id = _trip_id_of(page)
    _confirm(client, trip_id)

    stop = threading.Event()
    errors: list[str] = []

    def reader():
        with app.app_context():
            while not stop.is_set():
                trip = storage.get_trip(trip_id)
                boxes_ride = {r["box"] for r in trip["rides"]}
                boxes_res = {r["box"] for r in trip["results"]}
                if boxes_ride != boxes_res:
                    errors.append("读到半成品：rides 与 results 箱号不一致")
                    return

    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.05)

    # 连续做 5 次整趟替换，每次箱号集合都不同
    for i in range(5):
        handling = (
            "box,loaded_at,unloaded_at\n"
            f"BOX-{i},2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
        ).encode()
        client.post(
            f"/trips/{trip_id}/revise",
            data={"name": "t", "temp_limit": "8.0",
                  "handling_file": (io.BytesIO(handling), "h.csv")},
            content_type="multipart/form-data",
        )
        _confirm(client, trip_id)
    stop.set()
    th.join()
    assert errors == []
    assert storage.get_trip(trip_id)["revision"] == 6


def test_json_api(client):
    page = _upload(client)
    trip_id = _trip_id_of(page)
    _confirm(client, trip_id)
    resp = client.get(f"/trips/{trip_id}/api/results")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    data = resp.get_json()
    assert data["revision"] == 1 and len(data["results"]) == 2


# ---------- 预览/确认一致性（双窗口场景） ----------

def test_two_windows_old_confirm_cannot_publish_new_draft(client):
    """窗口A把上限 8℃ 改为 20℃ 重新预览；窗口B仍按 8℃ 旧页确认，
    不得发布 20℃ 的结论：旧页确认被拒绝，正式版保持原状（不存在）。"""
    page = _upload(client, limit="8.0")
    trip_id = _trip_id_of(page)
    token_8 = _preview_token(client, trip_id)  # 窗口B看到的 8℃ 预览

    # 窗口A：改为 20℃ 重新预览（草稿被替换，令牌更新）
    resp = _revise(client, trip_id, limit="20.0")
    assert resp.status_code == 200
    token_20 = _preview_token(client, trip_id)
    assert token_8 != token_20

    # 窗口B：仍按 8℃ 旧页点确认 -> 明确拒绝，正式版未产生
    resp = _confirm(client, trip_id, token=token_8, follow_redirects=True)
    assert "过期".encode() in resp.data
    assert storage.list_trips() == []
    # 20℃ 的草稿仍在，未被旧确认删除或发布
    draft = storage.get_draft(trip_id)
    assert draft is not None and draft["draft_id"] == token_20
    assert draft["temp_limit"] == 20.0

    # 窗口A按自己看到的 20℃ 页面确认 -> 发布的必须是 20℃ 的结论
    resp = _confirm(client, trip_id, token=token_20, follow_redirects=True)
    assert b"rev 1" in resp.data
    trip = storage.get_trip(trip_id)
    assert trip["temp_limit"] == 20.0
    # 20℃ 上限下所有读数合规：8℃ 时的 5 分钟超温不得出现在正式结论里
    assert all(r["hot_seconds"] == 0 for r in trip["results"])
    assert storage.get_draft(trip_id) is None


def test_confirm_without_token_rejected(client):
    page = _upload(client)
    trip_id = _trip_id_of(page)
    resp = client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    assert "过期".encode() in resp.data
    assert storage.list_trips() == []  # 正式版保持原状
    assert storage.get_draft(trip_id) is not None  # 草稿未被误删


def test_interleaved_confirm_cannot_delete_successful_revision(client):
    """确认与修订交错：保存成功的修订不能被旧确认删除。"""
    page = _upload(client, limit="8.0")
    trip_id = _trip_id_of(page)
    old_token = _preview_token(client, trip_id)

    # 新修订保存成功（草稿令牌更新）
    _revise(client, trip_id, limit="4.5")
    new_token = storage.get_draft(trip_id)["draft_id"]
    assert new_token != old_token

    # 旧页确认：被拒绝，且新草稿必须原样保留
    _confirm(client, trip_id, token=old_token)
    draft = storage.get_draft(trip_id)
    assert draft is not None and draft["draft_id"] == new_token
    assert draft["temp_limit"] == 4.5
    assert storage.list_trips() == []

    # 新草稿自己确认成功
    _confirm(client, trip_id, token=new_token)
    assert storage.get_trip(trip_id)["temp_limit"] == 4.5


def test_storage_level_stale_token_conflict_keeps_newer_draft(app, client):
    """存储层模拟交错：确认事务核对令牌失败时，不得触碰新草稿与正式版。"""
    page = _upload(client)
    trip_id = _trip_id_of(page)
    readings = parse_temperature_csv(TEMP_CSV)
    rides = parse_handling_csv(HANDLING_CSV)
    from app.engine import analyze_all
    results = analyze_all(readings, rides, 8.0)

    old_token = storage.get_draft(trip_id)["draft_id"]
    new_token = storage.upsert_draft(
        trip_id, 0, "测试趟次", 8.0, "t2", "h2", readings, rides, results
    )
    with pytest.raises(storage.DraftConflict):
        storage.commit_revision(
            trip_id, 0, "测试趟次", 8.0, readings, rides, results,
            draft_token=old_token,
        )
    # 新草稿完好，正式版未产生
    assert storage.get_draft(trip_id)["draft_id"] == new_token
    assert storage.list_trips() == []
    # 正确令牌可以提交
    rev = storage.commit_revision(
        trip_id, 0, "测试趟次", 8.0, readings, rides, results,
        draft_token=new_token,
    )
    assert rev == 1
    assert storage.get_draft(trip_id) is None


# ---------- 草稿一致性与重启恢复 ----------

def test_draft_csv_and_results_always_same_revision(client):
    """草稿的两份 CSV 原文与计算结果始终属于同一次修订。"""
    page = _upload(client)
    trip_id = _trip_id_of(page)
    draft = storage.get_draft(trip_id)
    assert draft["temp_csv"] == TEMP_CSV.decode()
    assert draft["handling_csv"] == HANDLING_CSV.decode()
    assert draft["results"]["BOX-1"]["hot_seconds"] == 300

    # 只替换温度文件：新草稿必须是"新温度原文 + 新结果 + 旧装卸原文"
    new_temp = (
        "timestamp_utc,celsius\n"
        "2026-11-18T08:00:00Z,15.0\n"
        "2026-11-18T08:05:00Z,16.0\n"
    ).encode()
    _revise(client, trip_id, limit="8.0", temp=new_temp)
    draft = storage.get_draft(trip_id)
    assert draft["temp_csv"] == new_temp.decode()
    assert draft["handling_csv"] == HANDLING_CSV.decode()
    assert draft["readings"] == [[r[0], r[1]] for r in parse_temperature_csv(new_temp)]
    # 结果按新读数重算：08:05 之后全部未知/超温，与旧结果不同
    assert draft["results"]["BOX-1"]["hot_seconds"] == 300
    assert draft["results"]["BOX-1"]["unknown_seconds"] == 3300

    # 确认后正式版同样携带本次修订的两份原文
    _confirm(client, trip_id)
    trip = storage.get_trip(trip_id)
    assert trip["temp_csv"] == new_temp.decode()
    assert trip["handling_csv"] == HANDLING_CSV.decode()

    # 确认后再修订（不传文件）：沿用的原文与数据仍属同一版本
    _revise(client, trip_id, limit="30.0")
    draft = storage.get_draft(trip_id)
    assert draft["temp_csv"] == new_temp.decode()
    assert draft["handling_csv"] == HANDLING_CSV.decode()
    assert all(r["hot_seconds"] == 0 for r in draft["results"].values())


def test_restart_keeps_consistent_state(app, client, tmp_path):
    """模拟进程重启：同一数据库重开应用，草稿不混杂、正式版完整。"""
    page = _upload(client)
    trip_id = _trip_id_of(page)
    _revise(client, trip_id, limit="4.5")

    db_path = tmp_path / "test.db"
    app2 = create_app(db_path)  # 重启
    app2.config.update(TESTING=True)
    with app2.app_context():
        draft = storage.get_draft(trip_id)
        assert draft is not None
        assert draft["temp_csv"] == TEMP_CSV.decode()
        assert draft["handling_csv"] == HANDLING_CSV.decode()
        assert draft["temp_limit"] == 4.5
        # 确认后重启：正式版完整，草稿已清
        storage.commit_revision(
            trip_id, draft["expected_revision"], draft["name"], draft["temp_limit"],
            draft["readings"], [tuple(r) for r in draft["rides"]], draft["results"],
            temp_csv=draft["temp_csv"], handling_csv=draft["handling_csv"],
            draft_token=draft["draft_id"],
        )
    app3 = create_app(db_path)
    app3.config.update(TESTING=True)
    with app3.app_context():
        trip = storage.get_trip(trip_id)
        assert trip["revision"] == 1
        assert {r["box"] for r in trip["rides"]} == {"BOX-1", "BOX-2"}
        assert {r["box"] for r in trip["results"]} == {"BOX-1", "BOX-2"}
        assert len(trip["readings"]) == 6
        assert storage.get_draft(trip_id) is None


def test_legacy_mixed_draft_discarded_on_upgrade(tmp_path):
    """旧版分表留下的混合草稿（结果与原文分属不同修订）在启动迁移时清除，
    正式版本数据不受影响。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE trip (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL,
            temp_limit REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT '2026-11-18T00:00:00Z',
            confirmed_at TEXT
        );
        CREATE TABLE trip_ride (
            trip_id TEXT NOT NULL, revision INTEGER NOT NULL, box TEXT NOT NULL,
            loaded_at INTEGER NOT NULL, unloaded_at INTEGER NOT NULL,
            PRIMARY KEY (trip_id, revision, box)
        );
        CREATE TABLE trip_result (
            trip_id TEXT NOT NULL, revision INTEGER NOT NULL, box TEXT NOT NULL,
            hot_seconds INTEGER NOT NULL, unknown_seconds INTEGER NOT NULL,
            total_seconds INTEGER NOT NULL, hot_segments TEXT NOT NULL,
            unknown_segments TEXT NOT NULL,
            PRIMARY KEY (trip_id, revision, box)
        );
        CREATE TABLE trip_reading (
            trip_id TEXT NOT NULL, revision INTEGER NOT NULL,
            ts INTEGER NOT NULL, celsius REAL NOT NULL
        );
        CREATE TABLE trip_draft (
            trip_id TEXT PRIMARY KEY, expected_revision INTEGER NOT NULL,
            name TEXT NOT NULL, temp_limit REAL NOT NULL,
            readings TEXT NOT NULL, rides TEXT NOT NULL, results TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '2026-11-18T00:00:00Z'
        );
        CREATE TABLE trip_draft_source (
            trip_id TEXT PRIMARY KEY, temp_csv TEXT NOT NULL, handling_csv TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO trip(id, name, revision, temp_limit, confirmed_at) "
        "VALUES('t1', '旧趟次', 1, 8.0, '2026-11-18T01:00:00Z')"
    )
    conn.execute(
        "INSERT INTO trip_ride VALUES('t1', 1, 'BOX-1', 100, 200)"
    )
    conn.execute(
        "INSERT INTO trip_result VALUES('t1', 1, 'BOX-1', 0, 100, 100, '[]', '[]')"
    )
    # 混合草稿：results 是修订B的计算结果，source 还是修订A的原文
    conn.execute(
        "INSERT INTO trip_draft VALUES('t1', 1, '旧趟次', 20.0, '[]', '[]', '{}', "
        "'2026-11-18T02:00:00Z')"
    )
    conn.execute(
        "INSERT INTO trip_draft_source VALUES('t1', 'rev-A-temp', 'rev-A-handling')"
    )
    conn.commit()
    conn.close()

    app = create_app(db)
    app.config.update(TESTING=True)
    with app.app_context():
        # 混合草稿被清除，不残留旁路表
        assert storage.get_draft("t1") is None
        with storage.db_cursor() as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "trip_draft_source" not in tables
        # 正式版完整可读
        trip = storage.get_trip("t1")
        assert trip["revision"] == 1
        assert trip["results"][0]["box"] == "BOX-1"
        # 迁移后可以正常走完整流程
        client = app.test_client()
        page = client.get("/trips/t1/preview")
        assert page.status_code == 404  # 草稿已清，需重新修订
        resp = client.post(
            "/trips/t1/revise",
            data={"name": "旧趟次", "temp_limit": "8.0",
                  "temp_file": (io.BytesIO(TEMP_CSV), "t.csv"),
                  "handling_file": (io.BytesIO(HANDLING_CSV), "h.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        _confirm(client, "t1", follow_redirects=True)
        assert storage.get_trip("t1")["revision"] == 2

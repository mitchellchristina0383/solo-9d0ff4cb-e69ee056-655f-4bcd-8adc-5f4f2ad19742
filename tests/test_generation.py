"""演练代次（generation）隔离测试。

复现“两个标签页交错”的核心故障：一个标签页重置演练并重发同号包，
另一个标签页迟到的旧回执不能确认新演练的报文；重置可在回执返回前
完成；同一场演练换线后旧线回执仍然有效。
"""

import pytest

from app import app as flask_app
from drill_engine import Drill, DrillConfig


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


@pytest.fixture(autouse=True)
def _fresh_drill(client):
    rv = client.post("/api/reset", json={})
    assert rv.status_code == 200
    yield


def make_config(total=4000, start=1400, step=200,
                A=(1500, 120, False), B=(1500, 60, True)):
    return {
        "total_bytes": total,
        "start_payload": start,
        "timeout_step": step,
        "lines": {
            "A": {"mtu": A[0], "overhead": A[1], "notify": A[2]},
            "B": {"mtu": B[0], "overhead": B[1], "notify": B[2]},
        },
    }


def reset(client, **kw):
    rv = client.post("/api/reset", json=make_config(**kw))
    assert rv.status_code == 200, rv.get_json()
    return rv.get_json()["state"]


def post(client, path, body=None, expect=200):
    rv = client.post(path, json=body or {})
    assert rv.status_code == expect, (path, rv.status_code, rv.get_json())
    return rv.get_json()


def test_reset_issues_new_generation_and_state_carries_it(client):
    g1 = client.get("/api/state").get_json()["state"]["generation"]
    st = reset(client, total=3000)
    g2 = st["generation"]
    assert g1 != g2 and isinstance(g2, str) and g2
    # 再重置一次，令牌继续单调换新
    g3 = reset(client)["generation"]
    assert g3 != g2 and g3 != g1


def test_stale_ack_after_reset_is_rejected_and_new_packet_untouched(client):
    # 第一场演练：A 线静默黑洞，连发 3 包（#1..#3）
    reset(client, total=4000)
    post(client, "/api/send-burst", {"count": 3})
    old_gen = client.get("/api/state").get_json()["state"]["generation"]

    # 让 #3 成为“实际在途、可回执”的包（1200 载荷小包）
    # 另一标签页在旧回执返回前重置演练：reset 不校验代次，直接成功
    new_state = reset(client, total=4000)
    new_gen = new_state["generation"]
    assert new_gen != old_gen

    # 新演练同样发包，制造编号相同的新 #3
    post(client, "/api/send-burst", {"count": 3})
    new_pkt3 = next(p for p in
                    client.get("/api/state").get_json()["state"]["packets"]
                    if p["id"] == 3)
    assert new_pkt3["status"] == "outstanding"

    # 旧标签页迟到的旧演练回执（带旧令牌）必须被退回
    rv = client.post("/api/packets/3/ack", json={"generation": old_gen})
    assert rv.status_code == 409
    body = rv.get_json()
    assert body["ok"] is False and "重置" in body["error"]

    # 新演练的同号 #3 没有被误确认，确认字节仍为 0
    st = client.get("/api/state").get_json()["state"]
    pkt3 = next(p for p in st["packets"] if p["id"] == 3)
    assert pkt3["status"] == "outstanding"
    assert st["acked_count"] == 0 and st["generation"] == new_gen

    # 新标签页用新令牌回执同号包，正常确认
    ok = post(client, "/api/packets/3/ack", {"generation": new_gen})
    assert ok["added_bytes"] == 1200
    assert ok["state"]["acked_count"] == 1200


def test_all_write_endpoints_reject_stale_generation(client):
    reset(client, total=3000, start=1000, A=(1500, 0, False))
    post(client, "/api/send-burst", {"count": 2})
    old_gen = client.get("/api/state").get_json()["state"]["generation"]
    reset(client, total=3000, start=1000, A=(1500, 0, False))

    stale = {"generation": old_gen}
    for path, body in [
        ("/api/send-next", {}),
        ("/api/send-burst", {"count": 2}),
        ("/api/resend-missing", {}),
        ("/api/switch", {"line": "B"}),
        ("/api/packets/1/ack", {}),
        ("/api/packets/1/timeout", {}),
    ]:
        merged = dict(stale, **body)
        rv = client.post(path, json=merged)
        assert rv.status_code == 409, (path, rv.status_code, rv.get_json())
        assert rv.get_json()["ok"] is False

    # 重置后新演练里没有任何报文（写操作全部被挡下）
    st = client.get("/api/state").get_json()["state"]
    assert st["packets"] == [] and st["acked_count"] == 0


def test_request_without_generation_is_backward_compatible(client):
    reset(client, total=3000, start=1000, A=(1500, 0, False))
    # 不带 generation 的请求（模拟旧客户端）照常工作
    pkts = post(client, "/api/send-burst", {"count": 2})["packets"]
    pid = pkts[0]["id"]
    rv = client.post(f"/api/packets/{pid}/ack")  # 空体、无令牌
    assert rv.status_code == 200
    assert rv.get_json()["added_bytes"] == 1000


def test_switch_within_same_generation_keeps_old_line_acks_valid(client):
    # 同一场演练换线不换代次：旧线在途回执照样确认对应字节
    st = reset(client, total=2800, A=(1500, 0, False), B=(1500, 60, True))
    gen = st["generation"]
    post(client, "/api/send-burst", {"count": 2})  # #1 #2 走 A 线、在途
    sw = post(client, "/api/switch",
              {"line": "B", "generation": gen})["result"]
    # 换线后令牌不变
    assert client.get("/api/state").get_json()["state"]["generation"] == gen

    # 旧 A 线 #1 的回执带同一场演练的令牌，正常确认
    r = post(client, "/api/packets/1/ack", {"generation": gen})
    assert r["added_bytes"] == 1400
    # B 线换线重发包也能确认
    b_id = sw["packets"][0]["id"]
    r2 = post(client, f"/api/packets/{b_id}/ack", {"generation": gen})
    assert r2["added_bytes"] == 0  # 与 #1 区间完全重叠，只去重、不报错


def test_engine_generation_attribute_round_trips():
    d = Drill(DrillConfig.from_raw({}), generation="gen-7")
    assert d.generation == "gen-7"
    payload = d.state_dict()
    assert payload["generation"] == "gen-7"
    d.reset(DrillConfig.from_raw({}), generation="gen-8")
    assert d.generation == "gen-8"

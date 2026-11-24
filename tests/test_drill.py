"""端到端 API 测试：静默丢包、超长通知轮次、换线后回包交错、重叠去重。"""

import pytest

from app import app as flask_app
from drill_engine import AckedRanges


# ---------------------------------------------------------------- helpers

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


def ack(client, pid):
    return post(client, f"/api/packets/{pid}/ack")


def timeout(client, pid):
    return post(client, f"/api/packets/{pid}/timeout")


def notice(client, pid):
    return post(client, f"/api/packets/{pid}/deliver-notice")


# ---------------------------------------------------------------- 基础页面

def test_index_and_healthz(client):
    assert client.get("/healthz").get_json()["ok"] is True
    html = client.get("/").get_data(as_text=True)
    assert "VPN 换线" in html
    assert "报表字节数" in html


def test_bad_config_rejected(client):
    rv = client.post("/api/reset", json={"total_bytes": 0})
    assert rv.status_code == 400
    rv = client.post("/api/reset",
                    json={"lines": {"A": {"mtu": "abc"}}})
    assert rv.status_code == 400


# ---------------------------------------------------------------- 区间单元

def test_acked_ranges_union_and_packetize():
    ar = AckedRanges(100)
    assert ar.add(10, 20) == 10
    assert ar.add(15, 30) == 10       # 重叠 5，新增到 30
    assert ar.ranges == [(10, 30)]
    assert ar.add(40, 50) == 10
    assert ar.count == 30
    assert ar.first_unacked() == 0

    # 从 0 开始，每 15 字节一段；遇到已确认区间 [10,30) 必须断开
    runs = ar.packetize_unacked(15)
    assert runs[0] == (0, 10)
    # 跳过 [10,30) 后从 30 继续
    assert (30, 40) in runs
    assert (50, 65) in runs

    ar.add(0, 10)
    assert ar.first_unacked() == 30


# ---------------------------------------------------------------- 场景一：静默丢包

def test_silent_drop_timeout_shrink_then_complete(client):
    reset(client, total=4000)  # A: 1400+120=1520 > 1500，且通知关
    data = post(client, "/api/send-burst", {"count": 3})
    p1, p2, p3 = data["packets"]

    assert (p1["start"], p1["end"]) == (0, 1400)
    assert p1["physical"] == "silent_drop"
    assert p1["notice_state"] is None
    assert p2["physical"] == "silent_drop"
    # 最后一包只有 1200 载荷：1200+120=1320 <= 1500，小包实际能通
    assert (p3["start"], p3["end"]) == (2800, 4000)
    assert p3["size"] == 1200
    assert p3["physical"] == "in_flight"

    # 静默丢弃的包不可能 ACK
    rv = client.post("/api/packets/1/ack")
    assert rv.status_code == 409

    st = ack(client, 3)["state"]
    assert st["acked_count"] == 1200
    assert st["complete"] is False

    # 触发当前轮 #1 超时：1400 -> 1200，轮次 1 -> 2，并立即重发最早缺口
    r = timeout(client, 1)["result"]
    assert r["shrunk"] is True
    assert r["old_payload"] == 1400 and r["new_payload"] == 1200
    assert r["resent"]["start"] == 0 and r["resent"]["end"] == 1200
    assert r["resent"]["physical"] == "in_flight"
    state = client.get("/api/state").get_json()["state"]
    assert state["round"] == 2 and state["cur_payload"] == 1200

    assert ack(client, 4)["state"]["acked_count"] == 2400

    # 重发剩余缺口：[1200,2400) 与被确认区间截断出的 [2400,2800)
    pkts = post(client, "/api/resend-missing")["packets"]
    assert [(p["start"], p["end"]) for p in pkts] == [(1200, 2400), (2400, 2800)]

    assert ack(client, 5)["state"]["acked_count"] == 3600
    final = ack(client, 6)["state"]
    assert final["acked_count"] == 4000
    assert final["complete"] is True

    # 传完后不再允许发送
    assert client.post("/api/send-next").status_code == 409


# ---------------------------------------------------------------- 场景二：超长通知与轮次

def test_notice_shrinks_once_per_round(client):
    # A 打开通知，eff=1380；total=4200 让三连包全是 1400
    reset(client, total=4200, A=(1500, 120, True))
    pkts = post(client, "/api/send-burst", {"count": 3})["packets"]
    assert all(p["physical"] == "notify_drop" for p in pkts)
    assert all(p["notice_state"] == "pending" for p in pkts)

    # 先投递 #2 的通知：生效，载荷直接降到 1380，进入第 2 轮
    r = notice(client, 2)["result"]
    assert r["applied"] is True and r["new_payload"] == 1380
    state = client.get("/api/state").get_json()["state"]
    assert state["round"] == 2

    # 同批 #1/#3 的通知属于旧轮，全部忽略，不能再缩
    r1 = notice(client, 1)["result"]
    r3 = notice(client, 3)["result"]
    assert r1["applied"] is False and r1["stale"] is True
    assert r3["stale"] is True
    state = client.get("/api/state").get_json()["state"]
    assert state["cur_payload"] == 1380 and state["round"] == 2

    # 重复投递已处理通知报错
    assert client.post("/api/packets/2/deliver-notice").status_code == 409


def test_timeout_stale_round_does_not_shrink(client):
    reset(client, total=2800, A=(1500, 0, False), B=(1500, 60, True))
    post(client, "/api/send-burst", {"count": 2})
    # 直接换线：轮次 1 -> 2，载荷重置 1400
    sw = post(client, "/api/switch", {"line": "B"})["result"]
    assert sw["round"] == 2 and sw["cur_payload"] == 1400

    # 旧轮 #1 超时：只标记，不缩包
    r = timeout(client, 1)["result"]
    assert r["stale"] is True and r["shrunk"] is False
    state = client.get("/api/state").get_json()["state"]
    assert state["cur_payload"] == 1400 and state["round"] == 2
    assert state["packets"][0]["status"] == "timeout"

    # 但旧线报文之后仍可补到“迟到回执”
    res = ack(client, 1)
    assert res["added_bytes"] == 1400
    p1 = next(p for p in res["state"]["packets"] if p["id"] == 1)
    assert p1["late_ack"] is True

    # 当前轮（B 线）报文超时则正常缩包 + 重发
    b_pkt_id = sw["packets"][0]["id"]
    r = timeout(client, b_pkt_id)["result"]
    assert r["shrunk"] is True
    assert r["old_payload"] == 1400 and r["new_payload"] == 1200
    assert r["resent"]["line"] == "B"


# ---------------------------------------------------------------- 场景三：换线后回包交错 + 重叠去重

def test_switch_then_interleaved_acks_dedup(client):
    reset(client)  # total=10000? 不，显式给 4200：
    reset(client, total=4200, A=(1500, 200, False))  # A eff=1300 全超
    post(client, "/api/send-burst", {"count": 3})     # #1..#3 静默丢
    timeout(client, 1)                                 # ->1200, #4 [0,1200)
    ack(client, 4)

    pkts = post(client, "/api/resend-missing")["packets"]
    assert [(p["id"], p["start"], p["end"]) for p in pkts] == [
        (5, 1200, 2400), (6, 2400, 3600), (7, 3600, 4200)]

    # 回执前换 B：载荷重置 1400，立即重发全部未确认 -> #8..#10
    sw = post(client, "/api/switch", {"line": "B"})["result"]
    assert sw["cur_payload"] == 1400 and sw["round"] == 3
    assert [(p["id"], p["start"], p["end"], p["line"]) for p in sw["packets"]] == [
        (8, 1200, 2600, "B"), (9, 2600, 4000, "B"), (10, 4000, 4200, "B")]

    # 乱序 1：先回执 B 线 #9 [2600,4000)
    st = ack(client, 9)["state"]
    assert st["acked_count"] == 2600  # [0,1200)+[2600,4000)

    # 乱序 2：旧 A 线 #5 [1200,2400) 照样确认
    assert ack(client, 5)["state"]["acked_count"] == 3800

    # 乱序 3：B 线 #8 [1200,2600) 与 #5 大面积重叠，只新增 200
    r = ack(client, 8)
    assert r["added_bytes"] == 200
    assert r["state"]["acked_count"] == 4000
    assert r["state"]["complete"] is False

    # 旧 A 线 #6 完全落在已确认并集内：新增 0
    r = ack(client, 6)
    assert r["added_bytes"] == 0
    assert r["state"]["acked_count"] == 4000

    # B 线尾包 #10 补齐最后 200
    r = ack(client, 10)
    assert r["added_bytes"] == 200
    assert r["state"]["acked_count"] == 4200
    assert r["state"]["complete"] is True


def test_switch_with_no_prior_ack_exact_duplicates_count_once(client):
    reset(client, total=2800, A=(1500, 0, True), B=(1500, 60, True))
    post(client, "/api/send-burst", {"count": 2})  # #1 [0,1400) #2 [1400,2800)

    sw = post(client, "/api/switch", {"line": "B"})["result"]
    assert [(p["start"], p["end"]) for p in sw["packets"]] == [
        (0, 1400), (1400, 2800)]  # 完全重叠的新副本 #3 #4

    assert ack(client, 3)["state"]["acked_count"] == 1400
    r = ack(client, 1)             # 旧线相同区间的回执
    assert r["added_bytes"] == 0
    assert r["state"]["acked_count"] == 1400

    final = ack(client, 2)["state"]
    assert final["acked_count"] == 2800 and final["complete"] is True

    # 完成后重复/重叠回执仍幂等，但不再允许发新包或换线
    assert ack(client, 4)["added_bytes"] == 0
    assert client.post("/api/send-next").status_code == 409
    assert client.post("/api/switch", json={"line": "A"}).status_code == 409


def test_burst_all_sends_until_frontier(client):
    reset(client, total=3000, start=1000)
    pkts = post(client, "/api/send-burst", {"count": "all"})["packets"]
    assert [(p["start"], p["end"]) for p in pkts] == [
        (0, 1000), (1000, 2000), (2000, 3000)]
    # 新字节已到末尾，再来普通连发会报 409 并提示用重发
    assert client.post("/api/send-burst", json={"count": 2}).status_code == 409


def test_send_next_advances_then_fills_gap(client):
    reset(client, total=3000, start=1000)
    p1 = post(client, "/api/send-next")["packet"]
    p2 = post(client, "/api/send-next")["packet"]
    p3 = post(client, "/api/send-next")["packet"]
    assert [(p["start"], p["end"]) for p in (p1, p2, p3)] == [
        (0, 1000), (1000, 2000), (2000, 3000)]

    ack(client, p1["id"])
    # 新字节已发完，send-next 转为补最早缺口 [1000,2000)
    p4 = post(client, "/api/send-next")["packet"]
    assert (p4["start"], p4["end"]) == (1000, 2000)

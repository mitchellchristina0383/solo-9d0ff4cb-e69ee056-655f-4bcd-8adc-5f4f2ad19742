"""VPN 换线 / MTU 黑洞演练页 —— Flask 入口。

单实例内存状态 + 一把锁，适合单人演练；部署见 Dockerfile / compose.yaml
（gunicorn 固定 1 个 worker、多线程，保证内存状态一致）。
"""

from __future__ import annotations

import threading

from flask import Flask, jsonify, render_template, request

from drill_engine import Drill, DrillConfig, DrillError

app = Flask(__name__)

_LOCK = threading.RLock()
_DRILL = Drill()

# 单调递增的演练代次计数器：每次重置分配一个新令牌，旧标签页手里的旧令牌
# 只能匹配上一场演练，重置之后才被处理的迟到写请求（如旧回执）一律退回。
_GENERATION_SEQ = 0


def _new_generation() -> str:
    global _GENERATION_SEQ
    _GENERATION_SEQ += 1
    return f"gen-{_GENERATION_SEQ}"


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _check_generation(data: dict) -> None:
    """拒绝属于上一场演练的迟到写请求。

    缺少 generation 字段的请求视为兼容旧客户端，放行；携带的令牌与当前
    演练不一致时返回 409（请求与当前状态冲突），且不改变任何状态。
    """
    token = data.get("generation")
    if token is None:
        return
    if token != _DRILL.generation:
        raise DrillError(
            "演练已被重置，请求来自旧演练（代次令牌已过期），请刷新页面后重试",
            409,
        )


@app.errorhandler(DrillError)
def handle_drill_error(err: DrillError):
    return jsonify({"ok": False, "error": err.message}), err.status


@app.errorhandler(404)
def handle_404(_err):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "接口不存在"}), 404
    return "Not Found", 404


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def get_state():
    with _LOCK:
        return jsonify({"ok": True, "state": _DRILL.state_dict()})


@app.post("/api/reset")
def reset_drill():
    global _DRILL
    data = _json_body()
    with _LOCK:
        cfg = DrillConfig.from_raw(data if data else None)
        # 重置可以在任何回执返回前完成：reset 不做代次校验，直接换令牌。
        _DRILL = Drill(cfg, generation=_new_generation())
        return jsonify({"ok": True, "state": _DRILL.state_dict()})


@app.post("/api/send-next")
def send_next():
    data = _json_body()
    with _LOCK:
        _check_generation(data)
        pkt = _DRILL.send_next()
        return jsonify({"ok": True, "packet": pkt.to_dict(),
                        "state": _DRILL.state_dict()})


@app.post("/api/send-burst")
def send_burst():
    data = _json_body()
    count = data.get("count")
    with _LOCK:
        _check_generation(data)
        pkts = _DRILL.send_burst(None if count in (None, "", 0, "all") else count)
        return jsonify({"ok": True, "packets": [p.to_dict() for p in pkts],
                        "state": _DRILL.state_dict()})


@app.post("/api/resend-missing")
def resend_missing():
    data = _json_body()
    with _LOCK:
        _check_generation(data)
        pkts = _DRILL.resend_missing(kind="missing")
        return jsonify({"ok": True, "packets": [p.to_dict() for p in pkts],
                        "state": _DRILL.state_dict()})


@app.post("/api/switch")
def switch_line():
    data = _json_body()
    line = data.get("line", "")
    with _LOCK:
        _check_generation(data)
        result = _DRILL.switch_line(str(line).upper())
        return jsonify({"ok": True, "result": result,
                        "state": _DRILL.state_dict()})


@app.post("/api/packets/<int:pid>/ack")
def ack_packet(pid: int):
    data = _json_body()
    with _LOCK:
        # 必须先查代次再按 id 找包：旧演练的 #1 与新演练的 #1 编号相同，
        # 代次不对直接退回，绝不触碰新演练的同号包。
        _check_generation(data)
        _pkt, added = _DRILL.acknowledge(pid)
        return jsonify({"ok": True, "added_bytes": added,
                        "state": _DRILL.state_dict()})


@app.post("/api/packets/<int:pid>/timeout")
def timeout_packet(pid: int):
    data = _json_body()
    with _LOCK:
        _check_generation(data)
        result = _DRILL.trigger_timeout(pid)
        return jsonify({"ok": True, "result": result,
                        "state": _DRILL.state_dict()})


@app.post("/api/packets/<int:pid>/deliver-notice")
def deliver_notice(pid: int):
    data = _json_body()
    with _LOCK:
        _check_generation(data)
        result = _DRILL.deliver_notice(pid)
        return jsonify({"ok": True, "result": result,
                        "state": _DRILL.state_dict()})


if __name__ == "__main__":
    # 本机直接 `python app.py` 时使用；容器内走 gunicorn。
    app.run(host="0.0.0.0", port=8000, threaded=True)

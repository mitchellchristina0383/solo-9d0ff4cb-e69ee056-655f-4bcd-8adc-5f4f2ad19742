"""VPN 换线 / PMTU 黑洞演练引擎。

与 Web 框架无关，Flask 路由只做参数解析和序列化。

核心规则（与题目要求一一对应）：
* 报文置 DF（禁止分片），``payload + overhead > mtu`` 即被线路丢弃；
* 超长通知开关打开时，丢弃同时产生“超长通知”，由页面手动投递；
* 每次发包带当前“轮次”号；只有当前轮次报文的通知/超时才能缩包，
  处理一次有效信号后轮次 +1，同批其它旧轮次信号全部失效；
* 换线会把当前载荷重置为“起始载荷”，轮次 +1，并立即重发全部未确认字节；
* 任何线路（包括旧线）报文的成功回执都有效；
* 已确认字节用合并区间的并集维护，重叠范围只计一次；
* 重发打包时跳过所有已确认区间，只取尚未确认的连续字节。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional

# ---- 输入/规模限制，防止误输入产生海量包 -------------------------------
MAX_TOTAL_BYTES = 1_000_000
MAX_PAYLOAD = 65_535
MAX_STEP = 65_535
MAX_PACKETS = 5_000
EVENT_KEEP = 400


class DrillError(Exception):
    """业务错误。status 为建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _as_int(data: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = data.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise DrillError(f"参数 {key} 必须是整数，收到：{raw!r}")
    if not lo <= value <= hi:
        raise DrillError(f"参数 {key} 必须在 {lo}~{hi} 之间，当前为 {value}")
    return value


def _as_bool(data: dict, key: str, default: bool) -> bool:
    raw = data.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        if raw.lower() in ("1", "true", "yes", "on", "开"):
            return True
        if raw.lower() in ("0", "false", "no", "off", "关"):
            return False
    raise DrillError(f"参数 {key} 必须是布尔值，收到：{raw!r}")


@dataclass
class LineConfig:
    key: str
    label: str
    mtu: int = 1500
    overhead: int = 100
    notify: bool = False

    @property
    def max_payload(self) -> int:
        """不分片时这条线路允许的最大三层载荷。"""
        return self.mtu - self.overhead

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "mtu": self.mtu,
            "overhead": self.overhead,
            "notify": self.notify,
            "max_payload": self.max_payload,
        }


@dataclass
class DrillConfig:
    total_bytes: int = 10_000
    start_payload: int = 1400
    timeout_step: int = 200
    lines: dict = field(default_factory=dict)

    @staticmethod
    def from_raw(data: Optional[dict]) -> "DrillConfig":
        data = data or {}
        total = _as_int(data, "total_bytes", 10_000, 1, MAX_TOTAL_BYTES)
        start = _as_int(data, "start_payload", 1400, 1, MAX_PAYLOAD)
        step = _as_int(data, "timeout_step", 200, 1, MAX_STEP)

        raw_lines = data.get("lines") if isinstance(data.get("lines"), dict) else {}
        defaults = {
            "A": {"mtu": 1500, "overhead": 120, "notify": False},
            "B": {"mtu": 1500, "overhead": 60, "notify": True},
        }
        lines: dict[str, LineConfig] = {}
        for key in ("A", "B"):
            ld = raw_lines.get(key) if isinstance(raw_lines.get(key), dict) else {}
            merged = dict(defaults[key])
            merged.update({k: v for k, v in ld.items() if k in merged})
            lines[key] = LineConfig(
                key=key,
                label=f"线路 {key}",
                mtu=_as_int(merged, "mtu", defaults[key]["mtu"], 1, MAX_PAYLOAD),
                overhead=_as_int(merged, "overhead", defaults[key]["overhead"], 0, MAX_PAYLOAD),
                notify=_as_bool(merged, "notify", defaults[key]["notify"]),
            )
        return DrillConfig(total, start, step, lines)

    def to_dict(self) -> dict:
        return {
            "total_bytes": self.total_bytes,
            "start_payload": self.start_payload,
            "timeout_step": self.timeout_step,
            "lines": {k: v.to_dict() for k, v in self.lines.items()},
        }


class AckedRanges:
    """已确认字节的合并区间并集（左闭右开），并负责未确认区间的打包。"""

    def __init__(self, total: int):
        self.total = total
        self.ranges: list[tuple[int, int]] = []
        self.count = 0

    def add(self, left: int, right: int) -> int:
        """加入 [left, right)，返回本次真正新增的确认字节数。"""
        if not (0 <= left < right <= self.total):
            raise DrillError(f"非法字节区间 [{left}, {right})")
        old_count = self.count
        merged: list[tuple[int, int]] = []
        lo, hi = left, right
        placed = False
        for a, b in self.ranges:
            if b < lo:  # 完全在左侧
                merged.append((a, b))
            elif a > hi:  # 完全在右侧
                if not placed:
                    merged.append((lo, hi))
                    placed = True
                merged.append((a, b))
            else:  # 相交或相邻，合并
                lo, hi = min(lo, a), max(hi, b)
        if not placed:
            merged.append((lo, hi))
        merged.sort()
        self.ranges = merged
        self.count = sum(b - a for a, b in merged)
        return self.count - old_count

    def first_unacked(self) -> Optional[int]:
        pos = 0
        for a, b in self.ranges:
            if a > pos:
                return pos
            if b > pos:
                pos = b
        return pos if pos < self.total else None

    def packetize_unacked(self, max_len: int) -> list[tuple[int, int]]:
        """按 max_len 把所有未确认字节切成连续区间；遇到已确认区间就断开。

        每一段内部保证全部未确认，因此“重发只取尚未确认的字节”。
        """
        if max_len < 1:
            raise DrillError("打包长度必须 >= 1")
        runs: list[tuple[int, int]] = []
        pos = 0
        i = 0
        rs = self.ranges
        n = len(rs)
        total = self.total
        while pos < total:
            while i < n and rs[i][1] <= pos:
                i += 1
            if i < n and rs[i][0] <= pos < rs[i][1]:
                pos = rs[i][1]  # 落在已确认区间内，跳到区间末尾
                continue
            end = min(total, pos + max_len)
            if i < n and rs[i][0] > pos:
                end = min(end, rs[i][0])  # 不跨过下一个已确认区间
            runs.append((pos, end))
            pos = end
        return runs


@dataclass
class Packet:
    id: int
    line: str
    size: int
    start: int
    end: int
    wire_size: int
    round: int
    kind: str  # new | missing | timeout | switch
    # 发送端看到的协议状态
    status: str = "outstanding"  # outstanding | acked | timeout
    # 上帝视角的物理结果
    physical: str = "in_flight"  # in_flight | silent_drop | notify_drop
    notice_state: Optional[str] = None  # None | pending | applied | ignored
    late_ack: bool = False
    created_order: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class Drill:
    """一次演练的全部可变状态。Web 层用锁保护对同一实例的调用。"""

    def __init__(self, config: Optional[DrillConfig] = None):
        self.reset(config or DrillConfig.from_raw(None))

    # ---- 初始化 / 事件 -------------------------------------------------
    def reset(self, config: DrillConfig) -> None:
        self.cfg = config
        self.active_line = "A"
        self.cur_payload = config.start_payload
        self.round = 1
        self.next_offset = 0
        self.next_pkt_id = 1
        self.packets: list[Packet] = []
        self.acked = AckedRanges(config.total_bytes)
        self.complete = False
        self.completed_at: Optional[str] = None
        self.events: list[dict] = []
        self._event_seq = 0
        self._log("reset", f"演练开始：报表 {config.total_bytes} 字节，"
                           f"起始载荷 {config.start_payload}，初始走线路 A（第 1 轮）")

    def _log(self, kind: str, text: str) -> dict:
        self._event_seq += 1
        event = {
            "seq": self._event_seq,
            "time": time.strftime("%H:%M:%S"),
            "kind": kind,
            "text": text,
        }
        self.events.append(event)
        if len(self.events) > EVENT_KEEP:
            self.events = self.events[-EVENT_KEEP:]
        return event

    def _line(self, key: Optional[str] = None) -> LineConfig:
        return self.cfg.lines[key or self.active_line]

    def _get_packet(self, pid: int) -> Packet:
        for p in self.packets:
            if p.id == pid:
                return p
        raise DrillError(f"找不到报文 #{pid}", 404)

    def _sync_complete(self) -> None:
        if not self.complete and self.acked.count >= self.cfg.total_bytes:
            self.complete = True
            self.completed_at = time.strftime("%H:%M:%S")
            self._log("complete",
                      f"累计确认 {self.acked.count}/{self.cfg.total_bytes} 字节，全部传完 ✅")

    # ---- 发送 ----------------------------------------------------------
    def _dispatch(self, start: int, end: int, kind: str,
                  line: Optional[str] = None) -> Packet:
        line_key = line or self.active_line
        lc = self._line(line_key)
        size = end - start
        wire = size + lc.overhead
        pkt = Packet(
            id=self.next_pkt_id,
            line=line_key,
            size=size,
            start=start,
            end=end,
            wire_size=wire,
            round=self.round,
            kind=kind,
            created_order=self.next_pkt_id,
        )
        self.next_pkt_id += 1

        if wire > lc.mtu:  # DF：连封装超 MTU，立即丢弃，不分片
            if lc.notify:
                pkt.physical = "notify_drop"
                pkt.notice_state = "pending"
                self._log(
                    "drop-notify",
                    f"#{pkt.id} [{start},{end}) 载荷 {size}+封装 {lc.overhead}"
                    f"={wire} > MTU {lc.mtu}，在{lc.label}被丢弃；已生成超长通知（待投递）",
                )
            else:
                pkt.physical = "silent_drop"
                self._log(
                    "drop-silent",
                    f"#{pkt.id} [{start},{end}) 载荷 {size}+封装 {lc.overhead}"
                    f"={wire} > MTU {lc.mtu}，在{lc.label}被静默丢弃（对端无任何回音）",
                )
        else:
            self._log(
                "send",
                f"#{pkt.id} 经{lc.label}发送 [{start},{end})，载荷 {size} 字节，"
                f"封装后 {wire}/{lc.mtu}，第 {self.round} 轮（{self._kind_text(kind)}）",
            )
        self.packets.append(pkt)
        return pkt

    @staticmethod
    def _kind_text(kind: str) -> str:
        return {
            "new": "新发顺序段",
            "missing": "重发未确认",
            "timeout": "超时缩包后重发",
            "switch": "换线后立即重发",
        }.get(kind, kind)

    def send_next(self) -> Packet:
        """发下一包：优先推进尚未首次发送的新字节；新字节发完后补最早的缺口。"""
        if self.complete:
            raise DrillError("报表已全部确认，无需再发", 409)
        total = self.cfg.total_bytes
        if self.next_offset < total:
            start = self.next_offset
            end = min(total, start + self.cur_payload)
            self.next_offset = end
            kind = "new"
        else:
            runs = self.acked.packetize_unacked(self.cur_payload)
            if not runs:
                self._sync_complete()
                raise DrillError("已经没有未确认字节", 409)
            start, end = runs[0]
            kind = "missing"
        return self._dispatch(start, end, kind)

    def send_burst(self, count: Optional[int]) -> list[Packet]:
        """连续发送 count 个顺序新包；count 为空时把剩余新字节全部发完。"""
        if self.complete:
            raise DrillError("报表已全部确认，无需再发", 409)
        if count is None:
            remaining_bytes = self.cfg.total_bytes - self.next_offset
            need = (remaining_bytes + self.cur_payload - 1) // self.cur_payload
            if need > MAX_PACKETS:
                raise DrillError(
                    f"按当前载荷 {self.cur_payload} 连发全部需要约 {need} 个包，"
                    f"超过上限 {MAX_PACKETS}，请调大起始载荷",
                )
            count = max(1, need)
        count = _as_int({"c": count}, "c", 1, 1, MAX_PACKETS)
        created: list[Packet] = []
        for _ in range(count):
            if self.next_offset >= self.cfg.total_bytes:
                break
            start = self.next_offset
            end = min(self.cfg.total_bytes, start + self.cur_payload)
            self.next_offset = end
            created.append(self._dispatch(start, end, "new"))
        if not created:
            raise DrillError("新字节已全部分段发送过，请用“重发未确认字节”补缺口", 409)
        self._log("burst", f"本次连续发包 {len(created)} 个（均为第 {self.round} 轮）")
        return created

    def resend_missing(self, kind: str = "missing") -> list[Packet]:
        """按当前载荷把所有未确认字节重新打包发出；只含尚未确认的字节。"""
        if self.complete:
            raise DrillError("报表已全部确认，无需重发", 409)
        runs = self.acked.packetize_unacked(self.cur_payload)
        if not runs:
            self._sync_complete()
            raise DrillError("没有需要重发的未确认字节", 409)
        if len(runs) > MAX_PACKETS:
            raise DrillError(
                f"按当前载荷 {self.cur_payload} 会产生 {len(runs)} 个包，"
                f"超过上限 {MAX_PACKETS}，请调大载荷或报表后重试",
            )
        pkts = [self._dispatch(s, e, kind) for s, e in runs]
        self._log("resend", f"按载荷 {self.cur_payload} 重新打包未确认字节，共 {len(pkts)} 包")
        return pkts

    # ---- 回执 / 超时 / 通知 -------------------------------------------
    def acknowledge(self, pid: int) -> tuple[Packet, int]:
        pkt = self._get_packet(pid)
        if pkt.status == "acked":
            self._log("ack-dup", f"#{pid} 的回执重复到达，忽略")
            return pkt, 0
        if pkt.physical != "in_flight":
            raise DrillError(
                f"#{pid} 实际已被线路丢弃（{pkt.physical}），不可能收到成功回执；"
                "静默丢包只能触发超时",
                409,
            )
        late = pkt.status == "timeout"
        added = self.acked.add(pkt.start, pkt.end)
        pkt.status = "acked"
        pkt.late_ack = late
        tag = "迟到回执" if late else "回执"
        if added > 0:
            self._log(
                "ack",
                f"#{pid} {tag}确认 [{pkt.start},{pkt.end})，新增 {added} 字节，"
                f"累计确认 {self.acked.count}/{self.cfg.total_bytes}",
            )
        else:
            self._log(
                "ack-overlap",
                f"#{pid} {tag}到达，但其区间 [{pkt.start},{pkt.end}) 已被其它回执覆盖，"
                f"新增 0 字节（重叠不重复计数），累计仍为 {self.acked.count}",
            )
        self._sync_complete()
        return pkt, added

    def trigger_timeout(self, pid: int) -> dict:
        pkt = self._get_packet(pid)
        if pkt.status != "outstanding":
            raise DrillError(f"#{pid} 当前状态为 {pkt.status}，不能再触发超时", 409)
        pkt.status = "timeout"

        if pkt.round != self.round:
            self._log(
                "timeout-stale",
                f"#{pid}（第 {pkt.round} 轮）超时，但当前已是第 {self.round} 轮；"
                "按规则旧轮次超时不再触发缩包",
            )
            return {"shrunk": False, "stale": True, "packet": pkt.to_dict(),
                    "resent": None}

        old = self.cur_payload
        self.cur_payload = max(1, old - self.cfg.timeout_step)
        self.round += 1
        self._log(
            "timeout-shrink",
            f"#{pid} 超时（当前轮）：载荷 {old} -> {self.cur_payload}"
            f"（步长 {self.cfg.timeout_step}），进入第 {self.round} 轮",
        )
        # 立即用新载荷重发最早的未确认连续段
        resent: Optional[Packet] = None
        runs = self.acked.packetize_unacked(self.cur_payload)
        if runs:
            s, e = runs[0]
            resent = self._dispatch(s, e, "timeout")
        self._sync_complete()
        return {"shrunk": True, "stale": False, "packet": pkt.to_dict(),
                "old_payload": old, "new_payload": self.cur_payload,
                "resent": resent.to_dict() if resent else None}

    def deliver_notice(self, pid: int) -> dict:
        pkt = self._get_packet(pid)
        if pkt.notice_state is None:
            raise DrillError(f"#{pid} 没有待处理的超长通知", 409)
        if pkt.notice_state != "pending":
            raise DrillError(f"#{pid} 的超长通知已经处理过（{pkt.notice_state}）", 409)

        if pkt.round != self.round:
            pkt.notice_state = "ignored"
            self._log(
                "notice-stale",
                f"#{pid} 的超长通知属于第 {pkt.round} 轮，当前已是第 {self.round} 轮"
                "（可能已缩包或换线）；按规则忽略，当前载荷不变",
            )
            return {"applied": False, "stale": True, "packet": pkt.to_dict()}

        lc = self._line(pkt.line)
        target = max(1, lc.max_payload)
        old = self.cur_payload
        self.cur_payload = min(old, target)
        pkt.notice_state = "applied"
        self.round += 1
        warn = ""
        if lc.max_payload < 1:
            warn = "；注意 MTU-封装开销 <= 0，配置有误，任何载荷都无法通过"
        self._log(
            "notice-apply",
            f"投递#{pid} 的{lc.label}超长通知：允许的最大载荷为 "
            f"{lc.mtu}-{lc.overhead}={lc.max_payload}；载荷 {old} -> "
            f"{self.cur_payload}，进入第 {self.round} 轮{warn}",
        )
        return {"applied": True, "stale": False, "packet": pkt.to_dict(),
                "old_payload": old, "new_payload": self.cur_payload}

    def switch_line(self, line_key: str) -> dict:
        if line_key not in self.cfg.lines:
            raise DrillError("线路只能是 A 或 B", 400)
        if self.complete:
            raise DrillError("报表已全部确认并传完；如需再次换线演练请重置", 409)
        old_line = self.active_line
        self.active_line = line_key
        self.cur_payload = self.cfg.start_payload  # 每次换线都从起始载荷重新试发
        self.round += 1
        self._log(
            "switch",
            f"{old_line} -> {line_key} 换线：当前载荷重置为起始载荷 "
            f"{self.cur_payload}，进入第 {self.round} 轮，并立即重发未确认字节",
        )
        pkts = self.resend_missing(kind="switch")
        return {
            "active_line": self.active_line,
            "cur_payload": self.cur_payload,
            "round": self.round,
            "packets": [p.to_dict() for p in pkts],
        }

    # ---- 序列化 -------------------------------------------------------
    def state_dict(self) -> dict:
        return {
            "config": self.cfg.to_dict(),
            "active_line": self.active_line,
            "cur_payload": self.cur_payload,
            "round": self.round,
            "next_offset": self.next_offset,
            "acked_count": self.acked.count,
            "acked_ranges": [list(r) for r in self.acked.ranges],
            "complete": self.complete,
            "completed_at": self.completed_at,
            "packets": [p.to_dict() for p in self.packets],
            "events": self.events[-150:],
            "limits": {
                "max_total_bytes": MAX_TOTAL_BYTES,
                "max_packets": MAX_PACKETS,
            },
        }

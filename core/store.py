"""滚动上下文与上报节流。

两件事：
1. 记住每个会话最近几轮原文 —— 上报时要把「她为什么这么说」一起带过去，
   只发一句「有人找我」是没法判断的。
2. 冷却与每日上限 —— 对方连着追问时不要连发十条提醒。

状态落盘到 ``state.json``，重启后还能接上上一轮的上下文。
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any


class AlertStore:
    def __init__(self, path: Path, keep: int = 40) -> None:
        self.path = Path(path)
        self.keep = max(4, int(keep))
        self._sessions: dict[str, deque[dict[str, Any]]] = {}
        self._alerts: dict[str, list[float]] = {}
        self._load()

    # ---------- 落盘 ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for umo, items in (raw.get("sessions") or {}).items():
            if isinstance(items, list):
                self._sessions[umo] = deque(items[-self.keep :], maxlen=self.keep)
        for umo, stamps in (raw.get("alerts") or {}).items():
            if isinstance(stamps, list):
                self._alerts[umo] = [float(t) for t in stamps if _is_num(t)]

    def _save(self) -> None:
        payload = {
            "sessions": {k: list(v) for k, v in self._sessions.items()},
            "alerts": self._alerts,
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------- 上下文 ----------

    def append(self, umo: str, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        dq = self._sessions.get(umo)
        if dq is None:
            dq = deque(maxlen=self.keep)
            self._sessions[umo] = dq
        dq.append({"r": role, "t": text[:600], "ts": time.time()})
        self._save()

    def recent(self, umo: str, rounds: int = 6) -> list[dict[str, Any]]:
        """取最近 N 轮（一问一答算一轮）。"""
        dq = self._sessions.get(umo)
        if not dq:
            return []
        return list(dq)[-max(2, rounds * 2) :]

    def clear(self, umo: str) -> None:
        self._sessions.pop(umo, None)
        self._save()

    # ---------- 节流 ----------

    def can_alert(
        self, umo: str, *, cooldown_min: int, daily_max: int, now: float | None = None
    ) -> tuple[bool, str]:
        now = time.time() if now is None else now
        stamps = self._alerts.get(umo, [])

        if cooldown_min > 0 and stamps:
            gap = now - stamps[-1]
            if gap < cooldown_min * 60:
                left = int((cooldown_min * 60 - gap) / 60) + 1
                return False, f"冷却中，还有约 {left} 分钟"

        if daily_max > 0:
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            n = sum(
                1
                for t in stamps
                if time.strftime("%Y-%m-%d", time.localtime(t)) == today
            )
            if n >= daily_max:
                return False, f"今天已报 {n} 次，达上限"

        return True, ""

    def mark_alert(self, umo: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        stamps = self._alerts.setdefault(umo, [])
        stamps.append(now)
        # 只保留最近 30 天，避免文件无限增长
        cutoff = now - 30 * 86400
        self._alerts[umo] = [t for t in stamps if t >= cutoff]
        self._save()

    def today_count(self, umo: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        return sum(
            1
            for t in self._alerts.get(umo, [])
            if time.strftime("%Y-%m-%d", time.localtime(t)) == today
        )


def _is_num(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False

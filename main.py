"""管理员呼叫 —— 她跟别人聊到「这事得找管理员」时，主动来找你。

两条并行的触发路径：

1. **她自己叫**（首选）
   注册一个 ``notify_owner`` 工具，人格里教她：遇到无权处理的事就调用它。
   这条路径的好处是「为什么上报」由她自己写，最接近真实语义。

2. **钩子兜底**（不依赖模型听话）
   每次她给别人回完话，扫一遍「对方原话 + 她的回复」：
   对方提到隐私 / 侵权 / 举报 / 报警，或者她提到管理员 / 越权 / 上报，
   就自动把最近几轮原文推送到你的私聊。
   模型忘了调工具也没关系——只要话说到那个份上，你一定会知道。

两条路径互斥：同一轮已经由工具报过，钩子就不再重复。
另外还有冷却与每日上限，避免对方连续追问时刷屏。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.detect import (
    DEFAULT_REPLY_ESCALATE,
    DEFAULT_USER_STRONG,
    DEFAULT_USER_WEAK,
    judge,
    reached,
    split_words,
)
from .core.store import AlertStore

PLUGIN_NAME = "astrbot_plugin_owner_alert"

URGENCY_LABEL = {"low": "低", "normal": "普通", "high": "高"}


class OwnerAlertPlugin(Star):
    """把「需要管理员说明」的对话主动推送给主人。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.settings = config

        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.store = AlertStore(
            self.data_dir / "state.json",
            keep=max(8, int(config.get("history_rounds") or 6) * 2 + 4),
        )

        tz_name = "Asia/Shanghai"
        try:
            tz_name = str(self.context.get_config().get("timezone") or tz_name)
        except Exception:
            pass
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:
            self.tz = ZoneInfo("Asia/Shanghai")

        self._tasks: set[asyncio.Task] = set()

    # ---------------- 归属判断 ----------------

    def _owner_umos(self) -> set[str]:
        raw = str(self.settings.get("owner_umo") or "").strip()
        if not raw:
            return set()
        return {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}

    def _admin_ids(self) -> set[str]:
        try:
            admins = self.context.get_config().get("admins_id") or []
        except Exception:
            admins = []
        return {str(a).strip() for a in admins if str(a).strip().isdigit()}

    def is_owner_session(self, event: AstrMessageEvent) -> bool:
        """这个会话是不是主人本人。

        `owner_umo` 填了就以它为准；留空则退回「admins_id 里的 QQ 私聊」，
        这样不用手写 unified_msg_origin 也能用。
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        owners = self._owner_umos()
        if owners:
            return umo in owners
        if not event.is_private_chat():
            return False
        return str(event.get_sender_id() or "") in self._admin_ids()

    def _target_umos(self) -> list[str]:
        """提醒往哪儿发。"""
        owners = self._owner_umos()
        if owners:
            return sorted(owners)

        umos: list[str] = []
        for qq in sorted(self._admin_ids()):
            umos.append(f"default:FriendMessage:{qq}")
        return umos

    # ---------------- 组装提醒 ----------------

    def _fmt(self, ts: float | None = None) -> str:
        return datetime.fromtimestamp(ts or datetime.now().timestamp(), self.tz).strftime(
            "%H:%M"
        )

    def _transcript(self, umo: str, rounds: int) -> str:
        items = self.store.recent(umo, rounds)
        if not items:
            return "（没有可用的前文）"
        lines = []
        for it in items:
            who = "对方" if it.get("r") == "user" else "她"
            text = str(it.get("t") or "").strip().replace("\n", " / ")
            if len(text) > 160:
                text = text[:160] + "…"
            lines.append(f"{who}：{text}")
        return "\n".join(lines)

    def build_alert(
        self,
        *,
        umo: str,
        sender: str,
        user_text: str,
        reply_text: str,
        reason: str,
        urgency: str = "normal",
        score: int = 0,
    ) -> str:
        rounds = max(1, int(self.settings.get("history_rounds") or 6))

        head = [f"【有人来找你 · {self._fmt()}】", f"来自：{sender}"]
        if reason:
            head.append(f"原因：{reason}")
        if urgency != "normal":
            head.append(f"紧急度：{URGENCY_LABEL.get(urgency, urgency)}")

        body = []
        if bool(self.settings.get("include_transcript", True)):
            body.append("—— 最近的对话 ——")
            body.append(self._transcript(umo, rounds))

        tail = []
        if bool(self.settings.get("show_session_id", False)):
            tail.append(f"会话：{umo}")
        tail.append("（自动上报。看不看都行，不回也没关系。）")

        chunks = [lines for lines in (head, body, tail) if lines]
        return "\n\n".join("\n".join(c) for c in chunks)

    async def _deliver(self, text: str) -> bool:
        targets = self._target_umos()
        if not targets:
            logger.warning("[owner_alert] 没有配置 owner_umo，也没找到 admins_id，无法上报")
            return False

        ok = False
        for umo in targets:
            try:
                sent = await self.context.send_message(umo, MessageChain([Plain(text)]))
            except Exception as e:
                logger.error(f"[owner_alert] 发往 {umo} 失败: {e}")
                continue
            if sent is False:
                logger.warning(f"[owner_alert] 未找到匹配平台，{umo} 未送达")
                continue
            ok = True
        return ok

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _alert(
        self,
        event: AstrMessageEvent,
        *,
        reason: str,
        urgency: str = "normal",
        score: int = 0,
        force: bool = False,
    ) -> bool:
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return False

        if not force:
            allow, why = self.store.can_alert(
                umo,
                cooldown_min=int(self.settings.get("cooldown_minutes") or 60),
                daily_max=int(self.settings.get("daily_max") or 10),
            )
            if not allow:
                logger.info(f"[owner_alert] 跳过上报：{why}")
                return False

        name = ""
        try:
            name = str(event.get_sender_name() or "")
        except Exception:
            name = ""
        sid = str(event.get_sender_id() or "")
        sender = f"{name}（{sid}）" if name and sid else (name or sid or "未知")

        text = self.build_alert(
            umo=umo,
            sender=sender,
            user_text=str(event.message_str or ""),
            reply_text="",
            reason=reason,
            urgency=urgency,
            score=score,
        )

        ok = await self._deliver(text)
        if ok:
            self.store.mark_alert(umo)
            event.set_extra("owner_alert_sent", True)
            logger.info(f"[owner_alert] 已上报：{reason}")
        return ok

    # ---------------- 路径一：她主动调用 ----------------

    @llm_tool(name="notify_owner")
    async def notify_owner_tool(
        self, event: AstrMessageEvent, reason: str, urgency: str = "normal"
    ) -> str:
        """把当前对话上报给唯一有权处理它的人（管理员）。

        以下任一情况都要调用：
        1. 对方要求你删除、修改、隐藏聊天记录，或索取你无权给出的东西；
        2. 对方指控你、平台或管理员侵犯隐私，提到举报、投诉、起诉、报警；
        3. 对方要求见真人、要求管理员出面、要求你"给个说法"；
        4. 对方情绪已经失控，你安抚不住，且这件事会留下后果。

        调用后在对话里只需说一句「这件事我会报上去」，不要承诺结果和时间。

        Args:
            reason(string): 一句话说明为什么需要管理员介入，20 字以内
            urgency(string): low / normal / high，默认 normal
        """
        if self.is_owner_session(event):
            return "这是管理员本人的会话，不需要上报。直接回答即可。"

        if event.get_extra("owner_alert_sent"):
            return "本轮已经上报过了，不要重复。"

        urgency = str(urgency or "normal").strip().lower()
        if urgency not in URGENCY_LABEL:
            urgency = "normal"

        ok = await self._alert(
            event,
            reason=str(reason or "").strip()[:60] or "对方提出了超出权限的要求",
            urgency=urgency,
            force=False,
        )
        if ok:
            return (
                "已上报。对话里只说一句「这件事我会报上去」，"
                "不承诺管理员会回复、不给时间、不透露管理员是谁。"
            )
        return "上报已被节流或未送达。仍然按原规则回答，不要说已经通知过了。"

    # ---------------- 路径二：钩子兜底 ----------------

    @filter.on_decorating_result()
    async def on_reply(self, event: AstrMessageEvent) -> None:
        if not bool(self.settings.get("enable", True)):
            return
        if self.is_owner_session(event):
            return
        if event.get_extra("owner_alert_recorded"):
            return
        event.set_extra("owner_alert_recorded", True)

        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return

        user_text = str(event.message_str or "")
        reply_text = self._reply_text(event)

        # 先记上下文，再判定 —— 这样上报里一定包含刚发生的这一轮
        self.store.append(umo, "user", user_text)
        if reply_text:
            self.store.append(umo, "bot", reply_text)

        if event.get_extra("owner_alert_sent"):
            return

        verdict = judge(
            user_text,
            reply_text,
            user_strong=split_words(
                str(self.settings.get("user_strong_keywords") or ""), DEFAULT_USER_STRONG
            ),
            reply_escalate=split_words(
                str(self.settings.get("reply_escalate_keywords") or ""),
                DEFAULT_REPLY_ESCALATE,
            ),
            user_weak=split_words(
                str(self.settings.get("user_weak_keywords") or ""), DEFAULT_USER_WEAK
            ),
        )

        threshold = int(self.settings.get("threshold") or 3)
        if not reached(verdict, threshold):
            return

        urgency = "high" if verdict.score >= 6 else "normal"
        logger.info(
            f"[owner_alert] 命中 {verdict.score} 分：{verdict.triggered_briefly}"
        )
        self._spawn(
            self._alert(
                event,
                reason=verdict.triggered_briefly,
                urgency=urgency,
                score=verdict.score,
            )
        )

    def _reply_text(self, event: AstrMessageEvent) -> str:
        try:
            result = event.get_result()
        except Exception:
            return ""
        if not result:
            return ""
        chain = getattr(result, "chain", None)
        if isinstance(chain, str):
            return chain.strip()
        parts: list[str] = []
        for comp in chain or []:
            text = getattr(comp, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()

    # ---------------- 管理命令 ----------------

    @filter.command("上报状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看上报配置与今日计数。"""
        owners = self._target_umos()
        strong = split_words(str(self.settings.get("user_strong_keywords") or ""), DEFAULT_USER_STRONG)
        escalate = split_words(
            str(self.settings.get("reply_escalate_keywords") or ""), DEFAULT_REPLY_ESCALATE
        )

        lines = ["【管理员呼叫状态】"]
        lines.append("开关：" + ("开" if bool(self.settings.get("enable", True)) else "关"))
        lines.append("提醒发往：" + ("、".join(owners) if owners else "未配置"))
        lines.append(
            f"节流：同一会话 {int(self.settings.get('cooldown_minutes') or 60)} 分钟一次，"
            f"每天最多 {int(self.settings.get('daily_max') or 10)} 次"
        )
        lines.append(f"触发阈值：{int(self.settings.get('threshold') or 3)} 分")
        lines.append(f"对方强信号词：{len(strong)} 个")
        lines.append(f"她的外包话术词：{len(escalate)} 个")
        try:
            today = self.store.today_count(getattr(event, "unified_msg_origin", "") or "")
            lines.append(f"本会话今日已报：{today} 次")
        except Exception:
            pass
        yield event.plain_result("\n".join(lines))

    @filter.command("上报测试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_test(self, event: AstrMessageEvent):
        """立刻发一条测试提醒，验证链路是否通。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        text = self.build_alert(
            umo=umo,
            sender="测试",
            user_text="",
            reply_text="",
            reason="这是一条测试提醒，用来确认上报链路正常",
            urgency="low",
        )
        ok = await self._deliver(text)
        yield event.plain_result("已发送，去看私聊。" if ok else "发送失败，检查 owner_umo 与平台连接。")

    @filter.command("上报清空")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_clear(self, event: AstrMessageEvent):
        """清掉当前会话的上下文缓存。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        self.store.clear(umo)
        yield event.plain_result("已清空本会话的上下文缓存。")

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()

"""触发判定：什么情况算「她需要找管理员说明」。

判定分两路：

- **对方施压**（用户侧）：提到隐私、侵权、举报、投诉、报警、要删记录……
- **她往外交权**（回复侧）：说了「管理员」「平台侧」「我无权」「上报」……

任一路单独命中即可触发，因为它们各自都意味着「这件事已经超出她手里
的权限，需要本人知道」。两路同时命中时评分更高，用于在提醒里标注紧急度。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 对方直接踩到法律 / 追责层面的词：单独一个就足够触发
DEFAULT_USER_STRONG = (
    "隐私,侵权,举报,投诉,起诉,报警,网警,曝光,律师函,法院,违法,追责,"
    "删掉聊天,删除聊天,删除记录,删掉记录,不留记录,聊天记录,举证,证据,"
    "未成年,骚扰,开盒,人肉,"
    # 对方开始点名要找「上头的人」时，事态同样已经升级
    "管理员,找你们管理,负责人,你们公司,开发者,客服,真人,给个说法"
)

# 她把自己的无权处理推给外部权威时常用的话术
DEFAULT_REPLY_ESCALATE = (
    "管理员,管理员们,平台侧,平台方,平台层面,越权,权限不足,我无权,"
    "不在我的权限,超出我的权限,上报,报上去,备案,客服,开发者,所有者,"
    "真人,人工处理,找平台"
)

# 只是可疑，需要和其他信号叠加
DEFAULT_USER_WEAK = (
    "删掉,删除,清空,监控,监视,偷看,后台,日志,存档,截图,权限,"
    "你是谁,你是不是机器人,举报你"
)


def split_words(raw: str, default: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        text = default
    words = [w.strip() for w in re.split(r"[,，;；\s]+", text) if w.strip()]
    # 长的排前面，避免短词先命中
    return sorted(set(words), key=len, reverse=True)


@dataclass
class Verdict:
    """一次判定结果。"""

    score: int = 0
    user_hits: list[str] = field(default_factory=list)
    reply_hits: list[str] = field(default_factory=list)
    weak_hits: list[str] = field(default_factory=list)

    @property
    def triggered_briefly(self) -> str:
        """一句话说明触发原因，用在提醒正文里。"""
        parts = []
        if self.user_hits:
            parts.append("对方提到「" + "、".join(self.user_hits[:3]) + "」")
        if self.reply_hits:
            parts.append("她提到「" + "、".join(self.reply_hits[:2]) + "」")
        if self.weak_hits and not parts:
            parts.append("对方提到「" + "、".join(self.weak_hits[:3]) + "」")
        return "；".join(parts) or "（无明显关键词）"


def judge(
    user_text: str,
    reply_text: str,
    *,
    user_strong: list[str],
    reply_escalate: list[str],
    user_weak: list[str],
    threshold: int = 3,
) -> Verdict:
    """给一轮对话打分。

    单路命中即可达标：强词 +3、她的外包话术 +3、弱词 +1。
    这样「对方说隐私」和「她说找管理员」各自都能独立触发。
    """
    u = str(user_text or "")
    r = str(reply_text or "")

    v = Verdict()

    # 只在没有以「你/我」开头的解释性语境里算，避免她自己解释平台规则时刷屏
    v.user_hits = [w for w in user_strong if w in u]
    v.reply_hits = [w for w in reply_escalate if w in r]
    v.weak_hits = [w for w in user_weak if w in u and w not in v.user_hits]

    if v.user_hits:
        v.score += 3
    if v.reply_hits:
        v.score += 3
    v.score += min(len(v.weak_hits), 2)

    return v


def reached(v: Verdict, threshold: int) -> bool:
    return v.score >= max(1, int(threshold))

"""知識回想 —— 開一張卡時，把過去做過的決策與記下的風險撈出來。

**這是 DevLoop 相對於「Claude 在聊天視窗給報告」唯一真正的差異。**
決策存進資料庫只解決一半；沒有人去讀它，下一張卡拿到的還是空白脈絡。

刻意不做語意檢索：先用關鍵字與指派關係，看實際命中率再決定要不要上 embedding。
"""

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.db.models import Card, Decision, Risk

_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}")
_CJK = re.compile(r"[一-鿿]{2,}")
_STOPWORDS = {"the", "and", "for", "with", "phase", "week", "task"}


def terms_of(text: str, labels: list[str] | None = None) -> set[str]:
    """從卡片標題與標籤抽出可比對的詞。

    中文沒有空白，所以取 2–4 字的連續片段；英文取長度 ≥3 的識別字。
    只看標題與標籤，不看整段描述 —— 描述太長，雜訊會蓋過訊號。
    """
    terms = {label.strip().lower() for label in (labels or []) if label.strip()}
    terms |= {w.lower() for w in _ASCII.findall(text) if w.lower() not in _STOPWORDS}
    for run in _CJK.findall(text):
        for size in (2, 3, 4):
            for i in range(len(run) - size + 1):
                terms.add(run[i : i + size])
    return terms


@dataclass
class Hit:
    kind: str  # decision / risk
    source_card: str
    text: str
    reason: str
    score: int = 0


@dataclass
class Recall:
    hits: list[Hit] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.hits)

    @property
    def decisions(self) -> list[Hit]:
        return [h for h in self.hits if h.kind == "decision"]

    @property
    def risks(self) -> list[Hit]:
        return [h for h in self.hits if h.kind == "risk"]

    def as_prompt_block(self) -> str:
        """沒有命中就回空字串 —— 不要在提示詞裡塞一個空白段落。"""
        if not self.hits:
            return ""
        lines = ["", "## 這個專案過去的決定與記下的風險", ""]
        for hit in self.hits:
            label = "決策" if hit.kind == "decision" else "風險"
            lines.append(f"- [{label}｜來自 {hit.source_card}｜{hit.reason}] {hit.text}")
        lines += [
            "",
            "寫報告時：**第 2 節「現況」必須明確處理上面每一條**——",
            "說明它對這張卡成不成立、要沿用還是推翻。不要重新討論已經定案的東西，",
            "也不要假裝那些風險不存在。",
        ]
        return "\n".join(lines)


# 純中文片段容易誤中，所以要兩個以上才算數；標籤與英文識別字一個就夠specific
_MIN_SCORE_CJK_ONLY = 2


def _is_cjk(term: str) -> bool:
    return bool(_CJK.fullmatch(term))


def _score(terms: set[str], text: str) -> tuple[int, list[str]]:
    lowered = text.lower()
    matched = [t for t in terms if t in lowered]
    if not matched:
        return 0, []
    if all(_is_cjk(t) for t in matched) and len(matched) < _MIN_SCORE_CJK_ONLY:
        return 0, []
    return len(matched), sorted(matched, key=len, reverse=True)[:3]


def recall(session: Session, project: str, card: Card, limit: int = 8) -> Recall:
    terms = terms_of(card.title, list(card.labels or []))
    keys = {c.key: c for c in session.scalars(select(Card).where(Card.project == project)).all()}

    hits: list[Hit] = []

    # 1. 直接指派給這張卡的風險 —— 最強的訊號，不需要比對關鍵字
    for risk in session.scalars(
        select(Risk).where(Risk.owner_card_key == card.key, Risk.resolved_at.is_(None))
    ).all():
        source = next((k for k, c in keys.items() if c.id == risk.card_id), "?")
        hits.append(Hit("risk", source, risk.text, "指派給這張卡", score=99))

    assigned = {h.text for h in hits}

    # 2. 關鍵字命中的決策
    for decision in session.scalars(select(Decision)).all():
        if decision.card_id == card.id:
            continue
        origin = next((k for k, c in keys.items() if c.id == decision.card_id), None)
        if origin is None:
            continue
        score, why = _score(terms, decision.text)
        if score:
            hits.append(Hit("decision", origin, decision.text, "、".join(why), score))

    # 3. 關鍵字命中的未解風險
    for risk in session.scalars(select(Risk).where(Risk.resolved_at.is_(None))).all():
        if risk.card_id == card.id or risk.text in assigned:
            continue
        origin = next((k for k, c in keys.items() if c.id == risk.card_id), None)
        if origin is None:
            continue
        score, why = _score(terms, risk.text)
        if score:
            hits.append(Hit("risk", origin, risk.text, "、".join(why), score))

    hits.sort(key=lambda h: (-h.score, h.source_card))
    return Recall(hits=hits[:limit])

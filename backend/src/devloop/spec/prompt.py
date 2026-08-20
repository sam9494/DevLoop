"""給 Claude Code 的指示。

報告的八節結構是固定的（docs/prd.html §5）—— 順序與標題不讓模型自由發揮，
因為 Sam 每張卡都要在同一個位置找到「關鍵決策點」。
"""

SECTIONS = [
    (1, "目標"),
    (2, "現況"),
    (3, "關鍵決策點"),
    (4, "檔案清單"),
    (5, "介面草稿"),
    (6, "測試策略"),
    (7, "驗收對照"),
    (8, "風險與技術債"),
]

_SECTION_LIST = "\n".join(f"  {n}. {title}" for n, title in SECTIONS)

TEMPLATE = """你要為一張 Jira 卡寫「動工前的實作報告」，給一位資深工程師審核。

卡號：{key}
標題：{title}
卡片內容：
{description}
{knowledge}
請先實際讀這個專案的程式碼與設定（你的工作目錄就是專案根目錄），
用**現況**寫報告，不要用規格書上的理想狀態。

輸出**只有一個 JSON 物件**，不要有其他文字。形狀如下：

{{
  "sections": [
    {{"n": 1, "title": "目標", "body_md": "markdown 內容"}}
    // 八節都要有，順序與標題固定：
{sections}
  ],
  "risks": [
    {{"slug": "kebab-case", "text": "這張卡會留下什麼風險或技術債",
      "owner_card": "真正該處理它的卡號，就是這張卡就填 null"}}
  ],
  "questions": [
    {{
      "slug": "kebab-case-語義名稱",
      "type": "single" | "boolean" | "text",
      "prompt": "要問的問題",
      "section_n": 3,
      "options": [
        {{"value": "a", "label": "選項標題", "detail": "這樣做會怎樣",
          "cost": "代價是什麼", "recommended": true}},
        {{"value": "none", "label": "以上皆非 —— 我要的是別的"}}
      ]
    }}
  ]
}}

問題的規則：
- 只問**真的需要人拍板**的取捨，不要問你自己查得到答案的事。通常 2 到 5 題。
- 每題掛在最相關的那一節底下（section_n），讀到那裡就答得到。
- 單選題**必須**有一個 value 為 "none" 的「以上皆非」選項 —— 選項沒涵蓋到時要有出口。
- 每個選項都要寫出 cost（代價），只寫好處等於沒給人判斷依據。
- 建議的選項標 recommended: true，一題最多一個。
- slug 要能跨版本沿用：描述問題本身（source-choice），不要用編號（q1）。

風險的規則：
- 只列**做完這張卡之後仍然存在**的風險與技術債，不要列已經解掉的。
- `owner_card` 是關鍵：如果這個風險真正該由另一張卡處理，就填那張卡的卡號。
  那張卡開的時候會被強迫面對它。不確定就填 null。
{revision}"""

REVISION_NOTE = """
這是改版。上一版的第 {section_n} 節被要求修改，理由：
{reason}

請重出完整的八節與問題。**沒有變動的問題請沿用原本的 slug**，
這樣之前答過的答案才對得回來。已經答過而且沒有被推翻的問題不要重問。
"""


def build_prompt(
    key: str,
    title: str,
    description: str,
    knowledge: str = "",
    revision_section: int | None = None,
    revision_reason: str = "",
) -> str:
    revision = ""
    if revision_section is not None:
        revision = REVISION_NOTE.format(section_n=revision_section, reason=revision_reason)
    return TEMPLATE.format(
        key=key,
        title=title,
        description=description or "（這張卡沒有內容）",
        knowledge=knowledge,
        sections=_SECTION_LIST,
        revision=revision,
    )

# DevLoop 的設計與決策紀錄

原本放在 `JobHunting/SpecGate/`，2026-08-20 併進本專案。
SpecGate 是這個系統最早的名字與 prototype 階段。

## 產品

| 檔案 | 是什麼 |
|---|---|
| [`prd.html`](prd.html) | 產品需求文件 v0.1 —— 問題、流程、八節報告結構、資料模型、三階段架構。**閘門的規格出處**（P0 是 approve / 要求修改兩顆鈕、題目穿插在內文、五分鐘完成） |

## Prototype（先於實作，用來驗證流程本身）

| 檔案 | 是什麼 |
|---|---|
| [`prototype/KAN-15.html`](prototype/KAN-15.html) | 單卡閘門原型。**現在服務的 CSS 與互動邏輯都是從這裡搬的** —— 題目樣式、進度條、以上皆非必填 |
| [`prototype/devloop.html`](prototype/devloop.html) | 六階段閉環的動畫原型（intake → spec → frozen → build → deploy → knowledge）。第一刀只做到 frozen，④⑤⑥ 仍是願景 |

## 決策紀錄

每一輪都是「HTML 頁面出題 → Sam 勾選 → 存成 JSON」。HTML 是題目與樣本，JSON 是答案。

| 輪次 | 題目頁 | 答案 | 決定了什麼 |
|---|---|---|---|
| TECH-001 | [`decisions/TECH-001.html`](decisions/TECH-001.html) | [`TECH-001.json`](decisions/TECH-001.json) | 做 DevLoop 全閉環、資料進資料庫、Neo4j 存圖、自建 Jira client、slug id、題目不可跳過、手機延後 |
| TECH-002 | [`decisions/TECH-002.html`](decisions/TECH-002.html) | [`TECH-002.json`](decisions/TECH-002.json) | Postgres 主庫 + Neo4j 圖層、背景佇列呼叫本地 Claude Code、Neo4j 走 Docker、**服務跑本機**、加「以上皆非」 |
| IMPL-01 | [`decisions/IMPL-01.html`](decisions/IMPL-01.html) | [`IMPL-01.json`](decisions/IMPL-01.json) | 第一刀做到「凍結規格」、語言與 repo 交由 Claude 決定（選了 Python + 獨立 repo）、Neo4j 第一刀就接、Jinja 渲染、`acceptEdits` 權限 |

**看答案 JSON 時注意**：`choice: null` 加上 `note` 不代表跳過 —— 那是「選項沒涵蓋到我要的」，備註才是真正的答案。TECH-001 有四題是這樣，整個架構是那四句話決定的。

## 這些紀錄的用途

1. 三週後回頭看程式碼時，查得到當初排除了什麼、為什麼排除
2. 面試被問技術選型時，是現成的素材
3. DevLoop 自己產的規格報告，凍結後會落成同樣形狀的 JSON（`/cards/{key}/review.json`）—— 這三份是那個格式的第一批樣本

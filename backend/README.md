# DevLoop — 閉環開發系統

一張 Jira 卡進來 → 服務呼叫本地 Claude Code 產規格報告 → Sam 讀與答題 → 凍結 → 決策落庫並進圖譜 → 下一張卡吃得到。

設計與決策紀錄在 `../../SpecGate/`（PRD、prototype、三輪選型 JSON）。

## 兩個 Jira 專案別搞混

| 專案 | 是什麼 |
|---|---|
| **DEV**（DevLoop） | DevLoop 自己的開發卡。<https://swallowhouse.atlassian.net/jira/software/projects/DEV/boards/3/backlog> |
| **KAN**（CareerBuddy） | 被 DevLoop 管理的產品線 —— `/settings` 裡填的「專案代號」是這個 |

換句話說：DevLoop 的卡開在 DEV，DevLoop 讀的卡來自 KAN。

## 跑起來

```bash
cp .env.example .env
# 產一把加密金鑰填進 DEVLOOP_SECRET_KEY：
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
make up                   # 只有資料庫進容器：postgres 5433 / neo4j 7687
make migrate
make dev                  # 服務跑在本機（才叫得到本地的 claude）
```

Jira 憑證**不放在 .env** —— 開 <http://localhost:8100/settings> 填，存進資料庫前會加密。

```bash
make ci                   # ruff + mypy strict + import-linter + pytest
```

## 主流程

```
/            卡片列表 —— 從 Jira 同步、對某張卡按「產生規格」
             ↓ worker 在背景跑 claude -p，產出八節報告與題目
/cards/{key} 規格頁 —— 讀報告、答題（題目穿插在相關段落底下）
             ↓ 必答題答完才解鎖
             凍結 → 版本鎖 v1.0、答案萃成決策寫進圖、Jira 卡移到「進行中」
/cards/{key}/review.json   決策紀錄，格式與 SpecGate 那幾份一致
```

沒有已驗證的 Jira 連線時，`/` 一律 307 導到 `/settings` —— 沒有憑證就拉不到卡，
主流程整條空轉，先擋在門口比讓人看到空列表誠實。

## 憑證怎麼保管

`connections` 表一列一個人（`owner_key`），token 以 Fernet 加密後存放，
金鑰在 `DEVLOOP_SECRET_KEY` 環境變數 —— 跟資料庫分開，只撈到資料庫解不開。
明文只在三個地方短暫存在：使用者送出的表單、驗證時的 HTTP 標頭、寫入前的加密呼叫。
頁面永遠只顯示遮罩（`••••••••1234`），log 不印。金鑰沒設時憑證操作直接失敗，
不會退化成明文存放。

## 為什麼服務不進容器

第 2 步要用 subprocess 執行本機的 `claude`。容器裡沒有 Claude Code、沒有登入狀態、
也看不到被管理的專案目錄，所以服務必須跑在本機。

## docker compose 的專案名寫死了

`docker-compose.yml` 第一行是 `name: devloop`。compose 預設拿目錄名當專案名，
而 JobRadar 的資料夾也叫 `backend` —— 沒有這一行，兩個專案會互相替換對方的容器、
共用同一個 volume。2026-08-19 實際踩過一次。

## 模組邊界

靠 import-linter，不靠微服務（抄 JobRadar 的紀律）：

- `core` 是葉子，不 import 任何其他模組
- `jira` / `runner` / `graph` 三個外部接點彼此獨立
- `spec.schema` 是純資料形狀，不碰外部世界

## 資料真相

Postgres 是唯一真相。Neo4j 是 `edges` 表的投影，砍掉可以整個重建。

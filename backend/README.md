# DevLoop — 閉環開發系統

一張 Jira 卡進來 → 服務呼叫本地 Claude Code 產規格報告 → Sam 讀與答題 → 凍結 → 決策落庫並進圖譜 → 下一張卡吃得到。

設計與決策紀錄在 [`../docs/`](../docs/README.md)：PRD、兩份 prototype、三輪選型的題目頁與答案 JSON。

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

Jira 憑證與**被管理的專案**都不放在 .env —— 開 <http://localhost:8100/settings> 填：
站台、帳號、API token（加密存放）、專案代號、專案根目錄。
換一個要管理的專案（例如從 DEV 切到 KAN）只要改這一頁，不用編輯檔案也不用重啟。

```bash
make ci                   # ruff + mypy strict + import-linter + pytest
                          # 測試跑在獨立的 devloop_test 資料庫，不碰開發資料
make rebuild-graph        # 清空 Neo4j 並從 Postgres 的 edges 表重建
```

## 主流程

```
/            卡片列表 —— 從 Jira 同步、對某張卡按「產生規格」
             每張卡顯示「🔗 知識庫命中 N 條」：過去做過的決策與記下的風險
             ↓ worker 在背景跑 claude -p，產出八節報告與題目
/cards/{key} 規格頁 —— 讀報告、答題（題目穿插在相關段落底下）
             ↓ 兩顆鈕：
             要求修改 → 退回某一節重產，已答過的題目依 slug 帶到新版
             凍結     → 版本鎖 v1.0、答案萃成決策、風險存成 Risk 節點、
                        寫進 edges 與 Neo4j、Jira 卡移到「進行中」
```

## 閉環怎麼閉的

報告會列出「做完這張卡之後仍然存在的風險」，每條可以指定 `owner_card` ——
**真正該處理它的是哪張卡**。那張卡開的時候，風險會出現在提示詞裡，報告的
第 2 節被要求明確處理它。

實測：KAN-16 的報告記下「LLM client 若寫在 enrichment 裡，KAN-17 的 embedding
會長出第二套成本帳」並指派給 KAN-17 —— KAN-17 開卡時就命中了它。

沒有這一層，那句話會消失在聊天記錄裡。
/cards/{key}/review.json   決策紀錄，格式與 docs/decisions/*.json 一致
```

沒有已驗證的 Jira 連線時，`/` 一律 307 導到 `/settings` —— 沒有憑證就拉不到卡，
主流程整條空轉，先擋在門口比讓人看到空列表誠實。

## 一次規格產生要多少

實測（KAN-16，讀完整個 JobRadar backend）：**US$1.68 / 39 turns / 7 分鐘**。
空轉一句話是 US$0.09 —— 那是系統提示快取的地板，不是實際工作的價錢。
每個 job 的成本都落在 `jobs.cost_usd`，卡片列表顯示「今日已用 / 上限」。
超過上限就不再排新工作；執行中的工作可以按「中止」——
那會把整個行程群組殺掉，不是只改狀態。

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

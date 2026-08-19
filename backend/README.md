# DevLoop — 閉環開發系統

一張 Jira 卡進來 → 服務呼叫本地 Claude Code 產規格報告 → Sam 讀與答題 → 凍結 → 決策落庫並進圖譜 → 下一張卡吃得到。

設計與決策紀錄在 `../../SpecGate/`（PRD、prototype、三輪選型 JSON）。

## 跑起來

```bash
cp .env.example .env      # 填 DEVLOOP_JIRA_TOKEN
make up                   # 只有資料庫進容器：postgres 5433 / neo4j 7687
make migrate
make dev                  # 服務跑在本機（才叫得到本地的 claude）
make ci                   # ruff + mypy strict + import-linter + pytest
```

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

# #448 聊天記錄檢索：設計審查實驗

這些是[#448 任務書](../../Yuki-448聊天记录检索增强任务书-2026-10-16.md)第三版的最小實驗，用來檢查設計是否自洽，不是產品驗收。全部使用合成資料或最小表，不讀取真實聊天、不連網、不呼叫模型或 QQ。E9 匯入倉庫的 `qq_ai_bot.persistence.database.Database`，E11 執行倉庫 `PROJECTION_TRIGGERS_0119` 的觸發器 SQL；其餘只用 Python 標準庫與 `sqlite3`（E3 另用 SQLAlchemy）。

## 檔案

| 檔案 | 內容 |
| --- | --- |
| `chat_search_probe.py` | 實驗腳本 |
| `results.json` | 本機完整執行的原始輸出（Apple M4、Python 3.12.7、`unicodedata` 15.0.0、SQLite 3.45.3、SQLAlchemy 2.0.51、aiosqlite 0.22.1） |

## 重現

在倉庫根目錄、以倉庫 `.venv`（已安裝 `qq_ai_bot`）執行：

```bash
PROBE_CPU="<cpu>" .venv/bin/python -I \
  docs/architecture/experiments/chat-search-448-2026-10-16/chat_search_probe.py \
  --workdir /tmp/chat-search-probe --out /tmp/chat-search-probe/results.json
```

預設參數：`--messages 200000 --queries 400 --repeats 30 --enqueue-rows 1000000 --normalization-samples 200000 --paging-trials 300`。本機完整執行約 65 秒。

所有隨機選擇都有固定種子，同一 Python、Unicode 與 SQLite 版本重跑時，計數類結果相同；耗時隨機器而異。

## 實驗與 `results.json` 的對應

| 編號 | 問題 | `results.json` 鍵 |
| --- | --- | --- |
| E1 | 現行 trigram 對短語、兩字詞、`NOT`、繁簡、全形的實際行為 | `trigram_semantics` |
| E2 | 各索引方案是否滿足候選完整性；索引大小、建置與寫入成本、延遲；高亮偏移；v0 定義的完整性 | `index_options`、`highlight_examples`、`v0_invariant` |
| E3 | 時間參數綁定是否造成 8 小時偏移 | `time_binding` |
| E4 | contentless-delete 表支援的操作；遷移中排入全部事件的持鎖成本 | `contentless_delete_operations`、`enqueue_write_lock` |
| E5 | `bm25()` 的方向、對無關插入的敏感度、累加偏差 | `bm25` |
| E6 | 證據組演算法在審查反例、重疊、三需求、遞移鏈上的結果 | `grouping` |
| E7 | 分頁推進：第二版停滯的重現；本版在隨機資料、各種預算與兩個方向上的終止、不重不漏與超支 | `second_draft_scan_no_progress`、`paging_engine` |
| E8 | 佇列推進：第二版停滯；本版的過期列退出、批量 1、處理中再入隊、`DISTINCT` 掃描 | `queue` |
| E9 | 一頁內讀快照：讀索引與讀佇列之間有索引器提交時是否漏查 | `snapshot` |
| E10 | 正規化：審查反例、等價對、分段與整串定義的一致性、高亮區間、安全起點邊界性質 | `normalization` |
| E11 | `prompt_source_revision` 在插入、ASR 更新、刪除、floor 變化時的行為；審查的重複反例 | `relevance_revision` |
| E12 | 空庫與兩條建庫路徑、第一條消息、回填、損壞重建、回填與重建的競爭 | `lifecycle` |
| E13 | 插件單頁回傳與內部續頁 | `plugin` |

## 口徑

- 資料：E2 使用 `generate(200000, seed=448)`，詳見腳本與任務書 §2.4；其他實驗的資料在各函式內。
- 正確答案：一律以任務書 §6.1 的整串 `fold`（腳本中的 `fold_whole`）暴力比對，不經分段實作 `normalize` 或索引。
- 計時：預熱 1 次，重複 30 次，取中位數與第 95 百分位；單執行緒、rollback journal。E9 使用倉庫 `Database` 的 WAL 設定。
- 大小：`VACUUM` 後的檔案大小減去只含原表時的大小。
- 合成資料不代表真實群聊分佈，延遲只作方案間的相對比較。
- E2 的「500 列重建索引寫交易」在已有 20 萬列的索引上測得。
- E4 的持鎖測量使用只有兩欄的窄表，是真實遷移的下限。
- E7、E13 是 Python 內的機制模擬，不經 SQLite，只驗證推進規則；E8、E12 使用與任務書 §7 同構的最小表，不是 Yuki 的真實表。
- E10 的安全起點邊界性質對 Unicode 15.0.0 全部已指派碼位中判定為安全起點者，各接在 16 種前文之後檢查。
- 第二版的分頁模擬（舊 `paging` 鍵，「最密集需求定下界」）已由 E7 取代並自腳本刪除。

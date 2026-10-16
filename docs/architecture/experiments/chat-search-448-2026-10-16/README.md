# #448 聊天記錄檢索：設計審查實驗

這些是[#448 任務書](../../Yuki-448聊天记录检索增强任务书-2026-10-16.md)第四版的最小實驗，用來檢查設計是否自洽，不是產品驗收。全部使用合成資料或最小表，不讀取真實聊天、不連網、不呼叫模型或 QQ。E9 匯入倉庫的 `qq_ai_bot.persistence.database.Database`，E11 執行倉庫 `PROJECTION_TRIGGERS_0119` 的觸發器 SQL；其餘只用 Python 標準庫與 `sqlite3`（E3 另用 SQLAlchemy）。

## 檔案

| 檔案 | 內容 |
| --- | --- |
| `chat_search_probe.py` | 實驗腳本 |
| `results.json` | 本機完整執行基線及本輪 E7／E14 定向重跑的輸出；其餘保留提交 `27854baa` 的結果（Apple M4、Python 3.12.7、`unicodedata` 15.0.0、SQLite 3.45.3、SQLAlchemy 2.0.51、aiosqlite 0.22.1） |

## 重現

在倉庫根目錄、以倉庫 `.venv`（已安裝 `qq_ai_bot`）執行：

```bash
PROBE_CPU="<cpu>" .venv/bin/python -I \
  docs/architecture/experiments/chat-search-448-2026-10-16/chat_search_probe.py \
  --workdir /tmp/chat-search-probe --out /tmp/chat-search-probe/results.json
```

預設參數：`--messages 200000 --queries 400 --repeats 30 --enqueue-rows 1000000 --normalization-samples 200000 --paging-trials 300`。本機完整執行約 65 秒。

所有隨機選擇都有固定種子，同一 Python、Unicode 與 SQLite 版本重跑時，計數類結果相同；耗時隨機器而異。

本輪只影響 E14，另重跑 E7 確認既有計價規則不變。可在倉庫根目錄定向重現（約 6 秒，不重建其餘索引或覆寫結果檔）：

```bash
.venv/bin/python -B - <<'PY'
import importlib.util
import json
from pathlib import Path

path = Path("docs/architecture/experiments/chat-search-448-2026-10-16/chat_search_probe.py")
spec = importlib.util.spec_from_file_location("chat_search_probe", path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
saved = json.loads(path.with_name("results.json").read_text())
for key, result in (
    ("paging_engine", probe.probe_paging_engine(seed=12, trials=300)),
    ("relevance_recompute_cost", probe.probe_relevance_recompute_cost()),
):
    assert json.loads(json.dumps(result)) == saved[key], key
    print(key, "PASS")
PY
```

## 實驗與 `results.json` 的對應

| 編號 | 問題 | `results.json` 鍵 |
| --- | --- | --- |
| E1 | 現行 trigram 對短語、兩字詞、`NOT`、繁簡、全形的實際行為 | `trigram_semantics` |
| E2 | 各索引方案是否滿足候選完整性；索引大小、建置與寫入成本、延遲；高亮偏移；v0 定義的完整性 | `index_options`、`highlight_examples`、`v0_invariant` |
| E3 | 時間參數綁定是否造成 8 小時偏移 | `time_binding` |
| E4 | contentless-delete 表支援的操作；遷移中排入全部事件的持鎖成本 | `contentless_delete_operations`、`enqueue_write_lock` |
| E5 | `bm25()` 的方向、對無關插入的敏感度、累加偏差 | `bm25` |
| E6 | 證據組演算法在審查反例、重疊、三需求、遞移鏈上的結果 | `grouping` |
| E7 | 分頁推進與工作量：第二版停滯的重現；本版在隨機資料、各種預算（含 1、來源數大於預算、窗口已用完預算）與兩個方向上的終止、不重不漏；每頁計價是否超過 §6.7 的上界、實際列數是否超過計價 | `second_draft_scan_no_progress`、`paging_engine` |
| E8 | 佇列推進與索引器競爭：第二版停滯；本版的過期列退出、批量 1；經觸發器初始入隊後，讀取與寫入之間更新 ASR（新項目保留、最新內容可搜）或刪除事件（直接查詢索引的孤兒列，無條件寫入與條件寫入對照）；`DISTINCT` 掃描 | `queue` |
| E9 | 一頁內讀快照：讀索引與讀佇列之間有索引器提交時是否漏查 | `snapshot` |
| E10 | 正規化：審查反例、等價對、分段與整串定義的一致性、高亮區間、安全起點邊界性質 | `normalization` |
| E11 | `prompt_source_revision` 在插入、ASR 更新、刪除、floor 變化時的行為；審查的重複反例 | `relevance_revision` |
| E12 | 空庫與兩條建庫路徑、第一條消息、回填、損壞重建、回填與重建的競爭 | `lifecycle` |
| E13 | 插件單頁回傳與內部續頁 | `plugin` |
| E14 | `relevance` 範圍發現與凍結重算：實際 keyset SQL、預算檢查點、原文核驗及封頂距離查詢。發現範圍才取輸出邊界，W = 首頁 `charged`；固定範圍只按候選計價，不查邊界、不計 b。保留 4 個完整範圍案例；新增 chunk = 25、B = 1171 的反例，以及 48 個隨機資料集 × 3 預算 × 3 chunk，含跨範圍窗口、不同重讀批量、索引進度與排序鍵續頁 | `relevance_recompute_cost`；新增檢查在其 `budgeted_ranges` |

## 口徑

- 資料：E2 使用 `generate(200000, seed=448)`，詳見腳本與任務書 §2.4；其他實驗的資料在各函式內。
- 正確答案：一律以任務書 §6.1 的整串 `fold`（腳本中的 `fold_whole`）暴力比對，不經分段實作 `normalize` 或索引。
- 計時：預熱 1 次，重複 30 次，取中位數與第 95 百分位；單執行緒、rollback journal。E9 使用倉庫 `Database` 的 WAL 設定。
- 大小：`VACUUM` 後的檔案大小減去只含原表時的大小。
- 合成資料不代表真實群聊分佈，延遲只作方案間的相對比較。
- E2 的「500 列重建索引寫交易」在已有 20 萬列的索引上測得。
- E4 的持鎖測量使用只有兩欄的窄表，是真實遷移的下限。
- E7、E13 是 Python 內的機制模擬，不經 SQLite，只驗證推進與計價規則；E7 的計價模型見腳本 `sim_engine`，規則未改。E8、E12、E14 使用與任務書 §7 同構的最小表，不是 Yuki 的真實表。
- E14 的 `range_page(budget=...)` 發現排名範圍，按 §6.7 確定候選範圍、輸出下界與 W；不帶 budget 時只重算已知範圍。後者可能多一個空探測批次，但沒有邊界 SQL，計價與批數無關。`charged`、`W` 是計價；`total_rows` 是來源＋掃描＋核驗列數；`search_rows` 再加上實際邊界與距離列數，兩種口徑分開。狀態列等固定控制讀取不屬於候選搜尋計價。
- E14 不以全會話掃描求 pos()：先枚舉最小覆蓋窗口，再以最多 M＋1 列的 SQL 求真實消息距離。隨機實驗的原文 oracle 來自既有合成資料，在搜尋以外建立；所有時間跨度設為 0，本輪不驗證時間排序、取消、權限或實際 runtime 游標裝配。
- `budgeted_ranges.stats` 記錄 433 組設定、3,556 個排名範圍、4,245 次重算與 2,607 組輸出；每次檢查計價上界、實際搜尋列數、分來源候選不增、重算無邊界查詢、排序鍵接續、窗口上界、游標進展及與原文 oracle 不重不漏。任一不符會觸發斷言，阻止寫入成功結果。
- E10 的安全起點邊界性質對 Unicode 15.0.0 全部已指派碼位中判定為安全起點者，各接在 16 種前文之後檢查。
- 第二版的分頁模擬（舊 `paging` 鍵，「最密集需求定下界」）已由 E7 取代並自腳本刪除。

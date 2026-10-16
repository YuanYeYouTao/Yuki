# #448 聊天記錄檢索：設計審查實驗

這些是[#448 任務書](../../Yuki-448聊天记录检索增强任务书-2026-10-16.md)第二版的最小實驗，用來檢查設計是否自洽，不是產品驗收。全部使用合成資料，不讀取真實聊天、不連網、不使用 Yuki 程式碼。

## 檔案

| 檔案 | 內容 |
| --- | --- |
| `chat_search_probe.py` | 實驗腳本（Python 標準庫與 `sqlite3`；E3 若可匯入 SQLAlchemy 則額外核對） |
| `results.json` | 本機完整執行的原始輸出（Apple M4、Python 3.12.7、SQLite 3.45.3） |

## 重現

```bash
PROBE_CPU="<cpu>" .venv/bin/python -I \
  docs/architecture/experiments/chat-search-448-2026-10-16/chat_search_probe.py \
  --workdir /tmp/chat-search-probe --messages 200000 --queries 400 --repeats 30 \
  --enqueue-rows 1000000 --out /tmp/chat-search-probe/results.json
```

所有隨機選擇都有固定種子，同一 Python 與 SQLite 版本重跑時，計數類結果（漏召回、候選數、證據組、分頁迭代）相同；耗時隨機器而異。

## 實驗與 `results.json` 的對應

| 編號 | 問題 | `results.json` 鍵 |
| --- | --- | --- |
| E1 | 現行 trigram 對短語、兩字詞、`NOT`、繁簡、全形的實際行為 | `trigram_semantics` |
| E2 | 各索引方案是否滿足候選完整性；索引大小、建置與寫入成本、延遲；高亮偏移；v0 定義的完整性 | `index_options`、`highlight_examples`、`v0_invariant` |
| E3 | 時間參數綁定是否造成 8 小時偏移 | `time_binding` |
| E4 | contentless-delete 表支援的操作；遷移中排入全部事件的持鎖成本 | `contentless_delete_operations`、`enqueue_write_lock` |
| E5 | `bm25()` 的方向、對無關插入的敏感度、累加偏差 | `bm25` |
| E6 | 證據組演算法在審查反例、重疊、三需求、遞移鏈上的結果 | `grouping` |
| E7 | 「最密集需求定下界」的掃描能否找到罕見命中 | `paging` |

## 口徑

- 資料：`generate(200000, seed=448)`，詳見腳本與任務書 §2.4。
- 正確答案：以任務書 §6.1 的 `normalized_v1` 定義暴力比對。
- 計時：預熱 1 次，重複 30 次，取中位數與第 95 百分位；單執行緒、rollback journal。
- 大小：`VACUUM` 後的檔案大小減去只含原表時的大小。
- 合成資料不代表真實群聊分佈，延遲只作方案間的相對比較。
- E2 的「500 列重建索引寫交易」在已有 20 萬列的索引上測得。
- E4 的持鎖測量使用只有兩欄的窄表，是真實遷移的下限。
- E7 是 Python 內的機制模擬，不經 SQLite，只驗證推進規則。
- `results.json` 產生後，腳本只做了 `ruff` 要求的格式與寫法調整（格式化、f-string、閉包綁定）；以小規模參數重跑，計數類結果與調整前相同。

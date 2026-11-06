# ytmd — YouTube 頻道逐字稿 → 單一 markdown

通用版：任何 YouTube 頻道都能跑，不再綁定 @drbergchinese。
每個頻道一個獨立工作區 `channels/<slug>/`，互不干擾。

## 快速開始

```bash
# 最近 30 天（預設），抓 zh-Hant 字幕 + 潤色成簡體中文
python3 ytmd.py --channel "@drbergchinese"

# 只要純文字檔（.txt）
python3 ytmd.py --channel "@drbergchinese" --format txt
python3 ytmd.py --channel "@drbergchinese" --format both      # md + txt 都出

# 任何頻道、任何語言
python3 ytmd.py --channel "@hubermanlab" --days 7 --lang en --polish source
python3 ytmd.py --channel "https://www.youtube.com/@somechannel" --all          # 全頻道
python3 ytmd.py --channel "@somechannel" --days 90 --polish zh-hant            # 繁體潤色
python3 ytmd.py --channel "@somechannel" --video "https://youtu.be/XXXXXXXXXXX" # 單片
python3 ytmd.py --channel "@somechannel" --build-only                          # 只重建 md（0.5 s）
```

## 產出

```
channels/<slug>/
├── transcripts.md            # 原始字幕（逐字稿）
├── transcripts.txt           # 同上，純文字版
├── transcripts-zh-hans.md    # 潤色版（模式 zh-hans；其他模式為 -zh-hant / -polished）
├── transcripts-zh-hans.txt   # 同上，純文字版
├── channel.json              # 這個頻道記住的 channel / lang / polish 設定
├── data/videos.tsv           # 頻道清單（6 小時內重複使用）
├── data/videos_window.tsv    # 本次選中的影片（上次跑的範圍）
├── data/meta_all.tsv         # 發佈日 / 時長 / 觀看數 cache
├── subs/<id>.<lang>.json3    # 原始字幕 cache
└── polished/<id>.md          # 潤色逐字稿 cache（可直接手改）
```

設定會被記住：之後只寫 `--channel "@somechannel" --build-only`（或省略 `--lang/--polish`）會沿用上次的設定。

## 參數

| 參數 | 說明 |
|---|---|
| `--channel` | 頻道 URL、`@handle`、`/channel/UC...`（必填） |
| `--days N` | 只處理最近 N 天的上傳（預設 30） |
| `--all` | 等於 `--days 0`：整個頻道 |
| `--lang` | 字幕語言：`zh-Hant`（預設）`zh-Hans` `en` `ja` … |
| `--polish` | `zh-hans` / `zh-hant` / `source`（同語言清理）/ `none`（不潤色）；預設：中文字幕→`zh-hans`，其他→`source` |
| `--video` | 只處理單一影片（可同時加進清單） |
| `--build-only` | 不連網，只用 cache 重建 md |
| `--refresh-list` | 強制重抓頻道清單（預設 6 小時 cache） |
| `--workers` | 平行抓字幕，預設 2（太高會被 429） |
| `--polish-workers` | 平行潤色，預設 4 |
| `--limit N` | 只處理前 N 部（抽樣測試用） |
| `--force-polish` | 重新潤色（預設跳過已有的 `polished/*.md`） |
| `--model` | 潤色模型，預設 `deepseek/deepseek-flash` |
| `--reasoning` | `none`（預設，快 5 倍）/ `""`（開啟思考，較精緻） |
| `--format` | `md`（預設）/ `txt` / `both`；txt 為純文字（`===` 分隔線 + 目錄 + 段落，無 markdown 語法） |
| `--out` | 自訂輸出路徑（副檔名 .md / .txt 對應哪一種格式） |

## 流程

```
list    yt-dlp --flat-playlist               頻道清單（3263 部 ≈ 1 min，6 小時 cache）
window  二分搜尋發佈日 → 最近 N 天            已抓過日期的片子直接查 cache，不用探測
fetch   yt-dlp --write-auto-subs              每部 ~6 s；可 WORKERS 並行；429 會自動退避重試
polish  切塊(~1200字) → LLM → 拼接            每部 ~2 s（4 並行）
build   排序 + 目錄 + 影片資訊 → md            0.4 s/125 部
```

實測（125 部、一個月）：字幕約 16 分鐘（2 並行）、潤色 224 秒、321 次 API 呼叫
（276 k input / 171 k output tokens）。全部 cache 命中時整份重建 0.5 秒。

潤色模式 `zh-hans` 會多做一次 opencc `t2s` 保底轉換，確保輸出不含繁體字。

## 前置需求

- `yt-dlp`，且 **Chrome 已登入 YouTube**（腳本用 `--cookies-from-browser chrome`；沒有 cookie 會被擋 429）
- `python3`；`pip install opencc-python-reimplemented`（簡繁保底轉換用，可選）
- 潤色用 `~/.pi/agent/models.json` 裏的 `magpie` provider（本機 proxy），不需要額外 key

## 疑難排解

| 症狀 | 處理 |
|---|---|
| `HTTP Error 429` 大量出現 | YouTube 對 `/api/timedtext` 限流，通常幾十分鐘到數小時；工具會自動退避重試。降低 `--workers`（1–2）可減少觸發。 |
| `could not list channel` | 頻道網址錯，或 cookie 過期（重新用 Chrome 登入 YouTube）。 |
| 某片沒有指定語言字幕 | 該片沒有這條字幕軌；`--lang en` / `--lang zh-Hant` 換一條，或略過。build 時會跳過並列出。 |
| 潤色結果明顯變短 | 正常是原文字數 0.78–0.93 倍（只刪機翻重複語）。低於 0.6 表示被摘要了：`--force-polish --limit …` 重跑該片。 |
| 想改潤色風格 | 改 `ytmd.py` 裏的 `PROMPTS`（zh-hans / zh-hant / source），再 `--force-polish`。 |
| 單篇修稿 | 直接編輯 `channels/<slug>/polished/<id>.md`，再 `--build-only`。模型不會覆蓋（除非 `--force-polish`）。 |

## repo 結構

```
ytmd.py            全部流程（list / window / fetch / polish / build）
README.md          本文件
requirements.txt   python 依賴（只有 opencc 可選）
channels/          每個頻道一個工作區，產出物會 commit
  <slug>/
    channel.json          頻道設定（channel / lang / polish）
    transcripts.md/.txt   原始字幕
    transcripts-*.md/.txt 潤色版
    polished/             潤色逐字稿 cache（commit，--build-only 靠它）
    data/meta_all.tsv     發佈日 / 時長 / 觀看數 cache（commit）
    data/videos*.tsv      頻道清單（gitignore，可重抓）
    subs/                 原始字幕 cache（gitignore，可重抓）
```

`subs/` 與 `data/videos.tsv` 不進版控（合計約 19 MB，隨時可重抓）；其餘都 commit，
所以 clone 下來 `--build-only` 就能重建 markdown / txt。

首次在乾淨的 clone 上跑完整流程，會重新抓頻道清單與字幕，並為缺 cache 的影片重新潤色。

# ytmd — YouTube channel → one transcript file

Turn **any** YouTube channel into a single transcript document (`.md` + `.txt`), optionally
machine-polished into clean Simplified Chinese.

Built to be driven by an AI agent: no prompts, no interactive steps, resumable, and every
stage is safe to re-run.

```bash
python3 ytmd.py --channel "@drbergchinese"          # last 30 days → channels/drbergchinese/transcripts*.md
```

---

## For agents — start here

Three commands cover 95% of use. All are non-interactive; none need stdin.

| Goal | Command | Time |
|---|---|---|
| Channel, default window (30 days) | `python3 ytmd.py --channel "@handle"` | 2–25 min |
| Whole channel | `python3 ytmd.py --channel "@handle" --all` | hours — run in background |
| One video | `python3 ytmd.py --channel "@handle" --video "https://youtu.be/XXXXXXXXXXX"` | ~15 s |
| Rebuild from cache only | `python3 ytmd.py --channel "@handle" --build-only` | ~0.5 s |
| English channel, English text | `python3 ytmd.py --channel "@handle" --lang en --polish source` | as above |

**How to tell it worked** — the run ends with `build:` lines naming the files and their video count:

```
build: /…/channels/<slug>/transcripts.md (125 videos, 920 KB)
build: /…/channels/<slug>/transcripts-zh-hans.md (125 videos, 898 KB)
```

Exit code is **0** on success. Exit code **1** with a message on stderr means a hard failure
(channel unreadable, empty window, no cache for `--build-only`).

A run can exit 0 while some videos are missing captions. Watch for this line — treat `N > 0` as a partial result:

```
warning: 3 videos have no zh-Hant captions
  no captions, skipped 3: abc12345678, def12345678, ...
```

Videos that fail are **not** written as empty sections; the file only contains what exists.

---

## Setup (one time, ~5 min)

### 1. Python + yt-dlp

```bash
python3 --version          # 3.9+ works (built and tested on 3.15)
yt-dlp --version           # must exist; install with: brew install yt-dlp
pip install opencc-python-reimplemented   # optional: guarantees zero traditional characters in zh-hans output
```

### 2. Chrome logged into YouTube — required

Caption downloads use `--cookies-from-browser chrome`. Without cookies YouTube answers
`HTTP Error 429: Too Many Requests` within a few videos.

```bash
# verify the browser session is usable for captions
yt-dlp --cookies-from-browser chrome --skip-download --write-auto-subs \
  --sub-langs zh-Hant --sub-format json3 -o /tmp/probe/%\(id\)s \
  "https://www.youtube.com/watch?v=ne6xylXZLbU" && ls /tmp/probe
```

- Chrome must be installed, logged in, and **not** running with `--disable-cookies`.
- macOS may ask for Keychain access the first time; allow it (the CLI cannot prompt for you).
- On a headless box with no Chrome, this pipeline will not work as-is; supply cookies another
  way and swap the `COOKIES` constant in `ytmd.py`.

### 3. Polish model (only needed for the polish stage)

Polishing calls the local **magpie** proxy configured in `~/.pi/agent/models.json`. No API key
is stored in this repo; the key is read from that file at runtime.

```bash
# provider health check (prints baseUrl + whether a key resolved, never the key itself)
python3 -c "
import json,os,urllib.request
p=json.load(open(os.path.expanduser('~/.pi/agent/models.json')))['providers']['magpie']
k=p['apiKey']; k=os.environ.get(k[4:],'') if k.startswith('env:') else k
print('baseUrl:', p['baseUrl'], '| key resolved:', bool(k))
print('proxy status:', urllib.request.urlopen(p['baseUrl'].rstrip('/')+'/models', timeout=5).status)
"
```

Expected: `baseUrl: http://127.0.0.1:3425/v1 | key resolved: True` and `proxy status: 200`.
If it fails, the proxy is down — skip polishing with `--polish none` and the pipeline still
produces raw transcripts.

### 4. Sanity check (all of the above at once)

```bash
python3 ytmd.py --channel "@drbergchinese" --build-only --format both
```

Expected on a fresh clone: `transcripts-zh-hans.*` rebuilt from committed `polished/`, and

```
build: skipped transcripts.md — no sources cached (fetch captions first, or run without --build-only)
```

That skip is correct behaviour: `subs/` is gitignored, and the build refuses to write an empty file.

---

## Usage

```bash
# windows
python3 ytmd.py --channel "@handle"                     # 30 days (default)
python3 ytmd.py --channel "@handle" --days 7
python3 ytmd.py --channel "@handle" --all               # entire channel
python3 ytmd.py --channel "@handle" --limit 3           # smoke test: first 3 videos

# language / polishing
python3 ytmd.py --channel "@handle" --lang zh-Hant --polish zh-hans   # (default for Chinese)
python3 ytmd.py --channel "@handle" --lang zh-Hans  --polish zh-hant
python3 ytmd.py --channel "@handle" --lang en       --polish source   # keep English, fix punctuation only
python3 ytmd.py --channel "@handle" --polish none                     # raw transcripts, no LLM

# formats and paths
python3 ytmd.py --channel "@handle" --format txt        # txt only
python3 ytmd.py --channel "@handle" --format both       # md + txt (default: md)
python3 ytmd.py --channel "@handle" --build-only --out /tmp/x.txt

# re-processing
python3 ytmd.py --channel "@handle" --force-polish      # redo every polish job
python3 ytmd.py --channel "@handle" --refresh-list      # re-scrape channel listing (6 h cache)
```

`--channel` accepts `@handle`, a full URL, or `/channel/UC…`. Settings (`lang`, `polish`) are
remembered per channel in `channels/<slug>/channel.json`, so later runs only need `--channel`.

`--polish none` writes only the raw transcript (no polished file, no LLM calls), but it also
persists `polish: none` for that channel — add `--polish zh-hans` on a later run to switch back.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--channel` | *required* | channel URL / `@handle` / `/channel/UC…` |
| `--days N` | `30` | uploads from the last N days |
| `--all` | off | shorthand for `--days 0` (everything) |
| `--lang` | `zh-Hant` | caption track: `zh-Hant`, `zh-Hans`, `en`, `ja`, … |
| `--polish` | auto | `zh-hans` / `zh-hant` / `source` / `none`; defaults to `zh-hans` for Chinese captions, `source` otherwise |
| `--video` | — | process a single URL/ID only |
| `--build-only` | off | rebuild from cache, zero network calls |
| `--format` | `md` | `md`, `txt`, or `both` |
| `--out` | — | explicit output path (extension picks the format) |
| `--workers` | `2` | parallel caption downloads; raise carefully (see 429 below) |
| `--polish-workers` | `4` | parallel LLM requests |
| `--limit N` | `0` | process only the first N videos — the output file then contains just those N; pass `--out` if you want to keep the full file |
| `--force-polish` | off | ignore existing `polished/*.md` |
| `--reasoning` | `none` | `none` = fast/cheap; `""` = enable model thinking (~5× slower, slightly cleaner) |
| `--model` | `deepseek/deepseek-flash` | polish model id from `models.json` |
| `--refresh-list` | off | bypass the 6-hour channel-listing cache |

### Outputs

```
channels/<slug>/                      slug = @handle lowercased
├── transcripts.md / .txt             raw captions
├── transcripts-zh-hans.md / .txt     polished (suffix varies: -zh-hant, -polished)
├── channel.json                      remembered channel / lang / polish
├── polished/<videoId>.md             per-video polished text — committed, hand-editable
└── data/{videos,videos_window,meta_all}.tsv
```

`transcripts*` files: header → numbered TOC → one section per video, newest first, with
date · duration · views · canonical URL. `.txt` is the same content without markdown syntax
(`===` rules, plain URLs) so it opens in any editor.

### Timing / cost (measured)

| Step | Cost |
|---|---|
| Channel listing (5k videos) | ~1 min, cached 6 h |
| Upload-date window search | ~1 s (cached dates) … ~30 s (cold) |
| Captions | ~6 s/video, `--workers 2` |
| Polish | ~4 s per 1.2k-char chunk, `--workers 4`; 125 videos ≈ 220 s, ~170k output tokens |
| Build | 0.4 s for 125 videos |

---

## Agent notes

- **Long commands**: `--all` on a big channel can run for hours. Run it detached
  (`nohup … &`), then poll the output file or re-run `--build-only` to see progress.
  Caption fetching alone is ~6 s/video; budget > 30 min for a 300-video channel.
- **Resumability**: every stage skips work that already exists (`subs/<id>.<lang>.json3`,
  `polished/<id>.md`, cached dates). Killing a run and restarting is always safe.
- **Rate limits**: YouTube throttles `/api/timedtext` per IP and it can last tens of minutes.
  The fetcher retries 4 passes with exponential backoff and reports what is still missing.
  Lower `--workers` to 1–2 if you see sustained 429s; do not add parallelism to "fix" it.
- **Do not** hand-write or truncate `transcripts*` files: they are generated. Edit
  `polished/<id>.md` instead (the model will not overwrite it without `--force-polish`).
- **Idempotency**: re-running with the same window produces byte-identical outputs except
  for the `Generated:` date.
- **No network**: `--build-only` makes zero requests; use it in CI or offline.
- **Reading results programmatically**: the `build:` lines are stable
  (`build: <path> (<n> videos, <kb> KB)`), and the TOC lists all titles.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `HTTP Error 429` on most videos | IP throttling on `/api/timedtext`. Wait (10 min – hours), keep `--workers 1`. Not a config error. |
| `could not list channel` | Bad URL, or Chrome session expired → log into YouTube in Chrome again. |
| `no videos in the selected window` | `--days` too small for a slow channel; use `--days 90` or `--all`. |
| `nothing cached yet; run without --build-only first` | Fresh clone with no `subs/`: run once with network. |
| Some videos silently absent | They have no captions in `--lang`; the run reports `no captions, skipped N`. Try `--lang en`. |
| Polished text suspiciously short | Normal is 0.78–0.93× the raw char count. Below 0.6 means it summarized: `--force-polish --limit N`. |
| Traditional characters leak into `zh-hans` | Install `opencc-python-reimplemented`; the build runs a `t2s` safety pass when it is present. |
| Polish stage hangs | magpie proxy down → `--polish none`, or check the health command in Setup §3. |

---

## Repo layout

```
ytmd.py             the whole pipeline: list → window → fetch → polish → build
README.md           this file
requirements.txt    python deps (opencc is optional)
channels/<slug>/    one workspace per channel; transcripts + polished/ are committed
```

`subs/` (raw caption cache, ~19 MB per 125 videos) and `data/videos.tsv` (channel listing)
are gitignored — both are re-fetchable, and no build needs them if `polished/` is present.

## Content note

Transcripts are YouTube auto-captions of the original channels' videos and remain the
respective owners' content; the polish stage rewrites wording only (no summarizing, no added
claims) and is not a substitute for the videos or for medical/professional advice.

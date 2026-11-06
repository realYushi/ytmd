#!/usr/bin/env python3
"""YouTube channel -> one markdown file of transcripts (any channel).

    python3 ytmd.py --channel "https://www.youtube.com/@drbergchinese" --days 30
    python3 ytmd.py --channel "@hubermanlab" --days 7 --lang en --polish source
    python3 ytmd.py --channel "@somechannel" --all
    python3 ytmd.py --channel "@somechannel" --video "https://youtu.be/XXXXXXXXXXX"
    python3 ytmd.py --channel "@somechannel" --build-only

Each channel gets its own workspace under channels/<slug>/ holding the raw caption
cache, the polished transcripts, the video listing and the generated markdown, so
running several channels never mixes data.

Stages
  1. list    flat playlist -> channels/<slug>/data/videos.tsv
  2. window  pick uploads within --days (binary search on upload dates)
  3. fetch   yt-dlp --write-auto-subs --sub-langs <lang>   (resumable)
  4. polish  LLM pass -> Simplified Chinese / Traditional / same-language cleanup
  5. build   markdown with TOC, newest first

Requires: yt-dlp, Chrome logged into YouTube (cookies), python3, and for the
polish stage the magpie provider in ~/.pi/agent/models.json.
"""
import argparse
import concurrent.futures as futures
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CHANNELS = os.path.join(ROOT, "channels")
MODELS = os.path.expanduser("~/.pi/agent/models.json")
COOKIES = ["--cookies-from-browser", "chrome"]
LIST_TTL = 6 * 3600  # seconds before the channel listing is re-scraped


# --------------------------------------------------------------------------- paths
def channel_slug(channel):
    """@handle or channel URL -> filesystem-safe slug."""
    m = re.search(r"@([A-Za-z0-9._-]+)", channel)
    if m:
        return m.group(1).lower()
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", channel)
    if m:
        return m.group(1)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", channel).strip("-").lower() or "channel"


def normalize_channel(channel):
    c = channel.strip()
    if c.startswith("http://") or c.startswith("https://"):
        return c
    if c.startswith("@"):
        return "https://www.youtube.com/" + c
    return "https://www.youtube.com/" + c.lstrip("/")


def paths(slug):
    ws = os.path.join(CHANNELS, slug)
    return {
        "ws": ws,
        "subs": os.path.join(ws, "subs"),
        "polished": os.path.join(ws, "polished"),
        "data": os.path.join(ws, "data"),
        "listing": os.path.join(ws, "data/videos.tsv"),
        "window": os.path.join(ws, "data/videos_window.tsv"),
        "meta": os.path.join(ws, "data/meta_all.tsv"),
        "config": os.path.join(ws, "channel.json"),
    }


def ensure(ws):
    for k in ("subs", "polished", "data"):
        os.makedirs(ws[k], exist_ok=True)


# --------------------------------------------------------------------------- helpers
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def load_config(ws):
    if os.path.exists(ws["config"]):
        return json.load(open(ws["config"], encoding="utf-8"))
    return {}


def save_config(ws, **kv):
    cfg = load_config(ws)
    cfg.update({k: v for k, v in kv.items() if v})
    with open(ws["config"], "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def read_tsv(path):
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            p = line.rstrip("\n").split("|||")
            if len(p) == 4:
                rows.append(p)
    return rows


def load_meta(ws):
    return {r[0]: r for r in read_tsv(ws["meta"])}


def fmt_duration(sec):
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ""
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_date(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if d and len(d) == 8 and d.isdigit() else ""


def fmt_views(v):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return ""
    if v >= 10_000:
        return f"{v/10_000:.1f} 万" if v >= 100_000 else f"{v/10_000:.1f} 萬"
    return str(v)


# --------------------------------------------------------------------------- 1. list
def list_channel(ws, channel, refresh=False):
    fresh = os.path.exists(ws["listing"]) and (time.time() - os.path.getmtime(ws["listing"]) < LIST_TTL)
    rows = read_tsv(ws["listing"])
    if rows and fresh and not refresh:
        print(f"channel listing: {len(rows)} videos (cached)")
        return rows
    print("channel listing: scraping ...", flush=True)
    url = channel.rstrip("/")
    if "/videos" not in url:
        url += "/videos"
    out = run(["yt-dlp", *COOKIES, "--flat-playlist",
               "--print", "%(id)s|||%(title)s|||%(duration)s|||%(view_count)s", url])
    rows = [l.split("|||") for l in out.stdout.splitlines() if l.count("|||") == 3]
    if not rows:
        sys.exit(f"could not list channel: {channel}\n{out.stderr.strip()[-400:]}")
    with open(ws["listing"], "w", encoding="utf-8") as f:
        f.write("\n".join("|||".join(r) for r in rows) + "\n")
    print(f"channel listing: {len(rows)} videos")
    return rows


# --------------------------------------------------------------------------- 2. window
def probe_date(vid, meta_cache, workers=1):
    if vid in meta_cache and meta_cache[vid][1] not in ("", "NA", "None"):
        return meta_cache[vid][1]
    out = run(["yt-dlp", *COOKIES, "--skip-download", "--simulate",
               "--print", "%(upload_date)s", f"https://www.youtube.com/watch?v={vid}"])
    d = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
    return d if len(d) == 8 and d.isdigit() else ""


def select_window(ws, rows, days, meta_cache):
    if days <= 0:
        return rows
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y%m%d")
    ids = [r[0] for r in rows]
    print(f"window: uploads on/after {cutoff} ({days} days), newest = {probe_date(ids[0], meta_cache)}")
    if probe_date(ids[0], meta_cache) < cutoff:
        return []
    lo, hi = 0, len(ids) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        d = probe_date(ids[mid], meta_cache)
        print(f"  probe {mid}: {d}", flush=True)
        if d and d >= cutoff:
            lo = mid
        else:
            hi = mid - 1
    return rows[: lo + 1]


# --------------------------------------------------------------------------- metadata
def update_metadata(ws, ids):
    """Fill data/meta_all.tsv for the given ids (only the ones still missing)."""
    meta = load_meta(ws)
    todo = [i for i in ids if i not in meta or meta[i][1] in ("", "NA", "None")]
    if not todo:
        return meta
    print(f"metadata: fetching {len(todo)} of {len(ids)}")
    tmp = os.path.join(ws["data"], "meta_new.tsv")
    batch = os.path.join(ws["data"], "ids_meta.txt")
    open(batch, "w", encoding="utf-8").write("\n".join(todo) + "\n")
    run(["yt-dlp", *COOKIES, "--skip-download", "--simulate", "--ignore-errors", "--no-warnings",
         "--print-to-file", "%(id)s|||%(upload_date)s|||%(duration)s|||%(view_count)s", tmp,
         "--batch-file", batch])
    rows = {}
    order = []
    for r in read_tsv(ws["meta"]) + read_tsv(tmp):
        if r[0] not in rows:
            order.append(r[0])
        rows[r[0]] = r
    with open(ws["meta"], "w", encoding="utf-8") as f:
        f.write("\n".join("|||".join(rows[i]) for i in order) + "\n")
    for p in (tmp, batch):
        if os.path.exists(p):
            os.remove(p)
    return load_meta(ws)


# --------------------------------------------------------------------------- 3. fetch
def fetch_subs(ws, ids, lang, workers=2, sleep_requests=1, max_passes=4):
    def missing():
        return [i for i in ids if not os.path.exists(os.path.join(ws["subs"], f"{i}.{lang}.json3"))]

    for p in range(max_passes):
        todo = missing()
        if not todo:
            break
        print(f"captions: {len(todo)} remaining (pass {p + 1})", flush=True)
        chunks = [[] for _ in range(max(1, workers))]
        for k, vid in enumerate(todo):
            chunks[k % len(chunks)].append(vid)
        procs = []
        for k, chunk in enumerate(chunks):
            if not chunk:
                continue
            bf = os.path.join(ws["data"], f"batch_{k}.txt")
            open(bf, "w", encoding="utf-8").write("\n".join(chunk) + "\n")
            procs.append(subprocess.Popen(
                ["yt-dlp", *COOKIES, "--skip-download", "--write-auto-subs",
                 "--sub-langs", lang, "--sub-format", "json3",
                 "--retries", "3", "--sleep-requests", str(sleep_requests),
                 "--ignore-errors", "--no-warnings",
                 "-o", os.path.join(ws["subs"], "%(id)s"), "--batch-file", bf],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        for proc in procs:
            proc.wait()
        got = len(ids) - len(missing())
        print(f"captions: {got}/{len(ids)} done")
        if missing() and p < max_passes - 1:
            wait = 60 * (2 ** p)
            print(f"captions: still missing some, retrying in {wait}s (YouTube throttling is common)")
            time.sleep(wait)
    return missing()


# --------------------------------------------------------------------------- 4. polish
PROMPTS = {
    "zh-hans": """你是專業的中文編輯，負責把字幕稿整理成通順的簡體中文。

規則：
1. 一律輸出簡體中文，並使用中國大陸的用語習慣。
2. 打破機器翻譯的英文語序，改寫成自然的中文句子；必要時合併或拆分句子。
3. 修正明顯的錯譯、錯字、錯誤斷句（依上下文合理判斷）；不確定時保留原意，不要臆測或添加內容。
4. 完整保留所有資訊與細節：不摘要、不刪重點、不補充原文沒有的內容、不加評論、不加標題。
5. 數字、劑量、單位、人名、專業名詞照原意保留，必要時可附原文。
6. 只輸出正文，不要任何說明、前言、引號或 Markdown 標記。""",
    "zh-hant": """你是專業的中文編輯，負責把字幕稿整理成通順的繁體中文。

規則：
1. 一律輸出繁體中文（台灣用語）。
2. 打破機器翻譯的英文語序，改寫成自然的中文句子；必要時合併或拆分句子。
3. 修正明顯的錯譯、錯字、錯誤斷句（依上下文合理判斷）；不確定時保留原意，不要臆測或添加內容。
4. 完整保留所有資訊與細節：不摘要、不刪重點、不補充原文沒有的內容、不加評論、不加標題。
5. 數字、劑量、單位、人名、專業名詞照原意保留，必要時可附原文。
6. 只輸出正文，不要任何說明、前言、引號或 Markdown 標記。""",
    "source": """你是專業的文字編輯，負責整理自動字幕稿（保留原文語言，不要翻譯）。

規則：
1. 維持原本的語言。
2. 修正語病、錯字、重複與錯誤斷句，補上正確的標點，讓句子通順自然。
3. 完整保留所有資訊與細節：不摘要、不刪重點、不補充原文沒有的內容、不加評論、不加標題。
4. 數字、劑量、單位、人名、專業名詞照原意保留。
5. 只輸出正文，不要任何說明、前言、引號或 Markdown 標記。""",
}

TXT_RULE = "=" * 78

USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
USAGE_LOCK = threading.Lock()


def load_api():
    d = json.load(open(MODELS, encoding="utf-8"))
    prov = d["providers"]["magpie"]
    key = prov["apiKey"]
    if isinstance(key, str) and key.startswith("env:"):
        key = os.environ.get(key[4:], "")
    return prov["baseUrl"].rstrip("/"), key


def caption_text(path):
    text = open(path, encoding="utf-8").read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data, _ = json.JSONDecoder().raw_decode(text)
    out = ""
    for ev in data.get("events", []):
        for seg in (ev.get("segs") or []):
            c = seg.get("utf8", "").replace("\n", " ").replace("\u200b", "")
            if not c.strip():
                continue
            if out and out[-1].isascii() and out[-1].isalnum() and c[0].isascii() and c[0].isalnum():
                out += " "
            out += c
    return re.sub(r"[ \t]+", " ", out).strip()


SENT_END = "。！？!?."


def split_chunks(text, limit=1200):
    sentences, buf = [], ""
    for ch in text:
        buf += ch
        if ch in SENT_END:
            sentences.append(buf)
            buf = ""
    if buf.strip():
        sentences.append(buf)
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > limit:
            chunks.append(cur)
            cur = ""
        cur += s
    if cur.strip():
        chunks.append(cur)
    final = []
    for c in chunks:
        while len(c) > limit * 2:
            cut = max(c.rfind("，", 0, limit), c.rfind(",", 0, limit), c.rfind(" ", limit // 2, limit))
            cut = cut + 1 if cut > limit // 2 else limit
            final.append(c[:cut])
            c = c[cut:]
        if c.strip():
            final.append(c)
    return final


def llm(base, key, model, system, user_text, retries=3, reasoning="none"):
    body = {"model": model, "temperature": 0.3,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_text}]}
    if reasoning:
        body["reasoning_effort"] = reasoning
    data = json.dumps(body).encode()
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(base + "/chat/completions", data=data,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                j = json.load(r)
            out = (j["choices"][0]["message"].get("content") or "").strip()
            u = j.get("usage") or {}
            with USAGE_LOCK:
                USAGE["calls"] += 1
                USAGE["prompt_tokens"] += u.get("prompt_tokens", 0)
                USAGE["completion_tokens"] += u.get("completion_tokens", 0)
            if out:
                return out
            last = "empty content"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last)


def polish(ws, ids, lang, mode, model, workers=4, force=False, reasoning="none", limit=0):
    system = PROMPTS[mode]
    base, key = load_api()
    os.makedirs(ws["polished"], exist_ok=True)
    todo = ids[:limit] if limit else ids
    done = [0]
    t0 = time.time()
    lock = threading.Lock()

    def task(vid):
        dst = os.path.join(ws["polished"], f"{vid}.md")
        if os.path.exists(dst) and os.path.getsize(dst) > 200 and not force:
            status, n = "cached", 0
        else:
            src = os.path.join(ws["subs"], f"{vid}.{lang}.json3")
            if not os.path.exists(src):
                return "no-captions"
            text = caption_text(src)
            if not text:
                return "empty"
            try:
                parts = split_chunks(text)
                out = [llm(base, key, model, system, p, reasoning=reasoning or None) for p in parts]
                with open(dst, "w", encoding="utf-8") as f:
                    f.write("\n\n".join(x.strip() for x in out if x.strip()) + "\n")
                status, n = "ok", len(parts)
            except Exception as e:  # noqa: BLE001
                status, n = "failed: " + str(e)[:100], 0
        with lock:
            done[0] += 1
            print(f"[{done[0]}/{len(todo)}] {vid} {status} ({n} chunks, {time.time()-t0:.0f}s)", flush=True)
        return status

    with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(task, todo))
    ok = sum(1 for r in results if r in ("ok", "cached"))
    print(f"polish: {ok}/{len(todo)} in {time.time()-t0:.0f}s, "
          f"{USAGE['calls']} api calls, {USAGE['prompt_tokens']} in / {USAGE['completion_tokens']} out tokens")
    bad = [r for r in results if r not in ("ok", "cached")]
    if bad:
        print("problems:", bad[:5])


# --------------------------------------------------------------------------- 5. build
def paragraphs(text, target=220):
    sent, buf = [], ""
    for ch in text:
        buf += ch
        if ch in SENT_END:
            sent.append(buf)
            buf = ""
    if buf.strip():
        sent.append(buf)
    paras, cur = [], ""
    for s in sent:
        cur += s
        if len(cur) >= target:
            paras.append(cur.strip())
            cur = ""
    if cur.strip():
        paras.append(cur.strip())
    return paras


def build(ws, rows, lang, mode, polished=False, out="", heading=None, fmt="md"):
    meta = load_meta(ws)
    t2s = None
    if polished and mode == "zh-hans":
        try:
            import opencc
            t2s = opencc.OpenCC("t2s").convert
        except ImportError:
            print("warning: opencc not installed; skipping the traditional->simplified safety pass")
    rows = sorted(rows, key=lambda r: meta.get(r[0], ["", "00000000"])[1] or "00000000", reverse=True)

    sections, toc, skipped = [], [], []
    for vid, title, dur, views in rows:
        body, kind = "", ""
        pol = os.path.join(ws["polished"], f"{vid}.md")
        raw = os.path.join(ws["subs"], f"{vid}.{lang}.json3")
        if polished and os.path.exists(pol) and os.path.getsize(pol) > 200:
            body, kind = open(pol, encoding="utf-8").read().strip(), "polished"
        elif os.path.exists(raw):
            text = caption_text(raw)
            if text.strip():
                body, kind = "\n\n".join(paragraphs(text)), "raw"
        if not body:
            skipped.append(vid)
            continue
        if t2s:
            body = t2s(body)
        if t2s:
            title = t2s(title)

        m = meta.get(vid, ["", "", dur, views])
        info = " · ".join(x for x in [fmt_date(m[1]), f"时长 {fmt_duration(m[2])}", f"观看 {fmt_views(m[3])}"] if x)
        note = "\n> ⚠️ 此片尚未润色，以下为原始字幕。\n" if (polished and kind == "raw") else ""
        n = len(sections) + 1
        url = f"https://www.youtube.com/watch?v={vid}"
        if fmt == "txt":
            toc.append(f"{n}. {title}")
            sections.append(
                f"{TXT_RULE}\n{n}. {title}\n{info} · {url}\n{TXT_RULE}\n\n"
                f"{note}{body}\n\n")
        else:
            toc.append(f"{n}. [{title}](#v{n})")
            sections.append(
                f'<a id="v{n}"></a>\n\n## {n}. {title}\n\n'
                f"<sub>{info} · [YouTube]({url})</sub>\n{note}\n"
                f"{body}\n\n---\n")

    zh = mode in ("zh-hans", "zh-hant")
    suffix = {"zh-hans": "-zh-hans", "zh-hant": "-zh-hant"}.get(mode, "-polished") if polished else ""
    ext = ".txt" if fmt == "txt" else ".md"
    name = out or os.path.join(ws["ws"], f"transcripts{suffix}{ext}")

    label = (
        {"title": "逐字稿彙整", "polished": "简体中文 · 已润色" if mode == "zh-hans" else "繁體中文 · 已潤色",
         "ch": "**频道：**", "n": "**影片数：**", "d": "**产出时间：**", "l": "**字幕语言：**",
         "src": "**来源：** YouTube 自动字幕", "pol": "，经语言模型润色",
         "note": "> 说明：自动字幕由机器生成，润色只修正语言问题，不保证内容准确，请以原片为准。",
         "toc": "## 目录"}
        if zh else
        {"title": "Transcripts", "polished": "polished",
         "ch": "**Channel:**", "n": "**Videos:**", "d": "**Generated:**", "l": "**Caption language:**",
         "src": "**Source:** YouTube auto-captions", "pol": ", language-model cleaned",
         "note": "> Note: auto-captions are machine generated; cleaning fixes wording only, not accuracy.",
         "toc": "## Contents"}
    )
    heading = heading or f"# {ws.get('handle', ws['slug'])} · {label['title']}" + (
        f"（{label['polished']}）" if polished and zh else " (polished)" if polished else "")

    if fmt == "txt":
        def plain(x):
            return x.replace("**", "").replace("> ", "").rstrip()

        head = [
            heading.lstrip("# ").replace("·", "-"),
            "",
            plain(f"{label['ch']} {ws.get('channel', ws['slug'])}"),
            plain(f"{label['n']} {len(sections)}"),
            plain(f"{label['d']} {time.strftime('%Y-%m-%d')}"),
            plain(f"{label['l']} {lang}"),
            plain(label["src"] + (label["pol"] if polished else "")),
            "",
            plain(label["note"]),
            "",
            TXT_RULE,
            plain(label["toc"]).lstrip("# "),
            "",
        ]
        with open(name, "w", encoding="utf-8") as f:
            f.write("\n".join(head) + "\n")
            f.write("\n".join(toc) + "\n\n\n")
            f.write("\n".join(sections))
    else:
        head = [
            heading,
            "",
            f"{label['ch']} {ws.get('channel', ws['slug'])}  ",
            f"{label['n']} {len(sections)}  ",
            f"{label['d']} {time.strftime('%Y-%m-%d')}  ",
            f"{label['l']} {lang}  ",
            label["src"] + (label["pol"] if polished else ""),
            "",
            label["note"],
            "",
            "---",
            "",
            label["toc"],
            "",
        ]
        if not sections and os.path.exists(name) and os.path.getsize(name) > 1000:
            print(f"build: skipped {name} — no sources available (would overwrite {os.path.getsize(name)//1024} KB)")
            return name
        with open(name, "w", encoding="utf-8") as f:
            f.write("\n".join(head) + "\n")
            f.write("\n".join(toc) + "\n\n---\n\n")
            f.write("\n".join(sections))
    print(f"build: {name} ({len(sections)} videos, {os.path.getsize(name)/1024:.0f} KB)")
    if skipped:
        print(f"  no captions, skipped {len(skipped)}: {', '.join(skipped[:5])}{' ...' if len(skipped) > 5 else ''}")
    return name


# --------------------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description="YouTube channel -> one markdown file of transcripts")
    ap.add_argument("--channel", help="channel URL, @handle, or /channel/UC... id")
    ap.add_argument("--days", type=int, default=30, help="only uploads from the last N days (0 = everything)")
    ap.add_argument("--all", action="store_true", help="shortcut for --days 0")
    ap.add_argument("--lang", default="", help="caption language, e.g. zh-Hant, zh-Hans, en (default: zh-Hant, else source)")
    ap.add_argument("--polish", default="", choices=["", "zh-hans", "zh-hant", "source", "none"],
                    help="polish mode; default zh-hans for Chinese captions, 'source' otherwise")
    ap.add_argument("--video", default="", help="add/refresh a single video by URL or id")
    ap.add_argument("--build-only", action="store_true", help="rebuild markdown from cache, no network")
    ap.add_argument("--refresh-list", action="store_true", help="re-scrape the channel listing")
    ap.add_argument("--workers", type=int, default=2, help="parallel caption downloads (default 2)")
    ap.add_argument("--polish-workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="process only the first N videos")
    ap.add_argument("--force-polish", action="store_true")
    ap.add_argument("--model", default="deepseek/deepseek-flash")
    ap.add_argument("--reasoning", default="none", help="reasoning_effort for the polish model ('' to enable thinking)")
    ap.add_argument("--format", default="md", choices=["md", "txt", "both"],
                    help="output format: md (default), txt, or both")
    ap.add_argument("--out", default="", help="output path (applies to the matching format; .md/.txt)")
    args = ap.parse_args()

    if not args.channel:
        ap.error("--channel is required (e.g. --channel @drbergchinese)")
    channel = normalize_channel(args.channel)
    slug = channel_slug(channel)
    ws = paths(slug)
    ws["slug"] = slug
    ws["channel"] = channel
    m = re.search(r"@([A-Za-z0-9._-]+)", channel)
    ws["handle"] = "@" + m.group(1) if m else slug
    ensure(ws)
    cfg = load_config(ws)
    lang = args.lang or cfg.get("lang") or "zh-Hant"
    mode = args.polish or cfg.get("polish") or ("zh-hans" if lang.lower().startswith("zh") else "source")
    save_config(ws, channel=channel, lang=lang, polish=mode)
    formats = ["md", "txt"] if args.format == "both" else [args.format]
    print(f"workspace: {ws['ws']}\nlanguage: {lang}   polish: {mode}   format: {'+'.join(formats)}")

    if args.build_only:
        rows = read_tsv(ws["window"]) or read_tsv(ws["listing"])
        if not rows:
            sys.exit("nothing cached yet; run without --build-only first")
        if args.limit:
            rows = rows[: args.limit]
        for fmt in formats:
            out = args.out if args.out.lower().endswith("." + fmt) else ""
            build(ws, rows, lang, mode, polished=False, out="", fmt=fmt)
            if os.path.isdir(ws["polished"]) and os.listdir(ws["polished"]):
                build(ws, rows, lang, mode, polished=True, out=out, fmt=fmt)
        return

    rows = list_channel(ws, channel, refresh=args.refresh_list)

    if args.video:
        vid = args.video.strip()
        m = re.search(r"([A-Za-z0-9_-]{11})", vid)
        vid = m.group(1) if m else vid
        match = [r for r in rows if r[0] == vid]
        if not match:
            out = run(["yt-dlp", *COOKIES, "--skip-download", "--simulate", "--print",
                       "%(id)s|||%(title)s|||%(duration)s|||%(view_count)s",
                       f"https://www.youtube.com/watch?v={vid}"])
            line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
            if line.count("|||") != 3:
                sys.exit(f"could not read video {vid}")
            match = [line.split("|||")]
            rows = match + rows
            with open(ws["listing"], "w", encoding="utf-8") as f:
                f.write("\n".join("|||".join(r) for r in rows) + "\n")
        rows = match

    meta = load_meta(ws)
    rows = select_window(ws, rows, 0 if args.all else args.days, meta)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        sys.exit("no videos in the selected window")
    with open(ws["window"], "w", encoding="utf-8") as f:
        f.write("\n".join("|||".join(r) for r in rows) + "\n")
    ids = [r[0] for r in rows]
    print(f"selected {len(ids)} videos")

    update_metadata(ws, ids)
    missing = fetch_subs(ws, ids, lang, workers=args.workers)
    if missing:
        print(f"warning: {len(missing)} videos have no {lang} captions")

    if mode != "none":
        polish(ws, ids, lang, mode, args.model, workers=args.polish_workers,
               force=args.force_polish, reasoning=args.reasoning, limit=0)
    for fmt in formats:
        out = args.out if args.out.lower().endswith("." + fmt) else ""
        if mode == "none":
            build(ws, rows, lang, mode, polished=False, out=out, fmt=fmt)
        else:
            build(ws, rows, lang, mode, polished=False, out="", fmt=fmt)
            build(ws, rows, lang, mode, polished=True, out=out, fmt=fmt)


if __name__ == "__main__":
    main()

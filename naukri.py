#!/usr/bin/env python3
"""
Sarkari Naukri Turant - automatic government job alert channel (Hindi).

  python naukri.py watch     check official sources, read new notices, post Shorts for the big ones
  python naukri.py digest    make today's long "all new jobs" video
  add --dry-run to make videos without uploading (saved in output/)
  python naukri.py selftest  render a sample Short offline (no keys needed)

Facts (posts, age, dates) are only used if they can be found in the official notice text.
"""
import argparse
import asyncio
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml

import motion

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
OUT = ROOT / "output"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
      "SarkariNaukriTurantBot/1.0")

YES = re.compile(r"recruit|vacanc|advertis|advt|notification|walk[\s-]?in|apply\s*online|engagement|contractual|"
                 r"post of|posts of|empanel|deputation|भर्ती|रिक्ति|विज्ञापन|नियुक्ति|संविदा|आवेदन|पदों", re.I)
NO = re.compile(r"result|answer\s*key|admit\s*card|merit\s*list|marks|cut[\s-]?off|interview\s*schedule|tender|"
                r"syllabus|corrigendum.*tender|परिणाम|उत्तर\s*कुंजी|प्रवेश\s*पत्र|निविदा|अंक\s*सूची|e-?auction|quotation", re.I)

HINDI_MONTHS = ["जनवरी", "फरवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
EN_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december"]
STATE_HI = {"Delhi": "दिल्ली", "Haryana": "हरियाणा", "Rajasthan": "राजस्थान", "Uttar Pradesh": "उत्तर प्रदेश",
            "Bihar": "बिहार", "India": "भारत"}


def now_ist():
    return dt.datetime.now(IST)


def log(*a):
    print(f"[{now_ist():%H:%M:%S}]", *a, flush=True)


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))[:200]} failed: {r.stderr[-800:]}")
    return r.stdout


def duration_of(p):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]).strip())


def load_cfg():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    st["seen"] = {k: v[-1500:] for k, v in st.get("seen", {}).items()}
    cutoff = (now_ist() - dt.timedelta(days=21)).isoformat()
    st["notices"] = [n for n in st.get("notices", []) if n.get("found", "") >= cutoff][-600:]
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def h(s):
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


# ============================================================================ AI chain (Gemini -> Groq -> Cloudflare)

_GEM_MODELS, _DEAD = None, set()


def _parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip()).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _gemini(prompt, cfg, temperature):
    global _GEM_MODELS
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("no Gemini key")
    if _GEM_MODELS is None:
        names = []
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", timeout=30,
                             headers={"x-goog-api-key": key}, params={"pageSize": 200})
            for m in r.json().get("models", []) if r.ok else []:
                n = m.get("name", "").split("/")[-1]
                if ("generateContent" in m.get("supportedGenerationMethods", []) and "flash" in n
                        and not re.search(r"image|tts|audio|live|embed|exp", n)):
                    names.append(n)
        except Exception:
            pass
        pref = cfg.get("gemini_model", "gemini-flash-latest")
        names = sorted(set(names), key=lambda n: (n != pref, n != "gemini-flash-latest", "lite" in n, "preview" in n))
        _GEM_MODELS = list(dict.fromkeys([pref] + names))[:6]
    gen = {"responseMimeType": "application/json", "temperature": temperature, "thinkingConfig": {"thinkingBudget": 0}}
    last = None
    for model in _GEM_MODELS:
        if model in _DEAD:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                r = requests.post(url, headers={"x-goog-api-key": key}, timeout=(15, 120),
                                  json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen})
                if r.status_code == 404:
                    _DEAD.add(model)
                    break
                if r.status_code == 400 and "thinking" in r.text.lower():
                    gen.pop("thinkingConfig", None)
                    continue
                if r.status_code == 429:
                    last = f"{model} quota/busy"
                    if "quota" in r.text.lower():
                        _DEAD.add(model)
                        break
                    time.sleep(10)
                    continue
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                return _parse_json("".join(p.get("text", "") for p in parts))
            except Exception as e:
                last = f"{model}: {e}"
                if "timed out" in str(e).lower():
                    break
                time.sleep(3)
    raise RuntimeError(f"gemini: {last}")


def _groq(prompt, cfg, temperature):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("no Groq key")
    last = None
    for model in [cfg.get("groq_model", "llama-3.3-70b-versatile"), "llama-3.1-8b-instant"]:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=120,
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": model, "temperature": temperature,
                                    "response_format": {"type": "json_object"},
                                    "messages": [{"role": "system", "content": "Reply with one valid JSON object only."},
                                                 {"role": "user", "content": prompt[:60000]}]})
            if r.status_code in (404, 400) and "model" in r.text.lower():
                last = f"{model} unavailable"
                continue
            if r.status_code == 429:
                last = f"{model} rate limited"
                time.sleep(8)
                continue
            r.raise_for_status()
            return _parse_json(r.json()["choices"][0]["message"]["content"])
        except Exception as e:
            last = f"{model}: {e}"
    raise RuntimeError(f"groq: {last}")


def _cloudflare(prompt, cfg, temperature):
    acct, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if not (acct and token):
        raise RuntimeError("no Cloudflare keys")
    r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                      headers={"Authorization": f"Bearer {token}"}, timeout=180,
                      json={"messages": [{"role": "system", "content": "Reply with one valid JSON object only."},
                                         {"role": "user", "content": prompt[:20000]}],
                            "max_tokens": 3000, "temperature": temperature})
    r.raise_for_status()
    res = r.json().get("result", {}).get("response")
    return res if isinstance(res, dict) else _parse_json(str(res))


def ai_json(prompt, cfg, prefer="gemini", temperature=0.4):
    order = [_gemini, _groq, _cloudflare] if prefer == "gemini" else [_groq, _gemini, _cloudflare]
    errs = []
    for fn in order:
        try:
            return fn(prompt, cfg, temperature)
        except Exception as e:
            errs.append(str(e)[:160])
    raise RuntimeError("all AI providers failed: " + " | ".join(errs))


# ============================================================================ watching sources

def plain_links(url):
    """Fallback without a browser: fetch the HTML and pull out the links."""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=90, verify=False)
    r.raise_for_status()
    out = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', r.text, re.S | re.I):
        href, inner = m.group(1).strip(), html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
        if href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        text = re.sub(r"\s+", " ", inner).strip()
        out.append((text, urljoin(url, href), text))
    return out


def page_links(page, url):
    """Render a page (many govt sites need JavaScript) and return [(text, absolute_url)]."""
    links = []
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as first:
            try:  # slow site: try the plain way before giving up
                got = plain_links(url)
                if got:
                    return got, None
            except Exception:
                pass
            raise first
        page.wait_for_timeout(2500)
        raw = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a => [
            (a.innerText || a.title || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g,' '),
            a.href, (a.closest('li,tr,p,div') || a).innerText.slice(0, 300).replace(/\\s+/g,' ')])""")
        for text, href, ctx in raw:
            if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            links.append((text or ctx[:150], urljoin(url, href), ctx))
        return links, None
    except Exception as e:
        return [], str(e)[:200]


def is_candidate(text, href, ctx):
    blob = f"{text} {ctx} {href}"
    if NO.search(text or "") or (not text and NO.search(href)):
        return False
    return bool(YES.search(blob)) and (href.lower().endswith(".pdf") or YES.search(text or "") or "recruit" in href.lower())


def watch(cfg, st, max_new, test_mode=False):
    from playwright.sync_api import sync_playwright
    sources = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    seen = st.setdefault("seen", {})
    health = st.setdefault("health", {})
    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-gpu"])
        ctx = browser.new_context(user_agent=UA, ignore_https_errors=True, locale="en-IN")
        page = ctx.new_page()
        for src in sources:
            links, err = page_links(page, src["url"])
            if err or not links:
                health[src["name"]] = {"ok": False, "error": err or "no links found", "at": now_ist().isoformat()}
                log(f"source {src['name']}: FAILED ({err or 'no links'})")
                continue
            health[src["name"]] = {"ok": True, "links": len(links), "at": now_ist().isoformat()}
            cands = [(t, u, c) for t, u, c in links if is_candidate(t, u, c)]
            key = src["name"]
            first_time = key not in seen
            known = set(seen.get(key, []))
            new = [(t, u, c) for t, u, c in cands if h(u) not in known]
            seen[key] = list(known | {h(u) for _, u, _ in cands})
            if first_time and not test_mode:
                log(f"source {key}: first check, remembered {len(cands)} existing notices (not posted)")
                continue
            if first_time and test_mode:  # test runs: try the 2 newest-looking notices from each source
                new = cands[:2]
            log(f"source {key}: {len(links)} links, {len(cands)} recruitment-like, {len(new)} new")
            for t, u, c in new:
                found.append({"source": key, "level": src["level"], "state": src["state"], "text": t, "url": u,
                              "context": c, "page": src["url"]})
        browser.close()
    bad = [k for k, v in health.items() if not v.get("ok")]
    if bad:
        log("Sources needing attention: " + ", ".join(bad))
    return found[:max_new]


# ============================================================================ reading a notice

DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def notice_text(url, work):
    """Text of a notice (PDF with OCR fallback, or a web page). Returns (text, kind)."""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60, stream=True, verify=False)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()
    data = b""
    for chunk in r.iter_content(1 << 16):
        data += chunk
        if len(data) > 20_000_000:
            raise RuntimeError("file too large")
    if url.lower().endswith(".pdf") or "pdf" in ctype or data[:4] == b"%PDF":
        pdf = work / f"n_{h(url)}.pdf"
        pdf.write_bytes(data)
        text = run(["pdftotext", "-layout", "-f", "1", "-l", "8", str(pdf), "-"], check=False)
        if len(re.sub(r"\s", "", text)) < 300:  # scanned PDF: read it with OCR
            log("scanned PDF, using OCR")
            base = work / f"ocr_{h(url)}"
            run(["pdftoppm", "-r", "200", "-f", "1", "-l", "4", "-png", str(pdf), str(base)], check=False)
            parts = []
            for img in sorted(work.glob(f"ocr_{h(url)}*.png")):
                parts.append(run(["tesseract", str(img), "-", "-l", "hin+eng", "--psm", "3"], check=False))
            text = "\n".join(parts)
        return text.translate(DEV_DIGITS), "pdf"
    page = data.decode(r.encoding or "utf-8", "ignore")
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))
    return text.translate(DEV_DIGITS)[:30000], "html"


def _num_in(text, n):
    if n is None:
        return False
    s = str(n).replace(",", "")
    t = text.replace(",", "")
    return re.search(rf"(?<![\d.]){re.escape(s)}(?![\d])", t) is not None


def date_in(text, iso):
    """Is this YYYY-MM-DD date written in the notice in any common format?"""
    try:
        d = dt.date.fromisoformat(iso)
    except Exception:
        return False
    t = text.lower()
    dd, mm, yy, y2 = d.day, d.month, d.year, d.year % 100
    pats = [rf"0?{dd}\s*[./\-]\s*0?{mm}\s*[./\-]\s*(?:{yy}|{y2:02d})",
            rf"0?{dd}(?:st|nd|rd|th)?\s*(?:{EN_MONTHS[mm - 1][:3]}[a-z]*|{HINDI_MONTHS[mm - 1]})[\s,.]*{yy}",
            rf"(?:{EN_MONTHS[mm - 1][:3]}[a-z]*)\s*0?{dd}(?:st|nd|rd|th)?[\s,.]*{yy}",
            rf"{yy}\s*[./\-]\s*0?{mm}\s*[./\-]\s*0?{dd}"]
    return any(re.search(p, t) for p in pats)


EXTRACT = """You read an official Indian government / university notice and extract recruitment facts.
Only use what the notice says. If something is not clearly written, use null. Never guess.

LINK TEXT: {link}
SOURCE: {source} ({state})
NOTICE TEXT (may be messy, Hindi and/or English):
{text}

Return JSON:
{{"is_recruitment": true/false (false for results, admit cards, answer keys, tenders, exam dates only),
  "org": "organisation name, short (e.g. UPSSSC, Bihar Police, AIIMS Patna)",
  "post_hi": "post name in simple Hindi (e.g. सिपाही, जूनियर असिस्टेंट, प्रोफेसर)",
  "post_en": "post name in English",
  "total_posts": number or null,
  "qualification_hi": "short, simple Hindi, e.g. 12वीं पास / ग्रेजुएट / B.Ed / MBBS",
  "age_min": number or null, "age_max": number or null,
  "fee_hi": "short Hindi, e.g. सामान्य ₹500, SC/ST ₹250, or null",
  "start_date": "YYYY-MM-DD or null", "last_date": "YYYY-MM-DD or null",
  "apply_link": "official apply URL if written, else null",
  "category": "police | teacher | health | court | railway | bank | university | clerk | engineer | defence | other",
  "importance": 1-10 (10 = huge all-India vacancy, 1 = single temporary post),
  "summary_hi": "2-3 short sentences in simple everyday Hindi, like telling a friend"}}"""


def extract(n, text, cfg):
    data = ai_json(EXTRACT.format(link=n["text"][:300], source=n["source"], state=n["state"], text=text[:14000]),
                   cfg, prefer="groq", temperature=0.1)
    if not data.get("is_recruitment"):
        return None
    # fact check: drop anything not found in the notice text
    for k in ("total_posts", "age_min", "age_max"):
        v = data.get(k)
        try:
            v = int(str(v).replace(",", "")) if v not in (None, "", "null") else None
        except Exception:
            v = None
        if v is not None and not _num_in(text, v):
            log(f"  dropped {k}={v} (not in notice)")
            v = None
        data[k] = v
    for k in ("start_date", "last_date"):
        if data.get(k) and not date_in(text, data[k]):
            log(f"  dropped {k}={data[k]} (not in notice)")
            data[k] = None
    if data.get("fee_hi"):
        fees = re.findall(r"\d[\d,]*", str(data["fee_hi"]))
        if any(not _num_in(text, f) for f in fees):
            data["fee_hi"] = None
    if data.get("last_date"):
        try:
            if dt.date.fromisoformat(data["last_date"]) < now_ist().date():
                log("  last date already passed, skipping")
                return None
        except Exception:
            data["last_date"] = None
    if data.get("apply_link") and not str(data["apply_link"]).startswith("http"):
        data["apply_link"] = None
    data["importance"] = max(1, min(10, int(data.get("importance") or 3)))
    return data


# ============================================================================ narration + scenes

def hi_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {HINDI_MONTHS[d.month - 1]} {d.year}"


def facts_numbers(f):
    nums = set()
    for k in ("total_posts", "age_min", "age_max"):
        if f.get(k) is not None:
            nums.add(str(f[k]))
    for k in ("start_date", "last_date"):
        if f.get(k):
            d = dt.date.fromisoformat(f[k])
            nums |= {str(d.day), str(d.year)}
    nums |= set(re.findall(r"\d+", str(f.get("fee_hi") or "").replace(",", "")))
    nums |= set(re.findall(r"\d+", str(f.get("qualification_hi") or "")))
    return nums


NARRATE = """Write the voice-over for a YouTube Short about a new government job, in simple, warm, everyday Hindi
(Devanagari script; common English words like post, form, online, last date are fine). Speak like a helpful elder
brother telling young job seekers. Use ONLY these facts - no other numbers, dates or claims:

{facts}

Return JSON with one short line (6-18 words) per key, null if that fact is missing:
{{"hook": "exciting first line saying the organisation has a new vacancy (with post count if known)",
  "posts": "line about number of posts and post name", "state": "line about which state/where",
  "qualification": "line about who can apply", "age": "line about age limit", "last_date": "line about the last date, urging not to miss it",
  "title": "YouTube title in Hindi+English, max 90 chars, e.g. 'UPSSSC भर्ती 2026: 2,500 पद | 12वीं पास | Last Date 24 Jul'"}}"""


def narration(f, cfg):
    facts = {k: f.get(k) for k in ("org", "post_hi", "total_posts", "qualification_hi", "age_min", "age_max",
                                   "fee_hi", "last_date", "state")}
    if f.get("last_date"):
        facts["last_date"] = hi_date(f["last_date"])
    lines = {}
    try:
        lines = ai_json(NARRATE.format(facts=json.dumps(facts, ensure_ascii=False)), cfg, prefer="gemini", temperature=0.6)
        allowed = facts_numbers(f) | {"2026", "2027"}
        for k, v in list(lines.items()):
            if k == "title" or not v:
                continue
            bad = set(re.findall(r"\d+", str(v).translate(DEV_DIGITS).replace(",", ""))) - allowed
            if bad:
                log(f"  narration '{k}' had unverified numbers {bad}; using template")
                lines[k] = None
    except Exception as e:
        log(f"  narration AI failed ({e}); using templates")
    org, post = f.get("org") or "", f.get("post_hi") or "विभिन्न"
    posts = f"{f['total_posts']:,}" if f.get("total_posts") else None
    tpl = {
        "hook": f"{org} ने {post} के {posts + ' ' if posts else ''}पदों पर नई भर्ती निकाली है!",
        "posts": f"कुल {posts} पदों पर भर्ती होगी, पद है {post}।" if posts else None,
        "state": f"यह भर्ती {STATE_HI.get(f.get('state'), f.get('state') or '')} के लिए है।",
        "qualification": f"योग्यता: {f['qualification_hi']}।" if f.get("qualification_hi") else None,
        "age": f"उम्र सीमा {f['age_min']} से {f['age_max']} साल रखी गई है।" if f.get("age_min") and f.get("age_max") else None,
        "last_date": f"आवेदन की आखिरी तारीख {hi_date(f['last_date'])} है, देर मत कीजिए!" if f.get("last_date") else None,
    }
    for k, v in tpl.items():
        if not lines.get(k) and v:
            lines[k] = v
        if (k == "posts" and not posts) or (k == "age" and not (f.get("age_min") and f.get("age_max"))) or \
                (k == "last_date" and not f.get("last_date")) or (k == "qualification" and not f.get("qualification_hi")):
            lines[k] = None
    return lines


ICON = {"police": "police", "teacher": "school", "health": "hospital", "court": "gavel", "railway": "train",
        "bank": "money", "university": "school", "clerk": "document", "engineer": "building", "defence": "police"}


def short_scenes(f, lines):
    sc = []
    org = (f.get("org") or "").upper()
    posts = f"{f['total_posts']:,}" if f.get("total_posts") else None
    sc.append({"type": "hook", "headline": f"{org} भर्ती" + (f" {posts} पद" if posts else ""), "highlight": org,
               "sub": f.get("post_hi") or "", "icon": ICON.get(f.get("category"), "building"), "stamp": "नई भर्ती",
               "doc_title": "नोटिफिकेशन",
               "narration": lines["hook"]})
    if posts and lines.get("posts"):
        sc.append({"type": "stat", "kicker": "कुल पद", "value": posts, "label": f.get("post_hi") or "",
                   "narration": lines["posts"]})
    st = f.get("state")
    if st and st != "India":
        sc.append({"type": "map", "kicker": "राज्य", "title": STATE_HI.get(st, st), "highlight": STATE_HI.get(st, st),
                   "view": "auto", "countries": [], "states": [{"name": st, "label": STATE_HI.get(st, st)}], "pins": [],
                   "narration": lines.get("state") or ""})
    else:
        sc.append({"type": "map", "kicker": "भर्ती", "title": "पूरे भारत से आवेदन", "highlight": "भारत", "view": "auto",
                   "countries": [{"iso3": "IND", "label": "INDIA"}], "states": [], "pins": [],
                   "narration": lines.get("state") or "इस भर्ती के लिए पूरे भारत से आवेदन किया जा सकता है।"})
    if lines.get("qualification"):
        sc.append({"type": "icon", "kicker": "योग्यता", "title": f.get("qualification_hi") or "", "icon": "school",
                   "narration": lines["qualification"]})
    if lines.get("age"):
        sc.append({"type": "versus", "title": "उम्र सीमा", "left_label": "न्यूनतम", "left_value": f"{f['age_min']} वर्ष",
                   "right_label": "अधिकतम", "right_value": f"{f['age_max']} वर्ष", "narration": lines["age"]})
    if lines.get("last_date"):
        d = dt.date.fromisoformat(f["last_date"])
        sc.append({"type": "icon", "kicker": "आखिरी तारीख", "title": hi_date(f["last_date"]), "icon": "calendar",
                   "date_text": f"{d.day} {EN_MONTHS[d.month - 1][:3].upper()}", "verdict": "देर मत करें!",
                   "verdict_color": "red", "narration": lines["last_date"]})
    for s in sc:
        if not s.get("narration"):
            s["narration"] = s.get("title") or s.get("headline") or ""
    return sc


# ============================================================================ voice, render, upload

async def _edge(text, voice, rate, out):
    import edge_tts
    try:
        c = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    except TypeError:
        c = edge_tts.Communicate(text, voice, rate=rate)
    words = []
    with open(out, "wb") as fh:
        async for ch in c.stream():
            if ch["type"] == "audio":
                fh.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
    return words


def tts(text, cfg, out):
    for _ in range(3):
        try:
            w = asyncio.run(_edge(text, cfg.get("voice", "hi-IN-MadhurNeural"), cfg.get("voice_rate", "+5%"), out))
            if Path(out).exists() and Path(out).stat().st_size > 1000:
                return w
        except Exception as e:
            log(f"voice retry: {e}")
            time.sleep(4)
    from gtts import gTTS
    gTTS(text, lang="hi").save(str(out))
    return []


def youtube(cfg):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=os.environ["YT_TOKEN"], client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"], token_uri="https://oauth2.googleapis.com/token")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def channel_info(cfg, work):
    try:
        sn = youtube(cfg).channels().list(part="snippet", mine=True).execute()["items"][0]["snippet"]
        url = (sn.get("thumbnails", {}).get("high") or {}).get("url")
        logo = None
        if url:
            from PIL import Image
            import io
            r = requests.get(re.sub(r"=s\d+", "=s800", url), timeout=30)
            logo = work / "logo.jpg"
            Image.open(io.BytesIO(r.content)).convert("RGB").save(logo, "JPEG", quality=92)
        return sn.get("title", ""), logo
    except Exception as e:
        log(f"channel info failed: {e}")
        return "", None


def render_video(scenes, cfg, work, title_line=None):
    title, logo = channel_info(cfg, work) if os.environ.get("YT_TOKEN") else ("", None)
    brand = (cfg.get("brand") or title or "SARKARI NAUKRI TURANT").upper()
    d = now_ist()
    brand_line = title_line or f"{brand} · {d.day} {EN_MONTHS[d.month - 1][:3].upper()}"
    scenes = [dict(s) for s in scenes] + [{"type": "outro", "line1": cfg.get("outro_line1", "सरकारी नौकरी"),
                                           "line2": cfg.get("outro_line2", "सबसे पहले"),
                                           "narration": cfg.get("outro_narration", "ऐसी ही सरकारी नौकरियों की सबसे पहले जानकारी के लिए चैनल को सब्सक्राइब करें।")}]
    wavs = []
    for i, sc in enumerate(scenes):
        mp3, wav = work / f"v{i:02d}.mp3", work / f"v{i:02d}.wav"
        sc["_words"] = tts(sc["narration"], cfg, mp3)
        run(["ffmpeg", "-y", "-i", str(mp3), "-af", f"apad=pad_dur={0.6 if sc['type'] == 'outro' else 0.3}",
             "-ar", "44100", "-ac", "1", str(wav)])
        sc["_dur"] = duration_of(wav)
        wavs.append(wav)
    (work / "voice.txt").write_text("".join(f"file '{p.name}'\n" for p in wavs))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "voice.txt", "-c", "copy", "voice.wav"], cwd=work)
    _, total = motion.render(scenes, {"logo": logo}, brand_line, work / "video.mp4", fps=30, log=log)
    run(["ffmpeg", "-y", "-i", "video.mp4", "-i", "voice.wav", "-filter_complex", "[1:a]loudnorm=I=-14:TP=-1.5[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.2f}",
         "-movflags", "+faststart", "final.mp4"], cwd=work)
    for p in list(work.glob("v[0-9]*.*")) + [work / "video.mp4", work / "voice.wav"]:
        p.unlink(missing_ok=True)
    return work / "final.mp4", total


def upload(video, title, desc, tags, cfg):
    from googleapiclient.http import MediaFileUpload
    body = {"snippet": {"title": re.sub(r"[<>]", "", title)[:100], "description": re.sub(r"[<>]", "", desc)[:4900],
                        "tags": tags[:15], "categoryId": str(cfg.get("category_id", "27")),
                        "defaultLanguage": "hi", "defaultAudioLanguage": "hi"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    req = youtube(cfg).videos().insert(part="snippet,status", body=body,
                                       media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 << 20, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    log(f"uploaded https://youtu.be/{resp['id']}")
    return resp["id"]


DISCLAIMER = ("⚠️ यह चैनल किसी भी सरकारी संस्था से संबद्ध नहीं है। आवेदन से पहले आधिकारिक नोटिफिकेशन ज़रूर पढ़ें।\n"
              "Not affiliated with any government body. Always read the official notification before applying.")


def description(f, n):
    rows = [f"📢 {f.get('org')} - {f.get('post_hi') or ''} ({f.get('post_en') or ''})", ""]
    if f.get("summary_hi"):
        rows += [f["summary_hi"], ""]
    if f.get("total_posts"):
        rows.append(f"🔢 कुल पद: {f['total_posts']:,}")
    if f.get("qualification_hi"):
        rows.append(f"🎓 योग्यता: {f['qualification_hi']}")
    if f.get("age_min") and f.get("age_max"):
        rows.append(f"🎂 आयु: {f['age_min']}-{f['age_max']} वर्ष")
    if f.get("fee_hi"):
        rows.append(f"💰 शुल्क: {f['fee_hi']}")
    if f.get("start_date"):
        rows.append(f"🗓️ आवेदन शुरू: {hi_date(f['start_date'])}")
    if f.get("last_date"):
        rows.append(f"⏰ आखिरी तारीख: {hi_date(f['last_date'])}")
    rows += ["", f"📄 आधिकारिक नोटिफिकेशन: {n['url']}"]
    if f.get("apply_link"):
        rows.append(f"🔗 आवेदन लिंक: {f['apply_link']}")
    rows += [f"🌐 स्रोत: {n['page']}", "", DISCLAIMER, "", "#SarkariNaukri #GovtJobs #SarkariJob #Bharti"]
    return "\n".join(rows)


# ============================================================================ jobs

def quota_left(st, cfg):
    today = now_ist().date().isoformat()
    if st.get("upload_day") != today:
        st["upload_day"], st["uploads_today"] = today, 0
    return int(cfg.get("max_uploads_per_day", 5)) - st.get("uploads_today", 0)


def job_watch(cfg, st, dry):
    work = OUT / f"watch-{now_ist():%Y%m%d-%H%M}"
    work.mkdir(parents=True, exist_ok=True)
    retry = st.pop("retry", []) if not dry else []  # notices that failed last time (e.g. AI limits)
    new = watch(cfg, st, int(cfg.get("max_new_per_run", 10)), test_mode=dry)
    log(f"{len(new)} new candidate notices, {len(retry)} to retry")
    todo = (retry + new)[:int(cfg.get("max_new_per_run", 10)) + 5]
    st["retry"] = (retry + new)[len(todo):]  # anything beyond this run's limit waits for the next run
    for n in todo:
        try:
            log(f"reading: [{n['source']}] {n['text'][:90]}")
            text, kind = notice_text(n["url"], work)
            if len(text.strip()) < 150:
                log("  too little text, skipping")
                continue
            f = extract(n, text, cfg)
            if not f:
                log("  not a recruitment notice")
                continue
            f.setdefault("state", n["state"])
            if n["state"] != "India":
                f["state"] = n["state"]
            st.setdefault("notices", []).append({**n, "facts": f, "found": now_ist().isoformat(),
                                                 "short": None, "digest": None})
            log(f"  ✓ {f.get('org')} | {f.get('post_en')} | posts={f.get('total_posts')} | last={f.get('last_date')} | imp={f['importance']}")
        except Exception as e:
            log(f"  failed: {str(e)[:200]}")
            if "AI providers failed" in str(e) and n.get("tries", 0) < 6:
                n["tries"] = n.get("tries", 0) + 1
                st.setdefault("retry", []).append(n)  # try again in 2 hours
    # post Shorts for the most important pending notices
    min_imp = int(cfg.get("short_min_importance", 5))
    keep_for_digest = 1  # always leave one upload for the evening digest
    pending = sorted([x for x in st.get("notices", []) if not x.get("short") and x["facts"]["importance"] >= min_imp
                      and x["found"][:10] == now_ist().date().isoformat()],
                     key=lambda x: -x["facts"]["importance"])
    per_run = int(cfg.get("shorts_per_run", 2))
    for x in pending[:per_run]:
        if not dry and quota_left(st, cfg) <= keep_for_digest:
            log("daily upload limit reached; the rest go in the digest")
            break
        f = x["facts"]
        sw = work / f"short-{h(x['url'])}"
        sw.mkdir(exist_ok=True)
        lines = narration(f, cfg)
        video, dur = render_video(short_scenes(f, lines), cfg, sw)
        title = lines.get("title") or f"{f.get('org')} भर्ती: {f.get('post_hi') or ''}"
        if "#shorts" not in title.lower():
            title = f"{title[:88]} #Shorts"
        (sw / "upload.json").write_text(json.dumps({"title": title, "description": description(f, x)}, ensure_ascii=False, indent=1))
        if dry:
            log(f"dry run: Short saved {video}")
            x["short"] = "dry-run"
            continue
        vid = upload(video, title, description(f, x), ["सरकारी नौकरी", "sarkari naukri", "govt jobs", f.get("org") or ""], cfg)
        st["uploads_today"] = st.get("uploads_today", 0) + 1
        x["short"] = vid


def job_digest(cfg, st, dry):
    today = now_ist().date().isoformat()
    items = [x for x in st.get("notices", []) if x["found"][:10] == today and not x.get("digest")]
    if len(items) < int(cfg.get("digest_min_items", 2)):
        log(f"only {len(items)} notices today; no digest")
        return
    items = sorted(items, key=lambda x: -x["facts"]["importance"])[:int(cfg.get("digest_max_items", 15))]
    work = OUT / f"digest-{now_ist():%Y%m%d}"
    work.mkdir(parents=True, exist_ok=True)
    d = now_ist()
    scenes = [{"type": "headline", "kicker": f"{d.day} {HINDI_MONTHS[d.month - 1]} {d.year}",
               "headline": f"आज की {len(items)} नई सरकारी भर्तियां", "highlight": f"{len(items)}",
               "narration": f"नमस्कार! आज {len(items)} नई सरकारी भर्तियां आई हैं। चलिए एक-एक करके देखते हैं।"}]
    for x in items:
        f = x["facts"]
        bits = []
        if f.get("total_posts"):
            bits.append(f"{f['total_posts']:,} पद")
        if f.get("last_date"):
            bits.append(f"अंतिम तिथि {hi_date(f['last_date'])}")
        say = f.get("summary_hi") or f"{f.get('org')} में {f.get('post_hi') or ''} की भर्ती।"
        allowed = facts_numbers(f) | {"2026", "2027"}
        if set(re.findall(r"\d+", say.translate(DEV_DIGITS).replace(",", ""))) - allowed:
            say = f"{f.get('org')} में {f.get('post_hi') or ''} की नई भर्ती आई है।"
        scenes.append({"type": "icon", "kicker": f"{f.get('org')} · {STATE_HI.get(f.get('state'), '')}",
                       "title": f.get("post_hi") or f.get("post_en") or "", "icon": ICON.get(f.get("category"), "document"),
                       "doc_title": "नोटिफिकेशन",
                       "verdict": " | ".join(bits[:1]) or None, "verdict_color": "yellow",
                       "sub": bits[1] if len(bits) > 1 else (f.get("qualification_hi") or ""), "narration": say})
    video, dur = render_video(scenes, cfg, work)
    title = f"आज की सरकारी भर्तियां {d.day} {HINDI_MONTHS[d.month - 1]} {d.year} | {len(items)} New Govt Jobs | UP Bihar Rajasthan Haryana Delhi"
    lines = [f"आज ({d.day} {HINDI_MONTHS[d.month - 1]}) की सभी नई सरकारी भर्तियां:", ""]
    for i, x in enumerate(items, 1):
        f = x["facts"]
        extra = f" - {f['total_posts']:,} पद" if f.get("total_posts") else ""
        last = f" - अंतिम तिथि {hi_date(f['last_date'])}" if f.get("last_date") else ""
        lines.append(f"{i}. {f.get('org')}: {f.get('post_hi') or f.get('post_en')}{extra}{last}\n   📄 {x['url']}")
    desc = "\n".join(lines + ["", DISCLAIMER, "", "#SarkariNaukri #GovtJobs #TodayJobs"])
    (work / "upload.json").write_text(json.dumps({"title": title, "description": desc}, ensure_ascii=False, indent=1))
    if dry:
        log(f"dry run: digest saved {video}")
        return
    if quota_left(st, cfg) <= 0:
        log("no uploads left today for the digest")
        return
    vid = upload(video, title[:100], desc, ["सरकारी नौकरी", "govt jobs today", "sarkari result", "naukri"], cfg)
    st["uploads_today"] = st.get("uploads_today", 0) + 1
    for x in items:
        x["digest"] = vid


def selftest():
    cfg = load_cfg()
    work = OUT / "selftest"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    f = {"org": "UPSSSC", "post_hi": "जूनियर असिस्टेंट", "post_en": "Junior Assistant", "total_posts": 2702,
         "qualification_hi": "12वीं पास + टाइपिंग", "age_min": 18, "age_max": 40, "last_date": "2026-12-24",
         "state": "Uttar Pradesh", "category": "clerk", "importance": 8}
    lines = narration(f, cfg)
    sc = short_scenes(f, lines)
    for s in sc:
        s["_dur"], s["_words"] = 2.5, []
    sc.append({"type": "outro", "line1": "सरकारी नौकरी", "line2": "सबसे पहले", "_dur": 2.0, "_words": [], "narration": ""})
    motion.render(sc, {}, "SARKARI NAUKRI TURANT · 1 NOV", work / "video.mp4", log=log)
    log(f"selftest video: {work / 'video.mp4'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["watch", "digest", "selftest"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.job == "selftest":
        return selftest()
    import urllib3
    urllib3.disable_warnings()
    cfg, st = load_cfg(), load_state()
    try:
        (job_watch if a.job == "watch" else job_digest)(cfg, st, a.dry_run)
    finally:
        save_state(st)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""distill · 材料准备：把各种输入变成带定位标记的纯文本，并登记到 manifest.json。

只用标准库。PDF 需要系统里有 pdftotext（poppler）；扫描版 OCR 需要 macOS + pdftoppm。

用法：
  prep.py init   <工作区>
  prep.py add    <工作区> <文件> [--title 标题] [--ocr] [--page-marker 正则]
                                                         # pdf / epub / docx / txt / md / html / srt / vtt
                 --ocr           这份文本本身是 OCR 或自动转写出来的（核对时对错字宽容）
                 --page-marker   文本里已有分页行时，用正则把它们转成页码标记；
                                 第一个括号要抓到页码数字，例如 "^=== 原书扫描件第 (\\d+) 页 ===$"
  prep.py add-url <工作区> <URL> --kind <类型> [--from-file F --not-verbatim]
  prep.py ocr    <工作区> <扫描版.pdf> [--title 标题] [--dpi 200]
  prep.py plan   <工作区> [--max-chars 40000]            # 按 chapters.json 排阅读批次
  prep.py status <工作区>
  prep.py sources <工作区>                               # 输出 Markdown 的材料清单（编号、材料、类型、链接），贴到成品末尾
  prep.py remove <工作区> <ID>                           # 去掉一份登记错的或用不上的材料
  prep.py set-kind <工作区> <ID> <类型>                  # 改一份网页材料的类型（登记时选错了）

定位标记（写在 text/<ID>.txt 里，单独占一行）：
  ⟦p45⟧     PDF 第 45 页（文件页序，不是印刷页码）
  ⟦c3⟧      EPUB 第 3 个正文文件
  ⟦t12:34⟧  音视频 12 分 34 秒
  ⟦页眉 …⟧  每页重复出现的页眉页脚，核对引文时会跳过它
  没有标记的格式（txt / md / docx / html / 网页）用行号定位：L120

退出码：0 成功；2 用法或输入错误；3 缺外部工具；4 抓取或解析失败。
"""
import argparse
import datetime
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

UA = "distill-skill/0.1 (personal reading tool)"
WEB_KINDS = ["全文", "试读", "目录", "作者访谈", "作者文章", "演讲", "长书评", "短评", "简介", "他人摘要", "百科", "其他"]
HEADING_RES = [
    re.compile(r"^\s*第\s*[一二三四五六七八九十百零〇两\d]+\s*[章部篇讲课卷回]"),
    re.compile(r"^\s*(chapter|part|lecture|lesson)\s+([0-9]+|[ivxlc]+)\b", re.I),
    re.compile(r"^\s*#{1,3}\s+\S"),
    re.compile(r"^\s*(前言|序言|自序|引言|导言|导论|绪论|结语|后记|跋|致谢|附录|参考文献|译后记|introduction|preface|foreword|prologue|epilogue|conclusion|acknowledg(e)?ments|appendix|bibliography|notes|index)\s*$", re.I),
]


def die(msg, code=2):
    print("错误：" + msg, file=sys.stderr)
    sys.exit(code)


# ---------- manifest ----------

def mpath(wd):
    return os.path.join(wd, "manifest.json")


def load_manifest(wd):
    if not os.path.isfile(mpath(wd)):
        die(f"{wd} 不是工作区（没有 manifest.json）。先跑 prep.py init。")
    with open(mpath(wd), encoding="utf-8") as f:
        return json.load(f)


def save_manifest(wd, m):
    with open(mpath(wd), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def next_id(m, prefix):
    n = 1 + sum(1 for s in m["sources"] if s["id"].startswith(prefix))
    return f"{prefix}{n}" if prefix == "S" else f"{prefix}{n:02d}"


# ---------- 文本抽取 ----------

class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article", "table", "ul", "ol", "pre", "hr", "dt", "dd", "figcaption"}
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "aside", "iframe", "button", "select"}

    def __init__(self, keep_chrome=False):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.skipset = {"script", "style", "noscript", "svg"} if keep_chrome else self.SKIP
        self.title = ""
        self._in_title = False
        self._heading = None

    def handle_starttag(self, tag, attrs):
        if tag in self.skipset:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self.skipset and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if not self.skip:
            self.out.append(data)

    def text(self):
        t = "".join(self.out).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
        out, blank = [], False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip() + "\n"


def html_to_text(raw, keep_chrome=False):
    p = _Text(keep_chrome=keep_chrome)
    try:
        p.feed(raw)
    except Exception:
        pass
    return p.text(), p.title.strip()


def read_text_file(path):
    data = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_pdf(path):
    if not shutil.which("pdftotext"):
        die("需要 pdftotext（poppler）。macOS：brew install poppler。装之前先问用户。", 3)
    r = subprocess.run(["pdftotext", "-enc", "UTF-8", path, "-"], capture_output=True)
    if r.returncode != 0:
        die("pdftotext 失败：" + r.stderr.decode("utf-8", "replace")[:300], 4)
    pages = r.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    sizes = [len(re.sub(r"\s", "", p)) for p in pages]
    out = []
    for i, p in enumerate(pages, 1):
        out.append(f"⟦p{i}⟧")
        out.append(p.strip("\n"))
    med = statistics.median(sizes) if sizes else 0
    info = {"pages": len(pages), "median_chars_per_page": med,
            "needs_ocr": bool(pages) and med < 60, "locator": "p"}
    return "\n".join(out) + "\n", info


def extract_epub(path):
    try:
        z = zipfile.ZipFile(path)
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").attrib["full-path"]
        opf = ET.fromstring(z.read(opf_path))
    except Exception as e:
        die(f"EPUB 解析失败：{e}", 4)
    base = os.path.dirname(opf_path)
    items = {it.attrib["id"]: it.attrib.get("href", "") for it in opf.findall(".//{*}manifest/{*}item")}
    spine = [it.attrib.get("idref") for it in opf.findall(".//{*}spine/{*}itemref")]
    title_el = opf.find(".//{*}metadata/{*}title")
    out, n = [], 0
    for idref in spine:
        href = items.get(idref)
        if not href:
            continue
        name = os.path.normpath(os.path.join(base, urllib.request.url2pathname(href.split("#")[0]))).replace("\\", "/")
        try:
            raw = z.read(name).decode("utf-8", "replace")
        except KeyError:
            continue
        text, _ = html_to_text(raw, keep_chrome=True)
        if not text.strip():
            continue
        n += 1
        out.append(f"⟦c{n}⟧")
        out.append(text.strip("\n"))
    info = {"spine_files": n, "locator": "c", "needs_ocr": False,
            "title_meta": (title_el.text or "").strip() if title_el is not None else ""}
    return "\n".join(out) + "\n", info


def extract_docx(path):
    try:
        z = zipfile.ZipFile(path)
        root = ET.fromstring(z.read("word/document.xml"))
    except Exception as e:
        die(f"DOCX 解析失败：{e}", 4)
    paras = []
    for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        t = "".join(x.text or "" for x in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        paras.append(t)
    return "\n".join(paras) + "\n", {"locator": "L", "needs_ocr": False}


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,]\d+\s*-->")


def extract_subs(path):
    raw = read_text_file(path)
    out, last_mark, buf, prev = [], -999, [], None
    for ln in raw.splitlines():
        m = _TS.search(ln)
        if m:
            sec = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if sec - last_mark >= 30:
                if buf:
                    out.append(" ".join(buf))
                    buf = []
                mm, ss = divmod(sec, 60)
                out.append(f"⟦t{mm}:{ss:02d}⟧")
                last_mark = sec
            continue
        ln = re.sub(r"<[^>]+>", "", ln).strip()
        if not ln or ln.isdigit() or ln.upper().startswith(("WEBVTT", "KIND:", "LANGUAGE:", "NOTE")):
            continue
        if ln == prev:          # 自动字幕常见的滚动重复
            continue
        prev = ln
        buf.append(ln)
    if buf:
        out.append(" ".join(buf))
    return "\n".join(out) + "\n", {"locator": "t", "needs_ocr": False}


def extract_any(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return extract_pdf(path) + ("pdf",)
    if ext == ".epub":
        return extract_epub(path) + ("epub",)
    if ext == ".docx":
        return extract_docx(path) + ("docx",)
    if ext in (".srt", ".vtt"):
        return extract_subs(path) + ("subs",)
    if ext in (".html", ".htm", ".xhtml"):
        text, _ = html_to_text(read_text_file(path))
        return text, {"locator": "L", "needs_ocr": False}, "html"
    if ext in (".txt", ".md", ".markdown", ".org", ".rst", ""):
        raw = read_text_file(path).replace("\r\n", "\n").replace("\r", "\n")
        info = {"locator": "L", "needs_ocr": False}
        if re.search(r"^\[?\d{1,2}:\d{2}(:\d{2})?\]?", raw, re.M) and len(re.findall(r"^\[?\d{1,2}:\d{2}", raw, re.M)) > 10:
            info["has_inline_timestamps"] = True
        return raw, info, "text"
    die(f"不支持的格式：{ext}。支持 pdf / epub / docx / txt / md / html / srt / vtt；其他格式请先转成其中一种。")


PAGE_HINTS = [
    re.compile(r"^\W*(?:原书|扫描件|PDF|pdf)?[^\d\n]{0,12}第\s*(\d{1,4})\s*页\W*$"),
    re.compile(r"^\W*(?:page|p\.)\s*(\d{1,4})\W*$", re.I),
    re.compile(r"^\W{2,}\s*(\d{1,4})\s*\W{2,}$"),
]


def page_hint(text):
    """纯文本里有没有成批的分页行。返回 (条数, 例子) 或 None。"""
    hits = [ln.strip() for ln in text.split("\n") if 0 < len(ln.strip()) <= 40 and any(r.match(ln.strip()) for r in PAGE_HINTS)]
    return (len(hits), hits[0]) if len(hits) >= 5 else None


def apply_page_marker(text, pattern):
    try:
        rx = re.compile(pattern)
    except re.error as e:
        die(f"--page-marker 不是合法的正则：{e}")
    if rx.groups < 1:
        die("--page-marker 的正则里要有一个括号抓页码数字，例如 \"^=== 第 (\\d+) 页 ===$\"")
    out, n = [], 0
    for ln in text.split("\n"):
        m = rx.match(ln.strip())
        if m and m.group(1).isdigit():
            out.append(f"⟦p{int(m.group(1))}⟧")
            n += 1
        else:
            out.append(ln)
    if n == 0:
        die("--page-marker 一行也没匹配上。先看一眼文本里分页行的原样。")
    return "\n".join(out), n


def mark_running_heads(text):
    """把每页开头或结尾反复出现的页眉页脚标出来，核对引文时跳过，跨页的句子才对得上。"""
    lines = text.split("\n")
    idx = [i for i, ln in enumerate(lines) if re.match(r"^⟦p\d+⟧$", ln)]
    if len(idx) < 6:
        return text, 0
    def key(s):
        return re.sub(r"[\d\s]+", "", s)
    cand = {}
    for n, i in enumerate(idx):
        end = idx[n + 1] if n + 1 < len(idx) else len(lines)
        body = [j for j in range(i + 1, end) if lines[j].strip()]
        for j in body[:2] + body[-1:]:
            t = lines[j].strip()
            if len(t) <= 30:
                cand.setdefault(key(t), set()).add(j)
    n = 0
    for k, js in cand.items():
        if len(js) >= 4 or (k == "" and len(js) >= 4):      # k 为空＝纯页码行
            for j in js:
                lines[j] = f"⟦页眉 {lines[j].strip()}⟧"
                n += 1
    return "\n".join(lines), n


def heading_candidates(text, limit=400):
    out = []
    for i, ln in enumerate(text.split("\n"), 1):
        s = ln.strip()
        if not s or len(s) > 60 or s.startswith("⟦"):
            continue
        if any(r.search(s) for r in HEADING_RES):
            out.append({"line": i, "text": s})
            if len(out) >= limit:
                break
    return out


def count_chars(text):
    return len(re.sub(r"\s|⟦[^⟧\n]*⟧", "", text))


def register(wd, m, sid, text, info, fmt, origin, title, extra=None):
    os.makedirs(os.path.join(wd, "text"), exist_ok=True)
    tpath = os.path.join(wd, "text", f"{sid}.txt")
    with open(tpath, "w", encoding="utf-8") as f:
        f.write(text)
    entry = {"id": sid, "title": title, "format": fmt, "origin": origin,
             "text": f"text/{sid}.txt", "chars": count_chars(text),
             "lines": text.count("\n") + 1,
             "added": datetime.date.today().isoformat()}
    entry.update(info)
    if extra:
        entry.update(extra)
    toks = re.findall(r"[A-Za-z]+", text[:200000])
    if len(toks) > 300 and sum(1 for t in toks if len(t) <= 2) / len(toks) > 0.55:
        entry["garbled"] = True
    heads = heading_candidates(text)
    with open(os.path.join(wd, "text", f"{sid}.headings.json"), "w", encoding="utf-8") as f:
        json.dump(heads, f, ensure_ascii=False, indent=1)
    entry["heading_candidates"] = len(heads)
    m["sources"] = [s for s in m["sources"] if s["id"] != sid] + [entry]
    save_manifest(wd, m)
    return entry


def report(entry):
    print(f"已登记 {entry['id']}：{entry['title']}")
    print(f"  文本：{entry['text']}（{entry['chars']} 字，{entry['lines']} 行）")
    if entry.get("pages"):
        print(f"  页数：{entry['pages']}，每页字数中位数 {entry['median_chars_per_page']}")
    loc = {"p": "页码 ⟦pN⟧ → 锚写 p45", "c": "正文文件 ⟦cN⟧ → 锚写 c3", "t": "时间 ⟦tM:SS⟧ → 锚写 t12:34", "L": "行号 → 锚写 L120"}
    print(f"  定位方式：{loc.get(entry.get('locator'), '行号')}")
    print(f"  疑似标题行：{entry['heading_candidates']} 处（text/{entry['id']}.headings.json）")
    if entry.get("page_hint"):
        n, ex = entry["page_hint"]
        print(f"  ⚠ 文本里有 {n} 处像分页行的内容，例如「{ex}」。想让出处写成页码，重新登记并加 --page-marker <正则>（括号抓页码数字），同时加 --id {entry['id']} 覆盖本条；文本是 OCR 或自动转写出来的，一并加 --ocr。")
    if entry.get("running_heads"):
        print(f"  已标出 {entry['running_heads']} 行重复的页眉页脚（⟦页眉 …⟧），核对引文时会跳过。")
    if entry.get("ocr"):
        print("  已记为 OCR／自动转写文本：会有错字，引文尽量挑没有错字的句子；核对报「近似」时人工看一眼。")
    if entry.get("needs_ocr"):
        print("  ⚠ 文本层几乎是空的，多半是扫描版。改用：prep.py ocr <工作区> <文件>")
    if entry.get("garbled"):
        print("  ⚠ 英文单词被拆成了零碎字母，文本层多半有字符间距问题。先抽看几页原文再决定：换 EPUB、换版本，或改用 ocr。")
    if entry["chars"] < 300 and not entry.get("needs_ocr"):
        print("  ⚠ 内容很短（不足 300 字），确认是不是拿错了文件。")
    if entry.get("verbatim") is False:
        print("  ⚠ 标记为非逐字来源：不得从它摘直接引语。")


# ---------- 子命令 ----------

def cmd_init(a):
    wd = a.workdir
    for d in ("text", "notes", "verify"):
        os.makedirs(os.path.join(wd, d), exist_ok=True)
    if not os.path.isfile(mpath(wd)):
        save_manifest(wd, {"created": datetime.date.today().isoformat(), "sources": []})
    print(f"工作区就绪：{os.path.abspath(wd)}")


def cmd_add(a):
    m = load_manifest(a.workdir)
    if not os.path.isfile(a.file):
        die(f"找不到文件：{a.file}")
    if os.path.getsize(a.file) == 0:
        die("文件是空的。")
    text, info, fmt = extract_any(a.file)
    if a.page_marker:
        if info.get("locator") != "L":
            die("--page-marker 只用于纯文本类材料（txt / md / docx / html）。")
        text, n = apply_page_marker(text, a.page_marker)
        info["locator"] = "p"
        info["pages"] = n
        info["median_chars_per_page"] = round(count_chars(text) / n)
    elif fmt == "text":
        h = page_hint(text)
        if h:
            info["page_hint"] = h
    if info.get("locator") == "p":
        text, nh = mark_running_heads(text)
        if nh:
            info["running_heads"] = nh
    if a.ocr:
        info["ocr"] = True
    title = a.title or info.pop("title_meta", "") or os.path.splitext(os.path.basename(a.file))[0]
    info.pop("title_meta", None)
    entry = register(a.workdir, m, a.id or next_id(m, "S"), text, info, fmt,
                     os.path.abspath(a.file), title, {"kind": "用户提供", "verbatim": True})
    report(entry)


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read(8_000_000)
    m = re.search(r"charset=([\w-]+)", ctype)
    enc = m.group(1) if m else None
    if not enc:
        m2 = re.search(rb"charset=[\"']?([\w-]+)", data[:4000])
        enc = m2.group(1).decode() if m2 else "utf-8"
    try:
        raw = data.decode(enc, "replace")
    except LookupError:
        raw = data.decode("utf-8", "replace")
    return raw, ctype


def cmd_add_url(a):
    m = load_manifest(a.workdir)
    if a.kind not in WEB_KINDS:
        die("--kind 必须是其中之一：" + " / ".join(WEB_KINDS))
    title = a.title or ""
    if a.from_file:
        text = read_text_file(a.from_file)
        verbatim = not a.not_verbatim
    else:
        if a.not_verbatim:
            die("--not-verbatim 只和 --from-file 一起用。")
        try:
            raw, ctype = fetch_url(a.url)
        except Exception as e:
            die(f"抓取失败：{e}。可换抓取方式后用 --from-file 登记；经过概括的内容要加 --not-verbatim。", 4)
        if "html" in ctype or raw.lstrip()[:1] == "<":
            text, t = html_to_text(raw)
            title = title or t
        else:
            text = raw
        verbatim = True
    if count_chars(text) < 400 and not a.from_file:
        print("⚠ 抓到的正文不足 400 字，页面可能要登录、被拦截或靠脚本渲染。这份来源多半不能用。", file=sys.stderr)
    sid = a.id or next_id(m, "W")
    head = f"来源：{a.url}\n类型：{a.kind}\n抓取：{datetime.date.today().isoformat()}\n逐字：{'是' if verbatim else '否（经过概括，不得摘引语）'}\n----\n"
    entry = register(a.workdir, m, sid, head + text, {"locator": "L", "needs_ocr": False}, "web",
                     a.url, title or a.url, {"kind": a.kind, "verbatim": verbatim, "url": a.url})
    report(entry)


def cmd_ocr(a):
    if sys.platform != "darwin":
        die("内置 OCR 只支持 macOS（用系统自带的文字识别，不联网）。其他系统请自行 OCR 成 txt 后用 add 登记。", 3)
    for tool in ("pdftoppm", "osascript"):
        if not shutil.which(tool):
            die(f"需要 {tool}。", 3)
    m = load_manifest(a.workdir)
    js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.js")
    tmp = tempfile.mkdtemp(prefix="distill-ocr-")
    try:
        print("正在把 PDF 转成图片……")
        r = subprocess.run(["pdftoppm", "-r", str(a.dpi), "-png", a.file, os.path.join(tmp, "pg")], capture_output=True)
        if r.returncode != 0:
            die("pdftoppm 失败：" + r.stderr.decode("utf-8", "replace")[:300], 4)
        pngs = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
        if not pngs:
            die("没有转出任何页面。", 4)
        print(f"共 {len(pngs)} 页，开始识别（每 20 页报一次进度）……")
        for i in range(0, len(pngs), 20):
            batch = [os.path.join(tmp, p) for p in pngs[i:i + 20]]
            r = subprocess.run(["osascript", "-l", "JavaScript", js, tmp] + batch, capture_output=True)
            if r.returncode != 0:
                die("OCR 失败：" + r.stderr.decode("utf-8", "replace")[:300], 4)
            print(f"  {min(i + 20, len(pngs))}/{len(pngs)}")
        out, sizes = [], []
        for i, p in enumerate(pngs, 1):
            tp = os.path.join(tmp, p[:-4] + ".txt")
            t = open(tp, encoding="utf-8").read() if os.path.isfile(tp) else ""
            sizes.append(len(re.sub(r"\s", "", t)))
            out.append(f"⟦p{i}⟧")
            out.append(t.strip("\n"))
        info = {"pages": len(pngs), "median_chars_per_page": statistics.median(sizes),
                "needs_ocr": False, "locator": "p", "ocr": True}
        joined, nh = mark_running_heads("\n".join(out))
        out = joined.split("\n")
        if nh:
            info["running_heads"] = nh
        title = a.title or os.path.splitext(os.path.basename(a.file))[0]
        entry = register(a.workdir, m, a.id or next_id(m, "S"), "\n".join(out) + "\n", info, "pdf-ocr",
                         os.path.abspath(a.file), title, {"kind": "用户提供", "verbatim": True})
        report(entry)
        print("  注意：这是 OCR 文本，会有错字；图表里的字和版式可能丢失。引文核对出现「近似」多半是 OCR 差异。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_plan(a):
    m = load_manifest(a.workdir)
    cpath = os.path.join(a.workdir, "chapters.json")
    if not os.path.isfile(cpath):
        die("还没有 chapters.json。先建全书地图（见 references/02-map-extract.md）。")
    chapters = json.load(open(cpath, encoding="utf-8"))
    texts = {}
    rows, batch, batch_chars, bid = [], [], 0, 1

    def flush():
        nonlocal batch, batch_chars, bid
        if batch:
            rows.append((f"B{bid:02d}", batch, batch_chars))
            bid += 1
            batch, batch_chars = [], 0

    for ch in chapters:
        if ch.get("class") in ("skip", "nomaterial"):
            continue
        sid = ch["source"]
        if sid not in texts:
            src = next((s for s in m["sources"] if s["id"] == sid), None)
            if not src:
                die(f"chapters.json 里的来源 {sid} 不在 manifest 里。")
            texts[sid] = open(os.path.join(a.workdir, src["text"]), encoding="utf-8").read().split("\n")
        lines = texts[sid]
        s, e = ch["start_line"], min(ch["end_line"], len(lines))
        n = count_chars("\n".join(lines[s - 1:e]))
        if n > a.max_chars:            # 大章单独成批并切段
            flush()
            seg_start, acc = s, 0
            for i in range(s, e + 1):
                acc += count_chars(lines[i - 1])
                if acc >= a.max_chars and not lines[i - 1].strip():
                    rows.append((f"B{bid:02d}", [(ch["id"], sid, seg_start, i)], acc))
                    bid += 1
                    seg_start, acc = i + 1, 0
            if seg_start <= e:
                rows.append((f"B{bid:02d}", [(ch["id"], sid, seg_start, e)], acc))
                bid += 1
            continue
        if batch_chars + n > a.max_chars:
            flush()
        batch.append((ch["id"], sid, s, e))
        batch_chars += n
    flush()
    total = sum(r[2] for r in rows)
    print(f"阅读批次：{len(rows)} 批，共约 {total} 字（跳过的章节不计）")
    print("| 批次 | 章节 | 文件与行号范围 | 字数 |")
    print("|---|---|---|---|")
    for b, parts, n in rows:
        chs = "、".join(dict.fromkeys(p[0] for p in parts))
        rng = "；".join(f"text/{p[1]}.txt L{p[2]}–L{p[3]}" for p in parts)
        print(f"| {b} | {chs} | {rng} | {n} |")
    with open(os.path.join(a.workdir, "verify", "plan.json"), "w", encoding="utf-8") as f:
        json.dump([{"batch": b, "parts": [dict(zip(("chapter", "source", "start_line", "end_line"), p)) for p in parts], "chars": n} for b, parts, n in rows], f, ensure_ascii=False, indent=1)


def cmd_status(a):
    m = load_manifest(a.workdir)
    if not m["sources"]:
        print("还没有登记任何材料。")
        return
    print("| ID | 标题 | 类型 | 格式 | 字数 | 定位 | 逐字 |")
    print("|---|---|---|---|---|---|---|")
    for s in m["sources"]:
        print(f"| {s['id']} | {s['title'][:30]} | {s.get('kind', '')} | {s['format']} | {s['chars']} | {s.get('locator', 'L')} | {'是' if s.get('verbatim', True) else '否'} |")
    wd = a.workdir
    for name in ("map.md", "chapters.json", "digest.md", "digest.html", "rulebook.md"):
        print(f"{name}：{'有' if os.path.isfile(os.path.join(wd, name)) else '无'}")
    notes = [f for f in os.listdir(os.path.join(wd, "notes"))] if os.path.isdir(os.path.join(wd, "notes")) else []
    print(f"底稿文件：{len([n for n in notes if n.endswith('.md')])} 个")


def cmd_sources(a):
    m = load_manifest(a.workdir)
    print("| 编号 | 材料 | 类型 | 链接或说明 |")
    print("|---|---|---|---|")
    for s in m["sources"]:
        note = s.get("url") or ("用户提供的文件" + ("（OCR 文本）" if s.get("ocr") else ""))
        if s.get("verbatim") is False:
            note += "（内容经过概括，未作引语）"
        print(f"| {s['id']} | {s['title'][:50].replace('|', '｜')} | {s.get('kind', '')} | {note} |")


def cmd_remove(a):
    m = load_manifest(a.workdir)
    hit = [s for s in m["sources"] if s["id"] == a.id]
    if not hit:
        die(f"没有编号为 {a.id} 的材料。")
    m["sources"] = [s for s in m["sources"] if s["id"] != a.id]
    save_manifest(a.workdir, m)
    for name in (f"{a.id}.txt", f"{a.id}.headings.json"):
        fp = os.path.join(a.workdir, "text", name)
        if os.path.isfile(fp):
            os.remove(fp)
    print(f"已去掉 {a.id}：{hit[0]['title']}。底稿和成品里如果还有指向它的锚，verify.py 会报出来。")


def cmd_set_kind(a):
    m = load_manifest(a.workdir)
    if a.kind not in WEB_KINDS:
        die("类型必须是其中之一：" + " / ".join(WEB_KINDS))
    hit = [s for s in m["sources"] if s["id"] == a.id]
    if not hit:
        die(f"没有编号为 {a.id} 的材料。")
    hit[0]["kind"] = a.kind
    save_manifest(a.workdir, m)
    print(f"{a.id} 的类型已改为：{a.kind}")


def main():
    ap = argparse.ArgumentParser(description="distill 材料准备")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("workdir"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("add"); p.add_argument("workdir"); p.add_argument("file")
    p.add_argument("--title"); p.add_argument("--id"); p.add_argument("--ocr", action="store_true")
    p.add_argument("--page-marker"); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("add-url"); p.add_argument("workdir"); p.add_argument("url")
    p.add_argument("--kind", required=True); p.add_argument("--title"); p.add_argument("--id")
    p.add_argument("--from-file"); p.add_argument("--not-verbatim", action="store_true"); p.set_defaults(fn=cmd_add_url)
    p = sub.add_parser("ocr"); p.add_argument("workdir"); p.add_argument("file")
    p.add_argument("--title"); p.add_argument("--id"); p.add_argument("--dpi", type=int, default=200); p.set_defaults(fn=cmd_ocr)
    p = sub.add_parser("plan"); p.add_argument("workdir"); p.add_argument("--max-chars", type=int, default=40000); p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("status"); p.add_argument("workdir"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("sources"); p.add_argument("workdir"); p.set_defaults(fn=cmd_sources)
    p = sub.add_parser("set-kind"); p.add_argument("workdir"); p.add_argument("id"); p.add_argument("kind"); p.set_defaults(fn=cmd_set_kind)
    p = sub.add_parser("remove"); p.add_argument("workdir"); p.add_argument("id"); p.set_defaults(fn=cmd_remove)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

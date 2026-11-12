#!/usr/bin/env python3
"""distill · 机器核对：引文逐字回查、出处锚校验、章节覆盖对账、成稿结构检查。

只用标准库。它只能证明「引号里的字在原文里、锚指的地方存在、每个核心章节有去向」，
不能证明转述忠实、归纳合理——那两件事由独立核对者去做（references/04-verify.md）。

用法：
  verify.py quotes   <工作区> [文件...]   # 默认查 notes/*.md、digest.md、rulebook.md
  verify.py coverage <工作区>
  verify.py lint     <工作区>
  verify.py all      <工作区>

锚的写法：〔p45〕〔p45-47〕〔c3〕〔t12:34〕〔L120〕〔L120-150〕〔ch03〕〔W02〕〔W02:L15〕〔S2:p45〕
多个锚用逗号或顿号隔开：〔p45、p52〕。只有一份用户材料时，S1: 前缀可省。

引文结果：
  OK      逐字找到，位置对
  MOVED   逐字找到，但不在锚指的位置（附实际位置）——改锚
  NEAR    没逐字找到，有高度近似的原句（附原句）——照原句改，或去掉引号改成转述
  FAIL    原文里找不到——删掉，或去掉引号并核对这句话到底有没有依据
  BADSRC  出自「非逐字」来源（被概括过的网页）——不得作引语
  BADANCHOR 锚指向不存在的页、行或来源

退出码：0 全部通过；1 有必须处理的问题；2 用法或输入错误。
"""
import argparse
import difflib
import json
import os
import re
import sys
import unicodedata

VERSION = "0.3.1"
ANCHOR_RE = re.compile(r"〔(?!存疑)([^〔〕]{1,120})〕")     # 〔存疑：…〕是存疑标记，不是出处锚
TOKEN_RE = re.compile(
    r"^(?:(?P<src>[SW]\d+):)?(?:"
    r"p(?P<p1>\d+)(?:[-–](?P<p2>\d+))?|"
    r"c(?P<c>\d+)|"
    r"t(?P<t>\d+:\d{2}(?::\d{2})?)|"
    r"L(?P<l1>\d+)(?:[-–]L?(?P<l2>\d+))?|"
    r"(?P<ch>ch\d+[a-z]?)"
    r")$|^(?P<bare>[SW]\d+)$")
MARK_RE = re.compile(r"^⟦([pct])([\d:]+)⟧\s*$")
NONTEXT_RE = re.compile(r"^⟦[^⟧\n]*⟧\s*$")
ELLIPSIS_RE = re.compile(r"……|…|\.\.\.+|[\[［【(（]\s*(?:…|\.\.\.|略)\s*[\]］】)）]")
QUOTE_PAIRS = [("“", "”"), ("「", "」"), ("『", "』"), ("《《", "》》")]
EXEMPT_HEAD = re.compile(r"评注|蒸馏说明|拆解说明|关于这份|只记|带走|三句话|自测|回忆|延伸|怎么读|使用说明|目录")
NOTE_TAG = re.compile(r"^\s*\**\s*怎么读")      # 成品开头给读者的阅读说明，不是引文
COMMENT_TAG = re.compile(r"^\s*[\[【]\s*(评注|推断|点评|蒸馏者)")


def die(msg):
    print("错误：" + msg, file=sys.stderr)
    sys.exit(2)


def norm_char(ch):
    out = []
    for c in unicodedata.normalize("NFKC", ch).lower():
        cat = unicodedata.category(c)
        if cat[0] in ("L", "N"):
            out.append(c)
    return "".join(out)


def normalize(s):
    return "".join(norm_char(c) for c in s)


def cjk_count(s):
    return sum(1 for c in s if "一" <= c <= "鿿")


class Source:
    def __init__(self, wd, entry):
        self.id = entry["id"]
        self.entry = entry
        self.verbatim = entry.get("verbatim", True)
        self.fuzzy_ok = bool(entry.get("ocr")) or entry.get("format") == "subs"
        raw = open(os.path.join(wd, entry["text"]), encoding="utf-8").read()
        self.lines = raw.split("\n")
        self.norm = []
        self.pos = []            # norm 下标 → (行号 1 起)
        self.marks = []          # (行号, 类型, 值)
        body_start = 0
        if entry.get("format") == "web":
            for i, ln in enumerate(self.lines[:8]):
                if ln.strip() == "----":
                    body_start = i + 1
                    break
        for i, ln in enumerate(self.lines, 1):
            m = MARK_RE.match(ln)
            if m:
                self.marks.append((i, m.group(1), m.group(2)))
                continue
            if NONTEXT_RE.match(ln):          # ⟦页眉 …⟧ 之类，不算正文
                continue
            if i <= body_start:
                continue
            for c in ln:
                n = norm_char(c)
                for x in n:
                    self.norm.append(x)
                    self.pos.append(i)
        self.norm = "".join(self.norm)
        self.maxpage = max([int(v) for _, k, v in self.marks if k == "p"] or [0])
        self.maxc = max([int(v) for _, k, v in self.marks if k == "c"] or [0])

    def where(self, line):
        d = {"L": line}
        for ln, k, v in self.marks:
            if ln > line:
                break
            d[k] = v
        return d

    def mark_line(self, kind, value):
        """某页/某文件/某时间点开始的行号；找不到返回 None。"""
        if kind == "t":
            target = tsec(value)
            best = None
            for ln, k, v in self.marks:
                if k == "t" and tsec(v) <= target:
                    best = ln
            return best
        for ln, k, v in self.marks:
            if k == kind and int(v) == int(value):
                return ln
        return None

    def describe(self, line):
        d = self.where(line)
        if "p" in d:
            return f"p{d['p']}"
        if "t" in d:
            return f"t{d['t']}"
        if "c" in d:
            return f"c{d['c']}（L{line}）"
        return f"L{line}"

    def snippet(self, line, n=1):
        seg = [l for l in self.lines[max(0, line - 1):line + n] if not NONTEXT_RE.match(l)]
        return re.sub(r"\s+", " ", " ".join(seg)).strip()[:110]


def tsec(v):
    parts = [int(x) for x in v.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]


class Work:
    def __init__(self, wd):
        mp = os.path.join(wd, "manifest.json")
        if not os.path.isfile(mp):
            die(f"{wd} 不是工作区（没有 manifest.json）。")
        self.wd = wd
        self.manifest = json.load(open(mp, encoding="utf-8"))
        self.sources = {e["id"]: Source(wd, e) for e in self.manifest["sources"]}
        self.user_sources = [s for s in self.sources if s.startswith("S")]
        self.default = self.user_sources[0] if len(self.user_sources) == 1 else (
            list(self.sources)[0] if len(self.sources) == 1 else None)
        cp = os.path.join(wd, "chapters.json")
        self.chapters = json.load(open(cp, encoding="utf-8")) if os.path.isfile(cp) else []

    def parse_anchor(self, text):
        """返回 [(token, 解析结果或 None, 错误信息)]"""
        out = []
        for tok in re.split(r"[,，、;；\s]+", text.strip()):
            if not tok:
                continue
            m = TOKEN_RE.match(tok)
            if not m:
                out.append((tok, None, "写法不认识"))
                continue
            g = m.groupdict()
            if g["bare"]:
                sid = g["bare"]
                if sid not in self.sources:
                    out.append((tok, None, f"来源 {sid} 没登记"))
                else:
                    out.append((tok, {"src": sid, "kind": "doc"}, None))
                continue
            if g["ch"]:
                ch = next((c for c in self.chapters if c["id"] == g["ch"]), None)
                if not ch:
                    out.append((tok, None, f"chapters.json 里没有 {g['ch']}"))
                else:
                    out.append((tok, {"src": ch["source"], "kind": "L", "l1": ch["start_line"], "l2": ch["end_line"]}, None))
                continue
            sid = g["src"] or self.default
            if not sid:
                out.append((tok, None, "有多份材料，锚要写来源前缀，如 S2:p45"))
                continue
            if sid not in self.sources:
                out.append((tok, None, f"来源 {sid} 没登记"))
                continue
            s = self.sources[sid]
            if g["p1"]:
                p1, p2 = int(g["p1"]), int(g["p2"] or g["p1"])
                if not s.maxpage:
                    out.append((tok, None, f"{sid} 没有页码标记，不能用 p 锚"))
                elif p1 < 1 or p2 > s.maxpage or p2 < p1:
                    out.append((tok, None, f"{sid} 只有 {s.maxpage} 页"))
                else:
                    out.append((tok, {"src": sid, "kind": "p", "p1": p1, "p2": p2}, None))
            elif g["c"]:
                c = int(g["c"])
                if c < 1 or c > s.maxc:
                    out.append((tok, None, f"{sid} 只有 {s.maxc} 个正文文件" if s.maxc else f"{sid} 没有 c 标记"))
                else:
                    out.append((tok, {"src": sid, "kind": "c", "c": c}, None))
            elif g["t"]:
                if s.mark_line("t", g["t"]) is None:
                    out.append((tok, None, f"{sid} 里没有这个时间点之前的时间标记"))
                else:
                    out.append((tok, {"src": sid, "kind": "t", "t": g["t"]}, None))
            else:
                l1, l2 = int(g["l1"]), int(g["l2"] or g["l1"])
                if l1 < 1 or l2 > len(s.lines) or l2 < l1:
                    out.append((tok, None, f"{sid} 只有 {len(s.lines)} 行"))
                else:
                    out.append((tok, {"src": sid, "kind": "L", "l1": l1, "l2": l2}, None))
        return out

    def anchor_lines(self, a):
        """锚 → (来源, 起行, 止行)；整份文档返回 None。"""
        s = self.sources[a["src"]]
        k = a["kind"]
        if k == "doc":
            return None
        if k == "L":
            return (a["src"], a["l1"], a["l2"])
        if k == "p":
            st = s.mark_line("p", a["p1"])
            en = s.mark_line("p", a["p2"] + 1)
            return (a["src"], st, (en - 1) if en else len(s.lines))
        if k == "c":
            st = s.mark_line("c", a["c"])
            en = s.mark_line("c", a["c"] + 1)
            return (a["src"], st, (en - 1) if en else len(s.lines))
        if k == "t":
            st = s.mark_line("t", a["t"])
            return (a["src"], st, st + 3)
        return None

    def anchor_matches(self, a, sid, line):
        if a["src"] != sid:
            return False
        s = self.sources[sid]
        k = a["kind"]
        if k == "doc":
            return True
        d = s.where(line)
        if k == "p":
            return "p" in d and a["p1"] - 1 <= int(d["p"]) <= a["p2"] + 1
        if k == "c":
            return d.get("c") is not None and int(d["c"]) == a["c"]
        if k == "t":
            return "t" in d and abs(tsec(d["t"]) - tsec(a["t"])) <= 90
        if k == "L":
            return a["l1"] - 40 <= line <= a["l2"] + 40
        return False


# ---------- 从 markdown 里取引文 ----------

DOUBT_RE = re.compile(r"〔存疑[^〔〕]{0,80}〕")


def strip_anchors(s):
    return DOUBT_RE.sub("", ANCHOR_RE.sub("", s))


def iter_blocks(path):
    """产出 (kind, 起始行号, 文本, 所在标题链)。kind: head / quote / comment / text / gap"""
    lines = open(path, encoding="utf-8").read().split("\n")
    head, in_code, i = "", False, 0
    chain = {}
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            i += 1
            continue
        hm = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if hm:
            lvl = len(hm.group(1))
            chain = {k: v for k, v in chain.items() if k < lvl}
            chain[lvl] = hm.group(2).strip()
            head = " / ".join(chain[k] for k in sorted(chain) if k > 1)
            yield ("head", i + 1, head, head)
            i += 1
            continue
        if ln.lstrip().startswith(">"):
            j, buf = i, []
            while j < len(lines) and lines[j].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[j]))
                j += 1
            text = "\n".join(buf)
            yield ("note" if NOTE_TAG.match(text) else "comment" if COMMENT_TAG.match(text) else "quote", i + 1, text, head)
            i = j
            continue
        if ln.strip():
            yield ("text", i + 1, ln, head)
        else:
            yield ("gap", i + 1, "", head)
        i += 1


def inline_quotes(text):
    """产出 (引文, 紧跟的锚文本或 None)。短引号（术语）不算。"""
    for lq, rq in QUOTE_PAIRS:
        for m in re.finditer(re.escape(lq) + r"([^" + re.escape(lq) + re.escape(rq) + r"]{6,600})" + re.escape(rq), text):
            q = m.group(1)
            n = normalize(q)
            if cjk_count(q) >= 12 or (cjk_count(q) < 4 and len(n) >= 30):
                tail = text[m.end():m.end() + 40]
                am = ANCHOR_RE.match(tail.lstrip("。.，,；; ）)"))
                yield (q, am.group(1) if am else None)


def collect_quotes(path):
    out = []
    for kind, lineno, text, head in iter_blocks(path):
        if kind in ("head", "gap", "note"):
            continue
        if kind == "quote":
            # 一个引用块里可以连写几条引文：每条以锚收尾，或用空的引用行隔开
            parts, buf, start = [], [], lineno
            for off, qline in enumerate(text.split("\n")):
                if not qline.strip():
                    if buf:
                        parts.append((start, "\n".join(buf)))
                        buf = []
                    continue
                if not buf:
                    start = lineno + off
                buf.append(qline)
                if re.search(r"〔[^〔〕]+〕\s*$", DOUBT_RE.sub("", qline).rstrip()):
                    parts.append((start, "\n".join(buf)))
                    buf = []
            if buf:
                parts.append((start, "\n".join(buf)))
            for pl, ptxt in parts:
                anchors = ANCHOR_RE.findall(ptxt)
                q = strip_anchors(ptxt).strip()
                q = q.strip("“”「」『』\"' \n")
                q = re.sub(r"^\*+|\*+$", "", q).strip()
                if len(normalize(q)) >= 6:
                    out.append({"line": pl, "quote": q, "anchor": anchors[-1] if anchors else None, "ctx": "引文块"})
            continue
        for q, a in inline_quotes(text):
            if kind == "comment" and not a:
                continue        # 评注里没挂锚的引号，视为蒸馏者自己的话
            out.append({"line": lineno, "quote": q, "anchor": a, "ctx": "评注内" if kind == "comment" else "行内"})
    return out


# ---------- 引文回查 ----------

def find_all(hay, needle, limit=60):
    out, i = [], hay.find(needle)
    while i >= 0 and len(out) < limit:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def match_in(src, frags):
    """按顺序找各片段，返回所有可行的起点行号列表。"""
    hits = []
    for start in find_all(src.norm, frags[0]):
        pos, ok = start + len(frags[0]), True
        for f in frags[1:]:
            nxt = src.norm.find(f, pos, pos + 4000 + len(f))
            if nxt < 0:
                ok = False
                break
            pos = nxt + len(f)
        if ok:
            hits.append(src.pos[start])
    return hits


def near_in(src, q):
    if len(q) < 10 or not src.norm:
        return None
    k = 6 if len(q) >= 18 else 4
    cands = set()
    for off in range(0, len(q) - k + 1, max(1, (len(q) - k) // 8 or 1)):
        for p in find_all(src.norm, q[off:off + k], limit=12):
            cands.add(max(0, p - off) // 8 * 8)
    best = (0.0, None)
    for st in sorted(cands)[:60]:
        seg = src.norm[max(0, st - 8):st + len(q) + 8]
        r = difflib.SequenceMatcher(None, q, seg, autojunk=False)
        blocks = r.get_matching_blocks()
        matched = sum(b.size for b in blocks)
        ratio = matched / len(q)
        if ratio > best[0]:
            best = (ratio, src.pos[min(len(src.pos) - 1, max(0, st))])
    return best if best[0] >= 0.85 else None


def check_quote(w, item):
    frags = [normalize(f) for f in ELLIPSIS_RE.split(item["quote"])]
    frags = [f for f in frags if len(f) >= 4]
    res = {"status": "FAIL", "detail": "原文里找不到。如果这是你自己的话（自拟的问句、概括），去掉引号；如果想引原话，回原文照抄"}
    if not frags:
        res.update(status="SKIP", detail="引文太短，没法核")
        return res
    anchors, bad = [], []
    if item["anchor"]:
        for tok, a, err in w.parse_anchor(item["anchor"]):
            if a:
                anchors.append(a)
            else:
                bad.append(f"{tok}：{err}")
    if bad:
        res.update(status="BADANCHOR", detail="；".join(bad))
        return res
    order = list(dict.fromkeys([a["src"] for a in anchors] + list(w.sources)))
    found = []
    for sid in order:
        for line in match_in(w.sources[sid], frags):
            found.append((sid, line))
    if found:
        good = [(s, l) for s, l in found if any(w.anchor_matches(a, s, l) for a in anchors)] if anchors else found
        pick = (good or found)[0]
        src = w.sources[pick[0]]
        if not src.verbatim:
            res.update(status="BADSRC", detail=f"出自 {pick[0]}，该来源登记为「非逐字」")
        elif anchors and not good:
            res.update(status="MOVED", detail=f"实际在 {pick[0]}:{src.describe(pick[1])}")
        else:
            res.update(status="OK", detail=f"{pick[0]}:{src.describe(pick[1])}")
        return res
    q = "".join(frags) if len(frags) == 1 else max(frags, key=len)
    best = None
    for sid in order:
        r = near_in(w.sources[sid], q)
        if r and (not best or r[0] > best[1]):
            best = (sid, r[0], r[1])
    if best:
        src = w.sources[best[0]]
        how = "字都在原文里，但不连续（中间隔着别的字），不能当一句原话引" if best[1] >= 0.999 else f"相似 {best[1]:.0%}，不是一字不差"
        res.update(status="NEAR", soft=src.fuzzy_ok,
                   detail=f"{best[0]}:{src.describe(best[2])} {how}；原文：{src.snippet(best[2], 2)}")
    return res


def default_files(wd):
    files = []
    nd = os.path.join(wd, "notes")
    if os.path.isdir(nd):
        files += sorted(os.path.join(nd, f) for f in os.listdir(nd) if f.endswith(".md"))
    for name in ("digest.md", "rulebook.md", "cross.md"):
        p = os.path.join(wd, name)
        if os.path.isfile(p):
            files.append(p)
    return files


def run_quotes(w, files, rep):
    files = files or default_files(w.wd)
    if not files:
        die("没有可查的文件（notes/*.md、digest.md 都不存在）。")
    counts, problems, per_file = {}, [], {}
    for path in files:
        rel = os.path.relpath(path, w.wd)
        group = "底稿" if rel.startswith("notes") else rel
        for item in collect_quotes(path):
            r = check_quote(w, item)
            st = r["status"]
            counts[st] = counts.get(st, 0) + 1
            g = per_file.setdefault(group, {})
            g[st] = g.get(st, 0) + 1
            if st not in ("OK", "SKIP"):
                problems.append((rel, item, r))
        # 所有锚的有效性（不止引文后面的）
        for i, ln in enumerate(open(path, encoding="utf-8").read().split("\n"), 1):
            for am in ANCHOR_RE.finditer(ln):
                for tok, a, err in w.parse_anchor(am.group(1)):
                    if err:
                        counts["BADANCHOR"] = counts.get("BADANCHOR", 0) + 1
                        problems.append((rel, {"line": i, "quote": "", "anchor": am.group(1), "ctx": "锚"}, {"status": "BADANCHOR", "detail": f"{tok}：{err}"}))
    rep.append("## 引文与锚\n")
    rep.append(f"查了 {len(files)} 个文件。引文（引用块，以及引号里 12 个汉字以上的句子）逐字回查结果：\n")
    for g, c in per_file.items():
        n = sum(v for k, v in c.items() if k != "BADANCHOR")
        rep.append(f"- {g}：引文 {n} 条，" + "，".join(f"{k} {v}" for k, v in sorted(c.items())))
    if counts.get("BADANCHOR"):
        rep.append(f"- 写法不对或指向不存在位置的锚：{counts['BADANCHOR']} 处")
    rep.append("")
    w.stats = {"quotes": per_file, "bad_anchor": counts.get("BADANCHOR", 0)}
    hard = 0
    if problems:
        rep.append("| 文件:行 | 结果 | 引文（前 40 字） | 写的锚 | 说明 |")
        rep.append("|---|---|---|---|---|")
        seen = set()
        for rel, item, r in problems:
            key = (rel, item["line"], r["status"], r["detail"])
            if key in seen:
                continue
            seen.add(key)
            soft = r["status"] == "NEAR" and r.get("soft")
            if not soft:
                hard += 1
            q = item["quote"].replace("\n", " ").replace("|", "｜")[:40]
            tag = r["status"] + ("（OCR/字幕来源，人工看一眼即可）" if soft else "")
            rep.append(f"| {rel}:{item['line']} | {tag} | {q} | {item['anchor'] or '—'} | {r['detail'].replace('|', '｜')} |")
        rep.append("")
    return hard, counts


# ---------- 覆盖对账 ----------

def collect_anchor_lines(w, path):
    out = []
    if not os.path.isfile(path):
        return out
    for ln in open(path, encoding="utf-8").read().split("\n"):
        for am in ANCHOR_RE.finditer(ln):
            for tok, a, err in w.parse_anchor(am.group(1)):
                if a:
                    r = w.anchor_lines(a)
                    if r and r[1]:
                        out.append(r)
    return out


def run_coverage(w, rep):
    rep.append("## 章节覆盖\n")
    if not w.chapters:
        rep.append("没有 chapters.json，跳过。（单篇短内容可以没有；整本书必须有。）\n")
        return 0
    notes = []
    nd = os.path.join(w.wd, "notes")
    if os.path.isdir(nd):
        for f in sorted(os.listdir(nd)):
            if f.endswith(".md"):
                notes += collect_anchor_lines(w, os.path.join(nd, f))
    digest = collect_anchor_lines(w, os.path.join(w.wd, "digest.md"))
    has_digest = os.path.isfile(os.path.join(w.wd, "digest.md"))
    hard = 0
    rep.append("「底稿锚落点」是底稿里的出处落在这一章的哪一段（0% 是章首，100% 是章尾），用来发现只读了开头的情况；后半章确实没有值得记的内容时，落点偏前是正常的。\n")
    rep.append("| 章 | 标题 | 类别 | 字数 | 底稿锚 | 底稿锚落点 | 成稿锚 | 结果 |")
    rep.append("|---|---|---|---|---|---|---|---|")
    by_src = {}
    for ch in w.chapters:
        if ch.get("class") == "nomaterial":       # 目录里有、但这次没拿到任何材料的章节
            rep.append(f"| {ch.get('id', '')} | {str(ch.get('title', ''))[:24]} | 无材料 | — | — | — | — | 无材料（成品里只列标题） |")
            continue
        for key in ("id", "source", "start_line", "end_line", "class"):
            if key not in ch:
                die(f"chapters.json 的条目缺字段 {key}：{ch}")
        sid, s, e = ch["source"], ch["start_line"], ch["end_line"]
        if sid not in w.sources:
            die(f"chapters.json 里的来源 {sid} 没登记。")
        by_src.setdefault(sid, []).append((s, e))
        src = w.sources[sid]
        chars = len(normalize("\n".join(l for l in src.lines[s - 1:e] if not NONTEXT_RE.match(l))))

        def inside(lst):
            return [(a, b) for (x, a, b) in lst if x == sid and b >= s and a <= e]
        n_hits, d_hits = inside(notes), inside(digest)
        span = max(1, e - s)
        if n_hits:
            lo = max(0.0, (min(a for a, _ in n_hits) - s) / span)
            hi = min(1.0, (max(b for _, b in n_hits) - s) / span)
            rng = f"{lo:.0%}–{hi:.0%}"
        else:
            lo = hi = None
            rng = "—"
        cls, verdict = ch["class"], "通过"
        if cls == "skip":
            if not ch.get("reason"):
                verdict, hard = "✗ 跳过的章节要写 reason", hard + 1
            else:
                verdict = "跳过：" + ch["reason"]
        elif cls == "core":
            if not n_hits:
                verdict, hard = "✗ 核心章没有底稿", hard + 1
            elif chars > 6000 and (hi < 0.6 or lo > 0.4):
                verdict, hard = "✗ 底稿只覆盖了这一章的一部分，回去读完", hard + 1
            elif has_digest and not d_hits and not ch.get("omitted_reason"):
                verdict, hard = "✗ 核心章在成稿里没有出现；要么写进去，要么在 chapters.json 写 omitted_reason", hard + 1
            elif has_digest and not d_hits:
                verdict = "成稿略去：" + ch["omitted_reason"]
        else:
            if not n_hits:
                verdict = "提醒：支撑章没有底稿"
        rep.append(f"| {ch['id']} | {str(ch.get('title', ''))[:24]} | {cls} | {chars} | {len(n_hits)} | {rng} | {len(d_hits) if has_digest else '—'} | {verdict} |")
    rep.append("")
    for sid, ranges in by_src.items():
        ranges.sort()
        total, cur, gaps = len(w.sources[sid].lines), 1, []
        for s, e in ranges:
            if s - cur > 0:
                gaps.append((cur, s - 1))
            cur = max(cur, e + 1)
        if total - cur > 0:
            gaps.append((cur, total))
        big = []
        for a, b in gaps:
            n = len(normalize("\n".join(w.sources[sid].lines[a - 1:b])))
            if n > 800:
                big.append(f"L{a}–L{b}（{n} 字）")
        if big:
            hard += 1
            rep.append(f"✗ {sid} 有内容不属于任何章节：{'、'.join(big)}。补进 chapters.json（不要的标 skip 并写 reason）。\n")
    return hard


# ---------- 成稿结构 ----------

def run_lint(w, rep):
    rep.append("## 成稿结构\n")
    path = os.path.join(w.wd, "digest.md")
    if not os.path.isfile(path):
        rep.append("还没有 digest.md，跳过。\n")
        return 0
    raw = open(path, encoding="utf-8").read()
    hard = 0
    m = re.search(r"材料等级[:：]\s*\**\s*(T[123])", "\n".join(raw.split("\n")[:40]))
    tier = m.group(1) if m else None
    if not tier:
        hard += 1
        rep.append("✗ 开头 40 行内没有「材料等级：T1/T2/T3」。\n")
    else:
        rep.append(f"材料等级：{tier}\n")
    has_full = any(s.startswith("S") or w.sources[s].entry.get("kind") == "全文" for s in w.sources)
    if tier == "T1" and not has_full:
        hard += 1
        rep.append("✗ 标的是 T1，但工作区里没有用户提供的原文，也没有登记为「全文」的来源。\n")
    # T1 按「块」查（一个段落、一整组要点、一整张表算一块，块里有一个锚就行）；
    # T2/T3 没有原书兜底，按「条」查（每个段落、每条要点、表格每行都要有来源）。
    by_item = tier in ("T2", "T3")
    units, bare, induct_bare, n_anchor = 0, [], [], len(ANCHOR_RE.findall(raw))
    block = []          # [(行号, 文本)]

    def weight(t):
        body = strip_anchors(re.sub(r"(?m)^\s*([-*+]|\d+[.、)])\s+|^\|", "", t))
        return cjk_count(body) + len(re.findall(r"[A-Za-z]{2,}", body)) * 2, body

    lead = {"anchored": False}      # 上一块（引导句）有没有锚

    def close(reset=False):
        nonlocal units
        if not block:
            if reset:
                lead["anchored"] = False
            return
        kind_ = shape(block[0][1])
        items = list(block) if by_item else [(block[0][0], "\n".join(t for _, t in block))]
        any_anchor = False
        for ln, t in items:
            wt, body = weight(t)
            has = bool(ANCHOR_RE.search(t))
            any_anchor = any_anchor or has
            if wt < 25:
                continue
            units += 1
            if "【归纳】" in t and not has:
                induct_bare.append(ln)
            covered = has or (not by_item and kind_ in ("table", "list") and lead["anchored"])
            if not covered:
                bare.append((ln, body.replace("\n", " ")[:36]))
        lead["anchored"] = any_anchor if kind_ == "para" else False
        if reset:
            lead["anchored"] = False
        block.clear()

    def shape(t):
        t = t.lstrip()
        if t.startswith("|"):
            return "table"
        if re.match(r"^([-*+]|\d+[.、)])\s+", t):
            return "list"
        return "para"

    for kind, lineno, text, head in iter_blocks(path):
        if kind == "gap":
            close()
            continue
        if kind != "text" or EXEMPT_HEAD.search(head or ""):
            close(reset=True)
            continue
        t = text.strip()
        if re.match(r"^\|?\s*:?-{2,}", t) or t.startswith(("---", "材料等级", "<", "![")):
            continue
        if block and (shape(block[-1][1]) != shape(text) or shape(text) == "para") and not text.startswith((" ", "\t")):
            close()
        block.append((lineno, text))
    close()
    def plain(t):          # 和 prep.py 报的字数同一口径：去掉空白和标记后的字符数
        return len(re.sub(r"\s", "", t))
    body_chars = plain(strip_anchors(raw))
    read_chars = 0
    if w.chapters:
        for ch in w.chapters:
            if ch.get("class") not in ("skip", "nomaterial") and ch.get("source") in w.sources:
                src = w.sources[ch["source"]]
                read_chars += plain("\n".join(l for l in src.lines[ch["start_line"] - 1:ch["end_line"]] if not NONTEXT_RE.match(l)))
    else:
        read_chars = sum(plain("\n".join(l for l in x.lines if not NONTEXT_RE.match(l))) for k, x in w.sources.items() if k.startswith("S"))
    if read_chars:
        pct = body_chars / read_chars
        rep.append(f"篇幅：成稿约 {body_chars} 字（含标点，不含出处标签和空白），相当于所读材料（约 {read_chars} 字）的 {pct:.1%}。\n")
        rep.append("（这只是参考数。详略按 references/03-digest.md 第 5 节看内容定，不要为了这个数增删。）\n")
    ratio = len(bare) / units if units else 0
    what = "条（段落、要点、表格行各算一条）" if by_item else "块（一个段落、一组要点、一张表各算一块）"
    rep.append(f"正文共 {units} {what}，其中没有出处锚的 {len(bare)} 个（{ratio:.0%}）。全文出处锚 {n_anchor} 个。\n")
    if not by_item and units and n_anchor > units * 3.5:
        rep.append(f"提醒（不计入必须处理）：锚偏密，平均每块 {n_anchor / units:.1f} 个。同一段、同一组要点出自相邻几页时，在块末挂一个范围锚（如 〔p45-47〕）就够，不必句句挂。\n")
    if induct_bare:
        hard += 1
        rep.append(f"✗ 这些【归纳】没列出依据的锚：第 {'、'.join(map(str, induct_bare))} 行。\n")
    if bare:
        if tier in ("T2", "T3"):
            hard += 1
            rep.append("✗ T2/T3 没有原书在手，正文每一条都必须挂来源锚；没有来源的内容删掉或移进评注。无锚的行：\n")
        else:
            rep.append("提醒（不计入必须处理，但要逐条看过）：下面这些块没有出处锚。纯过渡或导语可以不管；有实质内容的要补锚，补不出就是没依据，删掉或挪进评注：\n")
        for ln, t in bare[:40]:
            rep.append(f"- 第 {ln} 行起：{t}……")
        if len(bare) > 40:
            rep.append(f"- ……另有 {len(bare) - 40} 行")
        rep.append("")
    if tier in ("T2", "T3"):
        used = set()
        for am in ANCHOR_RE.finditer(raw):
            for tok, a, err in w.parse_anchor(am.group(1)):
                if a:
                    used.add(a["src"])
        weak = [s for s in used if w.sources[s].entry.get("kind") in ("他人摘要", "百科", "短评", "简介")]
        strong = [s for s in used if s not in weak]
        rep.append(f"成稿引用了 {len(used)} 份来源，其中二手摘要类 {len(weak)} 份、其余 {len(strong)} 份。\n")
        if tier == "T2" and not strong:
            hard += 1
            rep.append("✗ 标的是 T2，但成稿只引用了简介、短评、百科或他人摘要。按规则这是 T3。\n")
    return hard


def main():
    ap = argparse.ArgumentParser(description="distill 机器核对")
    ap.add_argument("cmd", choices=["quotes", "coverage", "lint", "all"])
    ap.add_argument("workdir")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()
    w = Work(a.workdir)
    rep, hard = [f"# 机器核对报告\n\n（distill 脚本版本 {VERSION}）\n"], 0
    if a.cmd in ("quotes", "all"):
        h, _ = run_quotes(w, a.files, rep)
        hard += h
    if a.cmd in ("coverage", "all"):
        hard += run_coverage(w, rep)
    if a.cmd in ("lint", "all"):
        hard += run_lint(w, rep)
    rep.append(f"**结论：{'必须处理的问题 ' + str(hard) + ' 项' if hard else '机器能查的全部通过'}。**")
    rep.append("\n机器查不了的：转述是否忠实、归纳是否成立、取舍是否得当。这些要走独立核对。")
    if a.cmd == "all" and not w.chapters:
        rep.append("\n（没有 chapters.json，章节覆盖这一项没有查。）")
    text = "\n".join(rep) + "\n"
    os.makedirs(os.path.join(a.workdir, "verify"), exist_ok=True)
    import datetime
    with open(os.path.join(a.workdir, "verify", "history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.datetime.now().isoformat(timespec="seconds"), "cmd": a.cmd,
                            "must_fix": hard, "stats": getattr(w, "stats", None)}, ensure_ascii=False) + "\n")
    with open(os.path.join(a.workdir, "verify", f"report-{a.cmd}.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()

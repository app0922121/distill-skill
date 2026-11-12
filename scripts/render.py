#!/usr/bin/env python3
"""distill · 把 digest.md 渲染成一个可以直接双击打开的单文件 HTML。

只用标准库。支持的 Markdown：标题、段落、有序/无序列表（两级）、表格、引用块、分隔线、
粗体、斜体、行内代码、链接。出处锚〔…〕变成小标签，网页来源的锚可点开原链接；
【归纳】变成徽标；以 [评注] 开头的引用块单独上色。页尾自动附「材料清单」。

用法：
  render.py <工作区> [--in digest.md] [--out digest.html]
"""
import argparse
import html
import json
import os
import re
import sys

ANCHOR_RE = re.compile(r"〔(?!存疑)([^〔〕]{1,120})〕")
COMMENT_TAG = re.compile(r"^\s*[\[【]\s*(评注|推断|点评|蒸馏者)[^\]】]*[\]】]\s*")

CSS = """
:root{--bg:#fbfaf7;--fg:#22211e;--mute:#6b675f;--line:#e4dfd3;--card:#ffffff;--acc:#8a4b1f;
--anc-bg:#efe9da;--anc-fg:#5d5443;--ind-bg:#e3ecf5;--ind-fg:#27507a;--cm-bg:#f6efe2;--cm-line:#c9a45c;--q-line:#9a9486}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#191816;--fg:#e9e5dc;--mute:#a09a8d;--line:#35322c;--card:#211f1c;--acc:#e0a070;
--anc-bg:#2e2b25;--anc-fg:#c2b9a5;--ind-bg:#1f2c3a;--ind-fg:#9cc0e6;--cm-bg:#2a2519;--cm-line:#a8853f;--q-line:#6d685d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.8 -apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif}
main{max-width:780px;margin:0 auto;padding:40px 16px 96px}
h1{font-size:30px;line-height:1.3;margin:0 0 12px}
h2{font-size:23px;margin:56px 0 12px;padding-top:20px;border-top:1px solid var(--line)}
h3{font-size:19px;margin:32px 0 8px}
h4{font-size:17px;margin:24px 0 6px}
p{margin:12px 0}
a{color:var(--acc)}
ul,ol{padding-left:24px;margin:12px 0}
li{margin:6px 0}
hr{border:0;border-top:1px solid var(--line);margin:32px 0}
code{background:var(--anc-bg);padding:1px 5px;border-radius:4px;font-size:.9em}
.tablewrap{overflow-x:auto;margin:16px 0}
table{border-collapse:collapse;width:100%;font-size:15px;line-height:1.6}
th,td{border:1px solid var(--line);padding:8px 10px;vertical-align:top;text-align:left}
th{background:var(--card)}
blockquote{margin:16px 0;padding:8px 16px;border-left:3px solid var(--q-line);color:var(--fg)}
blockquote.cm{background:var(--cm-bg);border-left-color:var(--cm-line);border-radius:0 6px 6px 0}
blockquote.cm .tag{font-size:13px;font-weight:600;color:var(--acc);margin-right:6px}
.anc{display:inline-block;font:12px/1.5 ui-monospace,Menlo,monospace;background:var(--anc-bg);color:var(--anc-fg);
padding:0 6px;border-radius:9px;margin:0 2px;white-space:nowrap;vertical-align:1px;text-decoration:none}
a.anc:hover{text-decoration:underline}
.doubt{font-size:12px;color:var(--acc);border:1px dashed var(--cm-line);border-radius:9px;padding:0 6px;margin:0 2px;white-space:nowrap}
.ind{display:inline-block;font-size:12px;font-weight:600;background:var(--ind-bg);color:var(--ind-fg);padding:0 7px;border-radius:9px;margin-right:4px;vertical-align:1px}
.legend{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;font-size:14px;line-height:1.7;color:var(--mute);margin:20px 0 8px}
.legend b{color:var(--fg)}
nav{font-size:15px;margin:16px 0 0}
nav a{display:block;padding:2px 0;text-decoration:none}
nav a.l3{padding-left:16px;color:var(--mute)}
footer{margin-top:64px;font-size:14px;color:var(--mute)}
@media print{body{background:#fff}nav{display:none}}
"""

KIND_TEXT = {"p": "p 加数字是所给文件的第几页", "L": "L 加数字是材料文本的第几行", "t": "t 加时间是音视频的时间点",
             "c": "c 加数字是电子书的第几个正文文件", "W": "W 开头的是网页来源，可以点开", "ch": "ch 加数字是第几章"}


def legend(md):
    toks = []
    for m in ANCHOR_RE.finditer(md):
        toks += [t for t in re.split(r"[,，、;；\s]+", m.group(1).strip()) if t]
    kinds, sample = [], None
    for t in toks:
        core = t.split(":", 1)[-1] if re.match(r"^S\d+:", t) else t
        k = "W" if core.startswith("W") else ("ch" if core.startswith("ch") else core[:1])
        if k in KIND_TEXT and k not in kinds:
            kinds.append(k)
            sample = sample or t
    sample = sample or "p45"
    expl = "；".join(KIND_TEXT[k] for k in kinds) or KIND_TEXT["p"]
    return ('<div class="legend"><b>怎么读这份东西：</b>正文是原书（原材料）说的，句后的小标签 '
            f'<span class="anc">{html.escape(sample)}</span> 是出处（{expl}），可以回原文核对；带 <span class="ind">归纳</span> 的句子是整理者把几处原文合起来的概括，'
            '原书没有这句原话；浅色底的「评注」块是整理者自己的判断，不是作者的意思。</div>')


def inline(s, web):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<em>\1</em>", s)
    s = s.replace("【归纳】", '<span class="ind">归纳</span>')

    def anc(m):
        out = []
        for tok in re.split(r"[,，、;；\s]+", m.group(1).strip()):
            if not tok:
                continue
            sid = re.match(r"^(W\d+)", tok)
            if sid and sid.group(1) in web:
                out.append(f'<a class="anc" href="{html.escape(web[sid.group(1)], quote=True)}" target="_blank" rel="noopener">{tok}</a>')
            else:
                out.append(f'<span class="anc">{tok}</span>')
        return "".join(out)
    s = re.sub(r"〔(存疑[^〔〕]{0,80})〕", r'<span class="doubt">\1</span>', s)
    return ANCHOR_RE.sub(anc, s)


def slug(n):
    return f"s{n}"


def convert(md, web):
    lines = md.split("\n")
    out, toc, i, hn, title = [], [], 0, 0, ""
    para = []

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para), web) + "</p>")
            para.clear()

    while i < len(lines):
        ln = lines[i]
        st = ln.strip()
        if st.startswith("```"):
            flush()
            j, buf = i + 1, []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            flush()
            lvl, txt = len(m.group(1)), m.group(2).strip()
            if lvl == 1 and not title:
                title = ANCHOR_RE.sub("", txt)
                out.append(f"<h1>{inline(txt, web)}</h1>")
                out.append("<!--LEGEND-->")
            else:
                hn += 1
                if lvl in (2, 3):
                    toc.append((lvl, slug(hn), ANCHOR_RE.sub("", txt)))
                out.append(f'<h{lvl} id="{slug(hn)}">{inline(txt, web)}</h{lvl}>')
            i += 1
            continue
        if re.match(r"^\s*([-*_])\s*\1\s*\1[\s\-*_]*$", ln) and st:
            flush()
            out.append("<hr>")
            i += 1
            continue
        if st.startswith(">"):
            flush()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            text = "\n".join(buf)
            cm = COMMENT_TAG.match(text)
            if re.match(r"^\s*\**\s*怎么读", text):
                out.append('<div class="legend">' + inline(text.replace("\n", " "), web) + "</div>")
                continue
            if cm:
                body = text[cm.end():]
                inner = "".join(f"<p>{inline(p.replace(chr(10), ' '), web)}</p>" for p in re.split(r"\n\s*\n", body) if p.strip())
                inner = inner.replace("<p>", f'<p><span class="tag">{html.escape(cm.group(1))}</span>', 1)
                out.append(f'<blockquote class="cm">{inner}</blockquote>')
            else:
                inner = "".join(f"<p>{inline(p.replace(chr(10), ' '), web)}</p>" for p in re.split(r"\n\s*\n", text) if p.strip())
                out.append(f"<blockquote>{inner}</blockquote>")
            continue
        if st.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], rows[2:]
            t = ['<div class="tablewrap"><table><thead><tr>' + "".join(f"<th>{inline(c, web)}</th>" for c in head) + "</tr></thead><tbody>"]
            for r in body:
                t.append("<tr>" + "".join(f"<td>{inline(c, web)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
            continue
        lm = re.match(r"^(\s*)([-*+]|\d+[.)、])\s+(.*)$", ln)
        if lm:
            flush()
            stack = []          # [(缩进, 标签)]
            while i < len(lines):
                lm = re.match(r"^(\s*)([-*+]|\d+[.)、])\s+(.*)$", lines[i])
                if not lm:
                    if lines[i].strip() and lines[i].startswith(" ") and stack:
                        out[-1] = out[-1] + " " + inline(lines[i].strip(), web)
                        i += 1
                        continue
                    break
                ind = len(lm.group(1).replace("\t", "    "))
                tag = "ul" if lm.group(2) in "-*+" else "ol"
                while stack and ind < stack[-1][0]:
                    out.append(f"</li></{stack.pop()[1]}>")
                if not stack or ind > stack[-1][0]:
                    out.append(f"<{tag}>")
                    stack.append((ind, tag))
                else:
                    out.append("</li>")
                out.append("<li>" + inline(lm.group(3), web))
                i += 1
            while stack:
                out.append(f"</li></{stack.pop()[1]}>")
            continue
        if not st:
            flush()
        else:
            para.append(st)
        i += 1
    flush()
    return "\n".join(out), toc, title


def sources_table(manifest):
    rows = []
    for s in manifest.get("sources", []):
        name = html.escape(s.get("title", ""))
        if s.get("url"):
            name = f'<a href="{html.escape(s["url"], quote=True)}" target="_blank" rel="noopener">{name}</a>'
        note = []
        if s.get("pages"):
            note.append(f"{s['pages']} 页")
        if s.get("ocr"):
            note.append("OCR 文本")
        if s.get("verbatim") is False:
            note.append("经过概括，未作引语")
        rows.append(f"<tr><td><span class='anc'>{s['id']}</span></td><td>{name}</td><td>{html.escape(s.get('kind', ''))}</td>"
                    f"<td>{s.get('chars', '')}</td><td>{'；'.join(note)}</td></tr>")
    if not rows:
        return ""
    return ('<h2 id="sources">材料清单</h2><div class="tablewrap"><table><thead><tr><th>编号</th><th>材料</th><th>类型</th><th>字数</th><th>备注</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div>")


def verify_note(wd):
    """页脚如实写三道核对各自的状态。"""
    def newer_than_digest(path):
        d = os.path.join(wd, "digest.md")
        return os.path.isfile(path) and (not os.path.isfile(d) or os.path.getmtime(path) >= os.path.getmtime(d) - 1)
    rp = os.path.join(wd, "verify", "report-all.md")
    if not os.path.isfile(rp):
        machine = "机器核对：未运行。"
    elif not newer_than_digest(rp):
        machine = "机器核对：成稿在上次核对之后改过，结果已过期。"
    else:
        txt = open(rp, encoding="utf-8").read()
        if "机器能查的全部通过" not in txt:
            machine = "机器核对：仍有未处理的问题，见 verify/report-all.md。"
        else:
            cov = "、章节覆盖" if os.path.isfile(os.path.join(wd, "chapters.json")) else ""
            machine = f"机器核对：引号里的原话、出处标签{cov}已通过脚本检查。"
    def human(name):
        fp = os.path.join(wd, "verify", name)
        if not os.path.isfile(fp):
            return "未做"
        return "已做" if newer_than_digest(fp) else "已做，之后成稿又改过"
    ind, rd = human("independent.md"), human("reader.md")
    return f"{machine}独立核对（另一人逐条回原文查转述）：{ind}。只读成品的读者试读：{rd}。"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--in", dest="src", default="digest.md")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    src = os.path.join(a.workdir, a.src)
    if not os.path.isfile(src):
        print(f"错误：找不到 {src}", file=sys.stderr)
        sys.exit(2)
    mp = os.path.join(a.workdir, "manifest.json")
    manifest = json.load(open(mp, encoding="utf-8")) if os.path.isfile(mp) else {"sources": []}
    web = {s["id"]: s["url"] for s in manifest.get("sources", []) if s.get("url")}
    md_text = open(src, encoding="utf-8").read()
    body, toc, title = convert(md_text, web)
    nav = "<nav>" + "".join(f'<a class="l{l}" href="#{sid}">{html.escape(t)}</a>' for l, sid, t in toc if l == 2) + "</nav>"
    body = re.sub(r"<!--LEGEND-->\n(<p>材料等级.*?</p>)", r"\1\n<!--LEGEND-->", body, count=1, flags=re.S)   # 等级行放在说明之前
    own = 'class="legend"' in body          # 成稿自己写了「怎么读」，就不再加自动图例
    body = body.replace("<!--LEGEND-->", ("" if own else legend(md_text)) + nav, 1)
    page = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title or '蒸馏')}</title><style>{CSS}</style></head><body><main>{body}{'' if '材料清单</h2>' in body else sources_table(manifest)}"
            f"<footer>本页由 distill 生成。出处标签对应所给材料的页序、行号或时间点。{verify_note(a.workdir)}</footer></main></body></html>")
    out = os.path.join(a.workdir, a.out or (os.path.splitext(a.src)[0] + ".html"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"已生成：{os.path.abspath(out)}（{len(page)} 字节）")


if __name__ == "__main__":
    main()

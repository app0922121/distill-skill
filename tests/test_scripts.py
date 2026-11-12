#!/usr/bin/env python3
"""distill 脚本回归测试。用法：python3 tests/test_scripts.py
造一本假书，埋进已知的错误，确认 verify.py 逐项抓到；再确认改对之后全部通过、render.py 出页面。
"""
import json
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "scripts")
fails = []


def run(script, *args):
    r = subprocess.run([sys.executable, os.path.join(S, script), *args], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def check(name, cond, detail=""):
    print(("通过  " if cond else "失败  ") + name + ("" if cond else "  ← " + detail[:300]))
    if not cond:
        fails.append(name)


def make_book(path):
    p = ["# 第一章 买方为什么不买",
         "买方不采取行动，往往不是因为不认可方案，而是因为没有意识到痛苦的代价。销售人员最常见的错误，是在买方承认痛苦之前就开始介绍产品。"]
    p += [f"第一章填充段落{i}，这里讲的是一些铺垫内容，用来把章节撑到足够的长度。" * 6 for i in range(60)]
    p += ["痛苦要沿着组织向上追溯，直到找到有权拨预算的人为止。这条链叫作痛苦链。",
          "# 第二章 诊断先于处方",
          "没有诊断就开处方，在医学上叫渎职，在销售上叫推销。提问的顺序是：先开放式，再控制式，最后确认式。"]
    p += [f"第二章填充段落{i}，继续铺垫，内容本身并不重要。" * 8 for i in range(60)]
    p += ["当买方能用自己的话说出解决之后的样子，构想才算建立。", "# 致谢", "感谢我的编辑和家人。" * 30]
    open(path, "w", encoding="utf-8").write("\n\n".join(p) + "\n")


def main():
    t = tempfile.mkdtemp(prefix="distill-test-")
    w = os.path.join(t, "w")
    book = os.path.join(t, "book.txt")
    make_book(book)
    code, out = run("prep.py", "init", w)
    check("init 建工作区", code == 0 and os.path.isfile(os.path.join(w, "manifest.json")), out)
    code, out = run("prep.py", "add", w, book, "--title", "测试书")
    check("add 登记 txt 并找到标题行", code == 0 and "疑似标题行：3 处" in out, out)
    code, out = run("prep.py", "add", w, os.path.join(t, "nope.pdf"))
    check("add 找不到文件时报错退出", code == 2, out)

    # epub
    ep = os.path.join(t, "t.epub")
    z = zipfile.ZipFile(ep, "w")
    z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="O/c.opf"/></rootfiles></container>')
    z.writestr("O/c.opf", '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>电子书</dc:title></metadata><manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
    z.writestr("O/a.xhtml", "<html><body><h1>第一章 起点</h1><p>第一章的内容在这里，足够长。</p></body></html>")
    z.writestr("O/b.xhtml", "<html><body><h1>第二章 转折</h1><p>第二章说了另一件事。</p></body></html>")
    z.close()
    code, out = run("prep.py", "add", w, ep)
    txt = open(os.path.join(w, "text", "S2.txt"), encoding="utf-8").read()
    check("add 解析 epub 并打 c 标记", code == 0 and "⟦c1⟧" in txt and "⟦c2⟧" in txt and "第二章说了另一件事" in txt, out)

    # 字幕
    vtt = os.path.join(t, "a.vtt")
    open(vtt, "w").write("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nhello there\n\n00:00:40.000 --> 00:00:44.000\nsecond part here\n")
    code, out = run("prep.py", "add", w, vtt)
    txt = open(os.path.join(w, "text", "S3.txt"), encoding="utf-8").read()
    check("add 解析字幕并打时间标记", code == 0 and "⟦t0:01⟧" in txt and "⟦t0:40⟧" in txt, out + txt)

    # 非逐字网页来源
    page = os.path.join(t, "page.txt")
    open(page, "w").write("这篇书评概括说，作者主张先诊断再给方案，并且反对一上来就介绍产品的做法。" * 8)
    code, out = run("prep.py", "add-url", w, "https://example.com/review", "--kind", "长书评", "--from-file", page, "--not-verbatim")
    check("add-url 登记非逐字来源", code == 0 and "非逐字" in out, out)
    code, out = run("prep.py", "add-url", w, "https://example.com/x", "--kind", "瞎写", "--from-file", page)
    check("add-url 拒绝不认识的类型", code == 2, out)

    json.dump([
        {"id": "ch01", "title": "买方为什么不买", "source": "S1", "start_line": 1, "end_line": 126, "class": "core"},
        {"id": "ch02", "title": "诊断先于处方", "source": "S1", "start_line": 127, "end_line": 252, "class": "core"},
        {"id": "ch03", "title": "致谢", "source": "S1", "start_line": 253, "end_line": 256, "class": "skip"},
    ], open(os.path.join(w, "chapters.json"), "w"), ensure_ascii=False)
    code, out = run("prep.py", "plan", w, "--max-chars", "6000")
    check("plan 把大章切成多批", code == 0 and "B01" in out and "B03" in out, out)

    bad = """### N-ch01-01
> 买方不采取行动，往往不是因为不认可方案，而是因为没有意识到痛苦的代价。 〔S1:L3〕
### N-ch01-02 锚错
> 痛苦要沿着组织向上追溯，直到找到有权拨预算的人为止。 〔S1:L3〕
### N-ch01-03 近似
> 销售人员最常见的错误，是在买家承认痛苦以前就开始介绍产品。 〔S1:L3〕
### N-ch01-04 编造
> 百分之七十的买方在第一次会面后就做出了决定。 〔S1:L5〕
### N-ch01-05 省略号
> 买方不采取行动……没有意识到痛苦的代价 〔S1:L3〕
### N-ch01-06 坏锚
述：某事 〔S1:p12〕 另一事 〔S1:L9999〕 没前缀 〔L3〕
### N-ch01-07 非逐字来源
> 作者主张先诊断再给方案，并且反对一上来就介绍产品的做法 〔W01〕
### N-ch01-08 存疑标记不算锚
述：某个数字 〔S1:L3〕 〔存疑：OCR 错字〕
"""
    open(os.path.join(w, "notes", "b1.md"), "w", encoding="utf-8").write(bad)
    code, out = run("verify.py", "all", w)
    check("verify 有问题时退出码为 1", code == 1, out)
    check("verify 抓到 MOVED 并给出实际位置", "MOVED" in out and "实际在 S1:L125" in out, out)
    check("verify 抓到 NEAR 并贴出原文", "NEAR" in out and "买方承认痛苦之前" in out, out)
    check("verify 抓到 FAIL（编造的引文）", "| FAIL | 百分之七十" in out, out)
    check("verify 抓到越界的行号", "只有" in out and "L9999" in out, out)
    check("verify 抓到不存在的页码锚", "没有页码标记" in out, out)
    check("verify 多材料时要求来源前缀", "锚要写来源前缀" in out, out)
    check("verify 抓到非逐字来源作引语（BADSRC）", "BADSRC" in out, out)
    check("verify 不把存疑标记当成坏锚", "存疑" not in out.split("## 章节覆盖")[0], out)
    check("verify 报核心章没有底稿", "核心章没有底稿" in out, out)
    check("verify 报底稿只覆盖一部分", "只覆盖了这一章的一部分" in out, out)
    check("verify 报跳过章节缺原因", "要写 reason" in out, out)

    good = """### N-ch01-01
> 买方不采取行动，往往不是因为不认可方案，而是因为没有意识到痛苦的代价。 〔S1:L3〕
### N-ch01-02
> 痛苦要沿着组织向上追溯，直到找到有权拨预算的人为止。 〔S1:L125〕
### N-ch02-01
> 没有诊断就开处方，在医学上叫渎职，在销售上叫推销。 〔S1:L129〕
### N-ch02-02
> 当买方能用自己的话说出解决之后的样子，构想才算建立。 〔S1:L251〕 〔存疑：OCR〕
"""
    open(os.path.join(w, "notes", "b1.md"), "w", encoding="utf-8").write(good)
    ch = json.load(open(os.path.join(w, "chapters.json")))
    ch[2]["reason"] = "致谢，无正文"
    ch += [{"id": "x1", "title": "电子书样例", "source": "S2", "start_line": 1, "end_line": 9, "class": "skip", "reason": "测试用"}]
    json.dump(ch, open(os.path.join(w, "chapters.json"), "w"), ensure_ascii=False)
    digest = """# 《测试书》精华解析

材料等级：T1 ｜ 测试版 ｜ 读了全文

## 一屏速览
- 一句话：买方不买，多半是没感到代价。

## 买方为什么不买
买方不行动，原因通常不是不认可方案，而是没意识到问题的代价〔S1:L3〕。作者说“没有诊断就开处方，在医学上叫渎职，在销售上叫推销。”〔S1:L129〕

【归纳】做法可以归成两步：先让买方承认痛苦，再帮他建立构想，两步缺一不可〔S1:L3、S1:L251〕。

> [评注] 这套说法偏大客户销售，小单未必适用。

## 蒸馏说明
读了全文。
"""
    open(os.path.join(w, "digest.md"), "w", encoding="utf-8").write(digest)
    code, out = run("verify.py", "all", w)
    check("改对之后 verify 全部通过", code == 0 and "机器能查的全部通过" in out, out)

    open(os.path.join(w, "digest.md"), "w", encoding="utf-8").write(digest.replace("材料等级：T1", "材料等级：T2").replace(
        "## 蒸馏说明", "这一整段话没有任何来源，是凭印象写出来的内容，长度足够被检查到。\n\n## 蒸馏说明"))
    code, out = run("verify.py", "lint", w)
    check("T2 成稿里无锚的正文被判为必须处理", code == 1 and "每一条都必须挂来源锚" in out, out)
    open(os.path.join(w, "digest.md"), "w", encoding="utf-8").write(digest.replace("材料等级：T1 ｜", "｜"))
    code, out = run("verify.py", "lint", w)
    check("缺材料等级行被判为必须处理", code == 1 and "没有「材料等级" in out, out)

    open(os.path.join(w, "digest.md"), "w", encoding="utf-8").write(digest)
    run("verify.py", "all", w)
    code, out = run("render.py", w)
    page = open(os.path.join(w, "digest.html"), encoding="utf-8").read() if code == 0 else ""
    check("render 生成网页，含出处标签、归纳徽标、评注块、材料清单",
          code == 0 and 'class="anc"' in page and 'class="ind"' in page and 'class="cm"' in page and "材料清单" in page, out)
    check("render 页脚写机器核对已通过，并如实写独立核对未做", "已通过脚本检查" in page and "独立核对（另一人逐条回原文查转述）：未做" in page, page[-400:])
    check("render 图例跟随实际出处类型（行号）", "L 加数字是材料文本的第几行" in page, page[:1500])

    # 纯文本里的分页行、页眉、OCR 标记、跨页引文
    pg = os.path.join(t, "paged.txt")
    parts = []
    for n in range(1, 8):
        parts.append(f"=== 第 {n} 页 ===")
        parts.append("第一章 示例章")
        parts.append(f"这是第{n}页的正文内容，讲的是一件具体的事情，写得足够长。" * 4)
    parts[5] = parts[5] + "这句话从第二页开始一直"
    parts[8] = "延续到第三页才算结束。" + parts[8]
    open(pg, "w", encoding="utf-8").write("\n".join(parts) + "\n")
    w2 = os.path.join(t, "w2")
    run("prep.py", "init", w2)
    code, out = run("prep.py", "add", w2, pg)
    check("add 发现纯文本里的分页行并提示", code == 0 and "像分页行" in out, out)
    code, out = run("prep.py", "add", w2, pg, "--id", "S1", "--ocr", "--page-marker", r"^=== 第 (\d+) 页 ===$")
    txt = open(os.path.join(w2, "text", "S1.txt"), encoding="utf-8").read()
    check("add --page-marker 把分页行转成页码标记", code == 0 and "⟦p3⟧" in txt and "锚写 p45" in out, out)
    check("add 标出重复页眉", "⟦页眉 第一章 示例章⟧" in txt, txt[:300])
    check("add --ocr 记为 OCR 文本", "已记为 OCR" in out, out)
    open(os.path.join(w2, "notes", "a.md"), "w", encoding="utf-8").write("> 这句话从第二页开始一直延续到第三页才算结束。 〔p2〕\n")
    code, out = run("verify.py", "quotes", w2)
    check("verify 跨页引文跳过页眉后能对上", code == 0 and "OK 1" in out, out)
    open(os.path.join(w2, "notes", "a.md"), "w", encoding="utf-8").write("述：他问“你们到底有没有给客户提供过真正的解决方案呢”\n")
    code, out = run("verify.py", "quotes", w2)
    check("verify 对找不到的引文提示可能是误加引号", code == 1 and "去掉引号" in out, out)
    open(os.path.join(w2, "notes", "a.md"), "w", encoding="utf-8").write(
        "> 这是第1页的正文内容，讲的是一件具体的事情，写得足够长。 〔p1〕\n> 这是第5页的正文内容，讲的是一件具体的事情，写得足够长。 〔p5〕\n")
    code, out = run("verify.py", "quotes", w2)
    check("verify 连写的两行引文各算一条", code == 0 and "引文 2 条" in out, out)
    check("verify 留下核对历史", os.path.isfile(os.path.join(w2, "verify", "history.jsonl")))

    print(f"\n共 {len(fails)} 项失败。" if fails else "\n全部通过。")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

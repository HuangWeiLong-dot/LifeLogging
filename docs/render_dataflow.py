#!/usr/bin/env python3
"""把 `docs/lls-dataflow.mmd` 渲染成 SVG + PNG。

**为什么不用 mmdc / 技能的 render_mermaid.py**

    两个都试过，这条路在当前环境里走不通：

      · `npm i -g @mermaid-js/mermaid-cli` 装不下来（puppeteer 那一步超时）
      · 技能里的 `scripts/render_mermaid.py` 同样依赖 mmdc

    所以改成：直接把 mermaid 的浏览器 bundle 喂给本机 Chrome。没有 npm 依赖，
    Chrome 本来就在（tools/ 下的验收脚本也一直用它）。

**mermaid v11 的两个坑（都踩过了）**

    1. **`%%` 注释块不能出现在 `flowchart` 之前。** 报 "Parse error on line 1"，
       而且错误里会出现 `%%%%flowchart` —— 看起来像你的文件坏了，其实是它
       自己把注释解析串了。所以 .mmd 的第一行必须是 `flowchart`。
    2. **单独一行 `%%` 会被当成一个节点画出来。** 图上会凭空多一个写着 `%%`
       的方框。注释行必须有正文。

**依赖**

    首次运行会把 mermaid 的 bundle 下到 `docs/_mermaid.min.js`（约 3.5 MB，
    gitignored）。之后离线可用。

用法：
  python docs/render_dataflow.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MMD = HERE / "lls-dataflow.mmd"
BUNDLE = HERE / "_mermaid.min.js"
RENDER = HERE / "_render.html"
SVG_OUT = HERE / "lls-dataflow.svg"
PNG_OUT = HERE / "lls-dataflow.png"

BUNDLE_URL = "https://unpkg.com/mermaid@11/dist/mermaid.min.js"
CHROME_CANDIDATES = (
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>body{{margin:0;padding:30px;background:#fbfbfa;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
#out svg{{max-width:100%;height:auto}}</style></head><body>
<div id="out">pending</div>
<script type="text/plain" id="src">{src}</script>
<script>{js}</script>
<script>
mermaid.initialize({{startOnLoad:false,theme:'base',
  flowchart:{{curve:'basis',nodeSpacing:40,rankSpacing:70,padding:14}},
  themeVariables:{{fontFamily:'-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
  fontSize:'14px',lineColor:'#8b949e',primaryColor:'#ffffff',
  edgeLabelBackground:'#fbfbfa'}}}});
mermaid.render('g1', document.getElementById('src').textContent)
  .then(function(r){{document.getElementById('out').innerHTML=r.svg;
                     document.title='OK';}})
  .catch(function(e){{document.getElementById('out').textContent='ERR '+e;
                      document.title='FAIL';}});
</script></body></html>"""


def chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    sys.exit("找不到 Chrome，改 CHROME_CANDIDATES 里的路径")


def ensure_bundle() -> str:
    if not BUNDLE.exists():
        print(f"  下载 mermaid bundle → {BUNDLE}")
        # ⚠️ jsdelivr 在这台机器上被 reset，unpkg 可以。别只写一个源。
        urllib.request.urlretrieve(BUNDLE_URL, BUNDLE)
    return BUNDLE.read_text(encoding="utf-8", errors="ignore")


def main() -> int:
    src = MMD.read_text(encoding="utf-8")
    first = next(l for l in src.splitlines() if l.strip())
    if not first.startswith("flowchart"):
        sys.exit("lls-dataflow.mmd 第一行必须是 flowchart（见文件头注释）")

    js = ensure_bundle()
    RENDER.write_text(PAGE.format(src=src, js=js), encoding="utf-8")

    dump = subprocess.run(
        [chrome(), "--headless=new", "--disable-gpu", "--virtual-time-budget=30000",
         "--dump-dom", RENDER.as_uri()],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
    ).stdout

    if "<title>OK" not in dump:
        m = re.search(r"ERR [^<]{0,200}", dump)
        sys.exit(f"渲染失败：{m.group(0) if m else '未知错误'}")

    i = dump.find('<div id="out">')
    m = re.search(r"<svg[\s\S]*?</svg>", dump[i:])
    if not m:
        sys.exit("渲染成功但没找到 svg")
    svg = m.group(0)
    if "xmlns=" not in svg:
        svg = svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
    SVG_OUT.write_text(svg, encoding="utf-8")

    vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    w = hgt = 0.0
    if vb:
        w, hgt = float(vb.group(1)), float(vb.group(2))
    subprocess.run(
        [chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--window-size={int(w) + 80},{int(hgt) + 80}",
         "--virtual-time-budget=12000",
         f"--screenshot={PNG_OUT}", RENDER.as_uri()],
        capture_output=True,
    )

    print(f"  OK  {SVG_OUT}")
    print(f"  OK  {PNG_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
r"""操作体验探针（真浏览器）：找角色 / 点击区尺寸 / 首屏密度 / 文案是否过时。

为什么单开一支：这些事**静态检查看不见**。
  · 「搜不到时有没有提示」只看 DOM 是看不出结果的 —— 得真输一个字；
  · 「点击区多大」要看渲染后的几何，CSS 里写 32px、被别的规则覆盖成 28px 很常见；
  · 「滤出来的是不是真的对」得比对每张卡的内容，不是数一数张数。

规矩（和其它真浏览器探针一致）：
  · 点输入框 / 点 × / 点折叠标题一律**真鼠标**（dispatchMouseEvent + 命中测试）；
  · 文字用 `Input.insertText` 走浏览器真实输入路径（会触发 input 事件，
    不是直接改 value —— 改 value 不触发事件，测不出"接没接上"）。

端口与 profile 都跟 probe_multi_ui 错开，两关可以先后跑。
用法（服务和探针必须同一条命令，见 verify_ui_ergonomics.py）：
    VOICESTUDIO_PORT=8897 python server.py &
    python tools/probe_ergonomics.py
"""
import json
import os
import subprocess
import sys
import time

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
# ★ 必须在 import 之前设：probe_multi_ui 在**模块级**读这两个环境变量。
os.environ.setdefault("VS_PORT", "8897")
os.environ.setdefault("VS_CDP_PORT", "9342")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

import probe_multi_ui as P  # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  [ok] " if cond else "  [FAIL] ") + name +
          (("  <- " + str(detail)) if detail else ""), flush=True)


def cards(c):
    return json.loads(c.js(r"""(()=>{
      const g=document.getElementById('grid');
      const names=[...g.querySelectorAll('.card[data-role]')].map(e=>{
        const n=e.querySelector('.nm'); const i=e.querySelector('.intro');
        return ((n?n.textContent:'')+' '+(i?i.textContent:'')).toLowerCase();});
      const fe=document.getElementById('findEmpty');
      return JSON.stringify({n:names.length, names:names,
        emptyShown: fe ? getComputedStyle(fe).display!=='none' : null,
        emptyQ: (document.getElementById('findEmptyQ')||{}).textContent||'',
        cnt: (document.getElementById('roleCount')||{}).textContent||''});})()"""))


def type_in(c, sel, text):
    ok, hit = c.click(sel)
    if not ok:
        return False, "点不到 %s：%s" % (sel, hit)
    c.js("(()=>{const e=document.querySelector(%s);e.select();return 1;})()"
         % json.dumps(sel))
    c.send("Input.insertText", text=text)
    time.sleep(0.5)
    return True, ""


def main():
    c = P.boot()
    if not c:
        print("  [FAIL] 浏览器起不来（Chrome / CDP 端口）", flush=True)
        BAD.append("浏览器起不来")
        print("\n通过 %d 项，失败 %d 项" % (len(OK), len(BAD)), flush=True)
        return 1
    try:
        c.js("window.scrollTo(0,0)")

        print("-" * 58 + "\nA 找角色：真的在过滤\n" + "-" * 58, flush=True)
        base = cards(c)
        check("页面有角色卡（后面的判据才有意义）", base["n"] > 3, base["n"])
        if base["n"] <= 3:
            # 没有角色就没法验，直接收工别假装绿
            print("\n通过 %d 项，失败 %d 项" % (len(OK), len(BAD)), flush=True)
            return 1

        kw = c.js(r"""(()=>{
          const n=document.querySelector('.card[data-role] .nm');
          const t=(n?n.textContent:'').trim();
          return t.slice(0,2) || t;})()""")
        print("  搜索词取自第一张卡：%r" % kw, flush=True)

        ok, why = type_in(c, "#roleFind", kw)
        check("真鼠标点得到搜索框、字输得进去", ok, why)
        r1 = cards(c)
        check("输真名字片段 => 张数真的变少（不是没反应）",
              r1["n"] < base["n"] and r1["n"] >= 1,
              "%d -> %d" % (base["n"], r1["n"]))
        check("剩下的每一张都真的含这个词（不是随便留的）",
              all(kw.lower() in n for n in r1["names"]),
              [n[:20] for n in r1["names"] if kw.lower() not in n])
        check("计数行说清「共几个 / 找到几个」",
              "找到" in r1["cnt"] and str(r1["n"]) in r1["cnt"], r1["cnt"].strip())
        check("过滤时不弹「没找到」", r1["emptyShown"] is False)

        print("-" * 58 + "\nB 搜不到 => 必须说清楚（负对照）\n" + "-" * 58, flush=True)
        ok, why = type_in(c, "#roleFind", "zzz不存在zzz")
        r2 = cards(c)
        check("搜不到时一张都不留（不是把全部留下）", r2["n"] == 0, r2["n"])
        check("搜不到时给「没找到」提示（不是静默空白）", r2["emptyShown"] is True)
        check("提示里带回用户输的词", "zzz" in (r2["emptyQ"] or ""), r2["emptyQ"])

        print("-" * 58 + "\nC 真鼠标点 × 恢复全部\n" + "-" * 58, flush=True)
        ok, hit = c.click("#roleFindX")
        check("× 按钮点得到（真鼠标 + 命中测试）", ok, hit)
        time.sleep(0.4)
        r3 = cards(c)
        check("点完恢复全部（%d 张）" % base["n"], r3["n"] == base["n"],
              "%d vs %d" % (r3["n"], base["n"]))
        check("输入框被清空（不是只放开过滤）",
              not (c.js("document.querySelector('#roleFind').value") or ""))
        check("「没找到」提示收起来了", r3["emptyShown"] is False)

        print("-" * 58 + "\nD 点击区与密度\n" + "-" * 58, flush=True)
        m = json.loads(c.js(r"""(()=>{
          const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
          const sm=[...document.querySelectorAll('.btn.sm')].filter(vis);
          const h=sm.map(e=>Math.round(e.getBoundingClientRect().height));
          const w=sm.map(e=>Math.round(e.getBoundingClientRect().width));
          const card=document.querySelector('.card[data-role]');
          const st=document.getElementById('secStage');
          const hd=document.querySelector('#secStage .stage-h');
          const det=document.getElementById('stageMore');
          return JSON.stringify({
            smN:sm.length, minH:h.length?Math.min(...h):0,
            minW:w.length?Math.min(...w):0,
            cardH:card?Math.round(card.getBoundingClientRect().height):0,
            stageH:Math.round(st.getBoundingClientRect().height),
            headChars:hd.innerText.replace(/\s+/g,'').length,
            detOpen:det?det.open:null,
            overflowX: document.documentElement.scrollWidth>innerWidth
                       ? document.documentElement.scrollWidth:0});})()"""))
        check("小按钮短边都 >=32px（原 28，手抖点不空）", m["minH"] >= 32,
              "最矮 %dpx / %d 个" % (m["minH"], m["smN"]))
        # ★ 宽度只防「挤成一条竖线」，不拿它当手感指标：宽度天然跟文字长短走，
        #   清空按钮 "×" 只有 51px 是很正常的（它就这么点字）。
        #   第一版把阈值定成 60，把 × 判红了 —— 那是尺子定错，不是按钮有问题。
        check("小按钮没被挤成一条竖线（>=40px）", m["minW"] >= 40, m["minW"])
        check("角色卡没被撑变形（原 252，允许 ±30）", 222 <= m["cardH"] <= 282, m["cardH"])
        check("合成台高度没被撑大（原 330，允许 +40）", m["stageH"] <= 370, m["stageH"])
        check("没有横向滚动条", m["overflowX"] == 0, m["overflowX"])
        check("合成台标题区字数从 110 降到 80 以下", m["headChars"] < 80, m["headChars"])
        check("对唱说明默认收起", m["detOpen"] is False)

        print("-" * 58 + "\nE 对唱说明：真鼠标能展开、内容不是过时的\n" + "-" * 58, flush=True)
        ok, hit = c.click("#stageMore > summary")
        time.sleep(0.3)
        st = json.loads(c.js(r"""(()=>{
          const d=document.getElementById('stageMore');
          const t=(d.innerText||'').replace(/\s+/g,'');
          return JSON.stringify({open:d.open, chars:t.length,
            saysNew: t.indexOf('只唱分到')>=0,
            saysOld: t.indexOf('整首唱一遍')>=0,
            h:Math.round(d.getBoundingClientRect().height)});})()"""))
        check("真鼠标点标题能展开", ok and st["open"] is True,
              "hit=%s open=%s" % (hit, st["open"]))
        check("展开后有内容（不是空壳）", st["h"] > 20 and st["chars"] > 20,
              "%dpx / %d 字" % (st["h"], st["chars"]))
        check("说的是「只唱分到的那几句」（不是过时的「整首唱一遍」）",
              st["saysNew"] and not st["saysOld"],
              "新=%s 旧=%s" % (st["saysNew"], st["saysOld"]))

        print("-" * 58 + "\nF 全程 JS 异常\n" + "-" * 58, flush=True)
        errs = json.loads(c.js("JSON.stringify(window.__errs||[])"))
        check("没有 JS 异常（有异常说明某段逻辑根本没跑）", not errs, errs[:3])

    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(c.chrome.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    print("\n通过 %d 项，失败 %d 项" % (len(OK), len(BAD)), flush=True)
    for b in BAD:
        print("  - %s" % b, flush=True)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())

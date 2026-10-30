# -*- coding: utf-8 -*-
"""
死代码 / 冗余扫描器（VoiceStudio）

★ 背景（这次事故的教训）

  上一轮出过一个典型怪事：`serve_file(path, download=True)` —— 这个 `download`
  参数**从被写出来的那天起就从来没被实现过**，只是个占位。助手加 Range 支持时
  "顺手把它实现了"，结果 `/` 和 `/static/*` 全被当成附件下载，Chrome 不渲染，
  页面白屏。curl 还照样返回 200，极难定位。

  教训：**定义了却没人用的东西，不是"以后可能有用"，是定时炸弹。**
  它骗过所有人（"这不是有这功能吗"），然后在某次改动里被引爆。
  ⇒ 第一选择永远是删掉，不是实现它。

  这个脚本就是把"定义了但没人用"的东西自动列出来，作为人工复核的候选清单。

★ 它查什么

  1. 未使用的 import（顶层 import 的名字在文件里再没出现过）
  2. 定义了但全项目零引用的顶层函数 / 类
  3. 定义了路由但前端从没请求过的 API 端点（跨语言：Python 路由 ↔ web/*.js 的 fetch）
  4. 重复的函数体（同样的代码块出现多次 ⇒ 该抽公共函数了）

★ 边界

  - 只读。绝不改任何文件。
  - 输出是**候选清单**，不是"该删清单"。第 2 类要人工复核：
    动态调用（`getattr` / 注册表 / 字符串派发）扫不出来，会误报。
  - 不扫 `data/` `models/` `web/` 以外的第三方目录。

用法：
  python tools/deadcode.py
  python tools/deadcode.py --py-only
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"data", "models", "steps", "_archive", ".git", "__pycache__",
             ".cdp-ability", ".cdp-viz", ".cdp-nav", "node_modules"}


def iter_py():
    for d, dns, fs in os.walk(ROOT):
        dns[:] = [x for x in dns if x not in SKIP_DIRS and not x.startswith(".")]
        for f in fs:
            if f.endswith(".py"):
                yield os.path.join(d, f)


def iter_js():
    web = os.path.join(ROOT, "web")
    if not os.path.isdir(web):
        return
    for f in sorted(os.listdir(web)):
        if f.endswith((".js", ".html")):
            yield os.path.join(web, f)


def src(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ── 1. 未使用的 import ───────────────────────────────────────────────────
def unused_imports(tree, text):
    """未使用的顶层 import。

    ★ 带 `# noqa: F401` 的一律跳过 —— 那是**故意留着**的（比如只为确认
      环境里装了 scipy），照着删就是把环境自检给拆了。
    """
    out = []
    body = text
    lines = text.splitlines()
    for node in ast.walk(tree):
        n = getattr(node, "lineno", None)
        if n is None:                 # ast.walk 会带出 Module 等没有行号的节点
            continue
        ln = n - 1
        if 0 <= ln < len(lines) and "noqa" in lines[ln]:
            continue
        if isinstance(node, ast.Import):
            for a in node.names:
                name = (a.asname or a.name).split(".")[0]
                # 出现次数 == 1 说明只有 import 这一行提到它
                if body.count(name) <= 1:
                    out.append((node.lineno, "import " + a.name))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                name = a.asname or a.name
                if body.count(name) <= 1:
                    out.append((node.lineno, "from %s import %s" % (node.module, a.name)))
    return out


# ── 2. 顶层定义 vs 全项目引用 ────────────────────────────────────────────
def top_defs(tree):
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(("def", n.name, n.lineno))
        elif isinstance(n, ast.ClassDef):
            out.append(("class", n.name, n.lineno))
    return out


def collect_all_text():
    blobs = []
    for p in list(iter_py()) + list(iter_js()):
        blobs.append((p, src(p)))
    return blobs


def find_dead_defs(blobs):
    """顶层定义，在其他所有文件里都没出现过 ⇒ 死代码候选。"""
    dead = []
    for p, _ in blobs:
        try:
            tree = ast.parse(src(p))
        except SyntaxError:
            continue
        for kind, name, line in top_defs(tree):
            if name.startswith("__"):
                continue
            hits = 0
            for q, txt in blobs:
                if q == p:
                    # 同文件内：去掉定义行本身再数
                    hits += max(0, txt.count(name) - 1)
                else:
                    hits += txt.count(name)
            if hits == 0:
                dead.append((p, kind, name, line))
    return dead


# ── 3. 路由 vs 前端调用 ──────────────────────────────────────────────────
def find_dead_routes(blobs):
    """Python 里注册的 /api/xxx 路由，全项目没人请求过的 ⇒ 候选。

    ★ 踩过的坑：第一版只扫 `web/*.js`，漏了**门禁脚本里内嵌的 JS 字符串**
      （`page.js("...fetch('/api/music/local/check')...")`）。结果把
      `/api/music/local/check` 误报成死路由 —— 而它正是门禁在测的功能，
      注释里还写着「死代码复活」。照着误报删就等于拆功能。
      ⇒ 引用面必须覆盖 **py + js**，不能只看前端。
    """
    srv = os.path.join(ROOT, "server.py")
    stxt = src(srv)
    # 只认 server.py 里的注册：精确型 + 前缀型
    exact = set(m.strip("'\"") for m in re_findall(r'path\s*==\s*["\']/api/[^"\']*["\']', stxt)
                for m in [m[m.index("/api"):].rstrip("\"'")])
    exact = set(re_findall(r'path\s*==\s*["\'](/api/[^"\']*)["\']', stxt))
    pre = set(re_findall(r'path\.startswith\(\s*["\'](/api/[^"\']*)["\']', stxt))

    # 全项目出现过的 /api 字面量（含 query），py + js 都算。
    # ★ 必须剔掉 server.py 里的**注册行**本身，否则 `c == route` 恒成立、
    #   每条路由都"有调用方" ⇒ 结果恒 0 处，尺子彻底失效（见 §3 的坑）。
    calls = set()
    for q, txt in blobs:
        if q == srv:
            txt = "\n".join(ln for ln in txt.splitlines()
                            if "path ==" not in ln and "path.startswith" not in ln)
        for m in re_findall(r"['\"]/api/[A-Za-z0-9_/\-\.\{\}]*(?:\?[^'\"]*)?['\"]", txt):
            calls.add(m.strip("'\""))

    def used(route):
        for c in calls:
            if c == route or c.startswith(route + "?") or c.startswith(route + "/"):
                return True
        return False

    out = []
    for r in sorted(exact | pre):
        if not used(r):
            out.append((srv, r))
    return out


def re_findall(pat, s):
    import re as _re
    return _re.findall(pat, s)


# ── 4. 重复函数体 ────────────────────────────────────────────────────────
def dup_bodies(blobs):
    seen = {}
    for p, txt in blobs:
        if not p.endswith(".py"):
            continue
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and len(n.body) >= 3:
                try:
                    key = ast.dump(ast.Module(body=n.body, type_ignores=[]))
                except Exception:
                    continue
                seen.setdefault(key, []).append("%s:%d %s()" % (short(p), n.lineno, n.name))
    return [v for v in seen.values() if len(v) > 1]


def short(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def main():
    py_only = "--py-only" in sys.argv
    blobs = collect_all_text()
    print("=" * 62)
    print("死代码扫描   root=%s" % ROOT)
    print("扫描 %d 个文件" % len(blobs))
    print("=" * 62)

    # 1
    print("\n[1] 未使用的 import")
    n = 0
    for p, txt in blobs:
        if not p.endswith(".py"):
            continue
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        for line, what in unused_imports(tree, txt):
            print("   %s:%d  %s" % (short(p), line, what))
            n += 1
    print("   共 %d 处" % n)

    # 2
    print("\n[2] 顶层定义但全项目零引用（★ 需人工复核，动态调用会误报）")
    dead = find_dead_defs(blobs)
    for p, kind, name, line in dead:
        print("   %s:%d  %s %s()" % (short(p), line, kind, name))
    print("   共 %d 处" % len(dead))

    if not py_only:
        # 3
        print("\n[3] 后端注册了但前端没请求过的 /api 路由")
        dr = find_dead_routes(blobs)
        for p, route in sorted(dr):
            print("   %-22s %s" % (short(p), route))
        print("   共 %d 处" % len(dr))

    # 4
    print("\n[4] 函数体完全重复（该抽公共函数）")
    dups = dup_bodies(blobs)
    for v in dups[:15]:
        print("   ×%d  %s" % (len(v), " | ".join(v[:4])))
    print("   共 %d 组" % len(dups))

    print("\n" + "-" * 62)
    print("⚠ 以上均为候选。删之前逐个确认：[2] 类尤要查有没有 getattr/字符串派发。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

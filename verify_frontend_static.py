r"""前端静态自检：找出「读了但看不到的名字」。

为什么需要它
------------
改 web/app.js 时，同一个机理已经栽了 **5 次** —— 短的声明行被整行删掉，
用法却留着，于是运行时 ReferenceError：

    1. shade()       里的  const n = parseInt(hex...)         -> render() 抛错，角色墙一张卡都不画
    2. 顶层                let STAGE_SONG = null;              -> 「一键合成」永远点不亮
    3. renderSteps()  里的  const cur = box.querySelector()    -> 「每步清单」整块画不出来
    4. 顶层                let pickedVideo = null;             -> 拖视频进来点「开始拆解」报错
    5. tickJobs()     里的  meta = $("#jn-" + id)              -> **任务永远显示不出"完成"**，
                                                                 watching 永不清空 ⇒ busyGuard 永久拦住新任务；
                                                                 所有完成回调（导歌填②号槽、训完刷新角色卡）全失效

第 5 次是**最贵的一次**：它不报在任何"功能"上，只是任务条卡住；只有
"真跑页面 + 真等一个任务结束 + 盯一个由完成回调驱动的东西"才看得见。

这种 bug 有三个特点，常规手段全都拦不住：
  · `node --check` 语法**完全合法**（读一个不存在的名字不是语法错误）；
  · 后端接口、文件、数据都正常，curl 全绿；
  · 只有**真跑页面**、而且**正好走到那条分支**才报错。

真浏览器验收当然要做，但那是"按路径"的，漏掉的分支要等下一次才被踩到。
这个脚本是"按符号"的兜底。

★ v2：从"扁平集合"升级为"作用域感知"
  第一版把所有文件的声明收成**一个集合**，于是第 5 次那个 bug 它报"0 可疑" ——
  因为 `meta` 确实**声明过**，只不过声明在**另一个块**里：
      } catch (e) { const meta = $("#jn-" + id); ... }     // 只在这个 catch 块里可见
      ... if (meta) { ... }                                 // 这里是函数体，看不到它
  "声明过"和"这里是看得见的"是两回事。现在按块作用域建**作用域树**，
  每次读取都从**最内层往外**找，找不到就报；找到的若是**兄弟/内层块**，报"作用域用错"。

抓法
  1. 用一个真字符扫描器把注释 / 字符串 / 模板串 / 正则剥掉（模板串用模式栈，见 scan）；
  2. 建作用域树：每个 `{` 一个作用域，标出哪些是函数体（var / function 要向上提升）；
  3. 收集声明（var/let/const 含解构、function 名与形参、箭头形参、catch 形参、
     class 名、for-of/for-in），挂到**声明所在的那个作用域**；
  4. 收集"当值用"的标识符（排除 `.xxx` 成员访问、排除对象字面量的键）；
  5. 每次读取沿作用域链向外找；跨文件共享的只有最顶层作用域（和以前一致）。

用法
    python verify_frontend_static.py               # 查 web/ 下三个 js
    python verify_frontend_static.py a.js b.js     # 查指定文件（做负对照用）
    python verify_frontend_static.py --selftest    # 自测：五个内置样例，必须红/绿如预期
退出码 0 = 可疑项为 0（且自测通过）。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# ★ 2026-10-14 补 web/viz.js：新脚本**上线了却没进扫描名单**，结果它提供给 app.js
#   的 `vizFromOutput` 被判成"未声明"的假阳性（真问题其实是名单漏了）。
#   ⇒ 所以下面有一条判据盯着"index.html 引入的每个本地脚本都必须在这个名单里"，
#     免得下次加脚本又悄悄逃过静态检查。
FILES = ["web/app.js", "web/lib.js", "web/loc.js", "web/mkt.js", "web/viz.js"]

PUNCT = set("(){}[];,.<>=+-*/%&|^!~?:")
RESERVED = set("""
var let const function class return if else for while do switch case break continue
new delete typeof instanceof in of void this null true false undefined NaN Infinity
try catch finally throw async await yield static get set default export import from
extends super arguments
""".split())

# 浏览器 / JS 内建 + 本项目里由 HTML 内联脚本提供的全局
BUILTIN = set("""
window document location navigator history screen console fetch localStorage
sessionStorage setTimeout clearTimeout setInterval clearInterval
requestAnimationFrame cancelAnimationFrame alert confirm prompt
JSON Math Date Number String Boolean Object Array Promise Error TypeError
RegExp Map Set WeakMap WeakSet Symbol BigInt Proxy Reflect
parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent
atob btoa structuredClone queueMicrotask
File FileReader FormData Blob URL URLSearchParams Headers Request Response
Image Audio Event CustomEvent AbortController TextEncoder TextDecoder
Intl globalThis performance crypto
AudioContext webkitAudioContext
loadLibrary locInit
""".split())


# --------------------------------------------------------------------------
# 扫描：一遍剥掉注释/字符串/模板串，留下 ("id"|"p", 文本, 原文偏移)
# --------------------------------------------------------------------------
def scan(src):
    """注释 / 字符串 / 模板串里的文本都不算代码。

    ★ 模板串必须用**模式栈**处理，不能"从反引号扫到下一个反引号"：
      这个项目的界面全是嵌套模板 ——
          ${cond ? `<span class="chip">成品 ${n}</span>` : ""}
      内层反引号会提前结束外层，于是 `<span` / `button` / `div` 这些**标签名**
      被当成标识符读出来，第一版因此报了一堆假阳性（span/br/div/button/...）。
      规则：进 ` 是模板模式；模板里的 ${ 切到表达式模式（真代码）；
      表达式模式里再遇到 ` 又是模板……靠栈配对，${} 的层数就自然对了。
    """
    n = len(src)
    tokens = []
    stack = ["code"]          # 模式栈：code / tpl / expr:<brace深度>
    i = 0
    prev_sig = " "

    while i < n:
        top = stack[-1]
        mode = top.split(":")[0]
        c = src[i]

        if mode == "tpl":
            if c == "\\":
                i += 2
                continue
            if c == "`":
                stack.pop()
                i += 1
                prev_sig = "`"
                continue
            if c == "$" and i + 1 < n and src[i + 1] == "{":
                stack.append("expr:0")
                i += 2
                prev_sig = "{"
                continue
            i += 1
            continue

        # code / expr 共用：先剥注释与字符串
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c in "\"'":
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            prev_sig = q
            continue
        if c == "`":
            stack.append("tpl")
            i += 1
            prev_sig = "`"
            continue

        if mode == "expr":
            d = int(top.split(":")[1])
            if c == "{":
                stack[-1] = "expr:%d" % (d + 1)
            elif c == "}":
                if d == 0:
                    stack.pop()
                    i += 1
                    prev_sig = "}"
                    continue
                stack[-1] = "expr:%d" % (d - 1)

        if c == "/":
            # 正则字面量 vs 除号。
            # 规则：`/` 前面若是**标识符 / 数字 / ) / ]**，它一定是除号。
            #   原版按"后面跟的是什么"猜，并且把空格也算进"合法的后续"，
            #   于是在 `const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);`
            #   上把 `s / 3600), m = ...` 整段当正则吞掉 —— 后果是 `m` 的声明
            #   没被收集，反而被判成"作用域用错"（假阳性 + 假阴性同时发生）。
            #   实测：不修这条，web/app.js 会稳定报 1 项假阳性。
            # （第一版按"前一个字符"猜，撞上 .replace(/[&<>"']/g) 之后又反过来
            #   把整段代码当字符串剥掉。两次都说明：这个判别要按**语法位置**做。）
            if prev_sig in ("x", "0", ")", "]"):
                i += 1
                prev_sig = "/"
                continue
            j = i + 1
            closed = -1
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    while j < n and src[j] != "]":
                        j += 2 if src[j] == "\\" else 1
                    if j >= n:
                        break
                    j += 1
                    continue
                if src[j] == "/":
                    closed = j
                    break
                j += 1
            if closed > 0:
                t = closed + 1
                while t < n and src[t] in "gimsuyGIMSUYv":
                    t += 1
                nxt = src[t] if t < n else ""
                if nxt == "" or nxt in ",;)}]. \t\r\n":
                    i = t
                    prev_sig = "/"
                    continue

        if c.isascii() and (c.isalpha() or c in "_$"):
            j = i
            while j < n and src[j].isascii() and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            tokens.append(("id", src[i:j], i))
            i = j
            prev_sig = "x"
            continue
        if c.isdigit():
            j = i
            while j < n and (src[j].isalnum() or src[j] == "."):
                j += 1
            i = j
            prev_sig = "0"
            continue
        if c in PUNCT:
            two = src[i:i + 2]
            if two in ("=>", "==", "!=", ">=", "<=", "&&", "||", "??", "+=", "-=", "++", "--", "?."):
                tokens.append(("p", two, i))
                i += 2
                prev_sig = two
                continue
            tokens.append(("p", c, i))
            i += 1
            if not c.isspace():
                prev_sig = c
            continue
        i += 1

    return tokens


# --------------------------------------------------------------------------
# 作用域树
# --------------------------------------------------------------------------
NON_FUNC_HEADS = {"if", "for", "while", "switch", "catch", "with", "do", "else",
                  "return", "typeof", "delete", "in", "of", "new", "await", "case"}


def matching(tokens, idx, open_t, close_t):
    """idx 是 open_t 的位置，向后找配对的 close_t；找不到返回 None。"""
    d = 0
    for j in range(idx, len(tokens)):
        k, t, _ = tokens[j]
        if k == "p" and t == open_t:
            d += 1
        elif k == "p" and t == close_t:
            d -= 1
            if d == 0:
                return j
    return None


def matching_back(tokens, idx, close_t, open_t):
    """idx 是 close_t 的位置，向前找配对的 open_t；找不到返回 None。

    形参列表要**往回**配对：`{` 前面是 `)`，而它的 `(` 在更左边。
    一开始这里调了正向的 matching，等于朝右找左括号 —— 永远找不到，
    于是所有函数体都被当成了普通块 ⇒ var/function 提升位置全错（假阳性源）。
    """
    d = 0
    for j in range(idx, -1, -1):
        k, t, _ = tokens[j]
        if k == "p" and t == close_t:
            d += 1
        elif k == "p" and t == open_t:
            d -= 1
            if d == 0:
                return j
    return None


def opens_func_body(tokens, i):
    """tokens[i] 是 `{`。判断它开的是不是**函数体**（var / function 要提升到这里）。"""
    if i == 0:
        return False
    k, t, _ = tokens[i - 1]
    if k == "p" and t == "=>":
        return True
    if k == "p" and t == ")":
        op = matching_back(tokens, i - 1, ")", "(")
        if op is None or op == 0:
            return False
        pk, pt = tokens[op - 1][0], tokens[op - 1][1]
        if pk == "id" and pt == "function":
            return True
        if pk == "id" and pt not in NON_FUNC_HEADS and pt not in RESERVED:
            return True
    return False


BLOCK_HEADS = {")", "else", "do", "try", "finally", ";", "{", "}"}


def opens_scope_body(tokens, i):
    """tokens[i] 是 `{`。判断它是不是**语句块 / 函数体**（而不是对象字面量或解构模式）。

    ★ 这一条是踩出来的：`const { items, skipped, root } = LOC_SCAN;` 里那个 `{`
    不是块，却被当成块 ⇒ 三个名字被塞进一个"兄弟作用域"，函数体里每一次读
    全部判红（loc.js 一次性 3 项假阳性，共 14 次读取）。
    规则：`{` 前面若是 `)` `else` `do` `try` `finally` `;` `{` `}`（语句位置），
    或它本身是函数体，才算开作用域；前面是 `=` `,` `(` `[` `return` `const`
    这类**表达式位置**的，是对象字面量/解构模式，不开作用域。
    """
    if i == 0:
        return False
    if opens_func_body(tokens, i):
        return True
    k, t, _ = tokens[i - 1]
    return k == "p" and t in BLOCK_HEADS


def build_scopes(tokens):
    """返回 (scopes, scope_of_token)。
    scopes[sid] = {parent, names:set, start, end, is_func}
    scope_of_token[i] = 第 i 个 token **所在**的最内层作用域（`{` 本身算外层）。

    花括号**不一定**开作用域（对象字面量/解构），所以开括号栈要单独记
    "这个括号到底推没推作用域"，否则右括号会 pop 错一个。
    """
    scopes = {0: {"parent": None, "names": set(), "start": -1,
                  "end": len(tokens), "is_func": True}}
    stack = [0]
    pushed = []            # 与未闭合的 `{` 一一对应：True=推了作用域
    nxt = 1
    scope_of_token = [0] * len(tokens)
    for i, (k, t, _) in enumerate(tokens):
        scope_of_token[i] = stack[-1]
        if k == "p" and t == "{":
            if opens_scope_body(tokens, i):
                scopes[nxt] = {"parent": stack[-1], "names": set(), "start": i,
                               "end": None, "is_func": opens_func_body(tokens, i)}
                stack.append(nxt)
                pushed.append(True)
                nxt += 1
            else:
                pushed.append(False)
        elif k == "p" and t == "}":
            if pushed:
                did = pushed.pop()
                if did and len(stack) > 1:
                    sid = stack.pop()
                    scopes[sid]["end"] = i
    return scopes, scope_of_token


def hoist_target(scopes, sid):
    """var / function 声明的落点：向外找到最近一个**函数体**（或模块顶层）。"""
    cur = sid
    while cur is not None and not scopes[cur]["is_func"]:
        cur = scopes[cur]["parent"]
    return cur if cur is not None else 0


def _scope_opened_at(scopes, brace_idx):
    for sid, sc in scopes.items():
        if sc["start"] == brace_idx:
            return sid
    return 0


def collect_decls(tokens, scopes, scope_of_token):
    """把每条声明挂到正确的作用域。返回 [(名字, 作用域id, 类别, token下标)]。"""
    out = []
    assign = []          # 形参等：先记下标，等作用域树建好再定位
    n = len(tokens)

    for idx, (k, t, _) in enumerate(tokens):
        if k != "id":
            continue

        if t in ("var", "let", "const"):
            j = idx + 1
            depth = 0
            while j < n:
                kk, tt, _ = tokens[j]
                if kk == "id" and tt in RESERVED and tt not in ("true", "false", "null"):
                    break
                if kk == "p" and tt in "([{":
                    depth += 1
                elif kk == "p" and tt in ")]}":
                    depth -= 1
                    if depth < 0:
                        break
                elif depth == 0 and kk == "p" and tt == ";":
                    break
                elif depth == 0 and kk == "p" and tt == "=":
                    # 跳过这一项的初始化表达式
                    d2 = 0
                    j += 1
                    while j < n:
                        k3, t3, _ = tokens[j]
                        if k3 == "p" and t3 in "([{":
                            d2 += 1
                        elif k3 == "p" and t3 in ")]}":
                            if d2 == 0:
                                break
                            d2 -= 1
                        elif d2 == 0 and k3 == "p" and t3 in ",;":
                            break
                        j += 1
                    continue
                if kk == "id" and tt not in RESERVED:
                    sid = scope_of_token[j]
                    if t == "var":
                        sid = hoist_target(scopes, sid)
                    out.append((tt, sid, t, j))
                j += 1
            continue

        if t == "function":
            j = idx + 1
            if j < n and tokens[j][0] == "id" and tokens[j][1] not in RESERVED:
                out.append((tokens[j][1], hoist_target(scopes, scope_of_token[j]),
                            "function", j))
                j += 1
            while j < n and tokens[j][1] != "(":
                j += 1
            if j < n:
                close = matching(tokens, j, "(", ")")
                if close:
                    for m in range(j + 1, close):
                        if tokens[m][0] == "id" and tokens[m][1] not in RESERVED:
                            assign.append(("param", m, j))
            continue

        if t == "catch":
            j = idx + 1
            if j < n and tokens[j][1] == "(":
                close = matching(tokens, j, "(", ")")
                if close:
                    for m in range(j + 1, close):
                        if tokens[m][0] == "id" and tokens[m][1] not in RESERVED:
                            assign.append(("catch", m, j))
            continue

        if t == "class":
            j = idx + 1
            if j < n and tokens[j][0] == "id":
                out.append((tokens[j][1], scope_of_token[j], "class", j))
            continue

    # 箭头函数形参：( a , b ) =>   /   a =>
    for idx, (k, t, _) in enumerate(tokens):
        if k != "p" or t != "=>":
            continue
        j = idx - 1
        if j >= 0 and tokens[j][0] == "id":
            assign.append(("param", j, j - 1))
        elif j >= 0 and tokens[j][1] == ")":
            close = j
            d = 0
            k2 = close
            while k2 >= 0:
                if tokens[k2][1] == ")":
                    d += 1
                elif tokens[k2][1] == "(":
                    d -= 1
                    if d == 0:
                        for m in range(k2 + 1, close):
                            if tokens[m][0] == "id" and tokens[m][1] not in RESERVED:
                                assign.append(("param", m, k2))
                        break
                k2 -= 1

    for kind, m, anchor in assign:
        if kind == "catch":
            # catch 形参只活在紧跟的 { } 里
            j = matching(tokens, anchor, "(", ")")
            tgt = scope_of_token[anchor]
            if j is not None and j + 1 < n and tokens[j + 1][1] == "{":
                tgt = _scope_opened_at(scopes, j + 1)
            out.append((tokens[m][1], tgt, "catch", m))
        else:
            # 形参挂在函数体上；没有块体（表达式箭头）就挂在当前位置
            out.append((tokens[m][1], _param_target(tokens, scopes, scope_of_token, anchor),
                        "param", m))

    return out


def _param_target(tokens, scopes, scope_of_token, anchor):
    """anchor 是形参列表的 `(`（或用单参箭头时的形参下标）。"""
    if tokens[anchor][1] != "(":
        return scope_of_token[anchor]
    close = matching(tokens, anchor, "(", ")")
    if close is None:
        return scope_of_token[anchor]
    j = close + 1
    if j < len(tokens) and tokens[j][1] == "=>":
        j += 1
    if j < len(tokens) and tokens[j][1] == "{":
        return _scope_opened_at(scopes, j)
    return scope_of_token[anchor]


def chain(scopes, sid):
    out = []
    cur = sid
    while cur is not None:
        out.append(cur)
        cur = scopes[cur]["parent"]
    return out


def uses_of(tokens):
    """当值用的标识符：[（名字, token下标, 行号）]。"""
    out = []
    for i, (k, t, pos) in enumerate(tokens):
        if k != "id" or t in RESERVED or t in BUILTIN:
            continue
        if i > 0 and tokens[i - 1][0] == "p" and tokens[i - 1][1] == ".":
            continue                       # 成员访问 .xxx
        if i > 1 and tokens[i - 1][0] == "p" and tokens[i - 1][1] == "?":
            pass                           # 可选链 a?.b 已在扫描期合并
        if i + 1 < len(tokens) and tokens[i + 1][1] == ":":
            # 对象字面量的键（`{k: v}` 或 `{a, k: v}`）不算读取；
            # 三元 `c ? x : y` 里的 x 前面是 `?`，不是这两种，仍算读取。
            if i == 0 or tokens[i - 1][1] in ("{", ","):
                continue
        out.append((t, i, pos))
    return out


# --------------------------------------------------------------------------
def run(files):
    """跑一批文件，返回 [(文件, 类别, 名字, 行号)]。"""
    sources = [(f, open(f, encoding="utf-8").read()) for f in files]
    prepared = []          # (f, src, tokens, scopes, scope_of_token, decls)

    # 声明是提升的 ⇒ 先把**所有**文件的顶层声明收齐（跨文件可见性），
    # 再判每一次读取。否则"读在声明之前"会报假阳性。
    module_names = set()
    for f, src in sources:
        tokens = scan(src)
        scopes, scope_of_token = build_scopes(tokens)
        decls = collect_decls(tokens, scopes, scope_of_token)
        # ★ 必须把声明**写回作用域**，否则 scopes[*]["names"] 恒为空，
        #   每一次读取都会判红（自测第一版就是这么红的：12 项全红）。
        for name, sid, _kind, _idx in decls:
            scopes[sid]["names"].add(name)
        prepared.append((f, src, tokens, scopes, scope_of_token, decls))
        for name, sid, _kind, _idx in decls:
            # 只有**顶层**声明跨文件可见。函数体内层声明的 function/var 由
            # hoist_target 提升到最近的函数体作用域，不会混进模块级
            # —— 否则内层同名函数会把外面的真 bug 遮掉（判据越松越没用）。
            if sid == 0:
                module_names.add(name)

    found = []
    for f, src, tokens, scopes, scope_of_token, decls in prepared:
        decl_scope = {}
        # ★ 声明**位置本身**不算"读取"。`function esc(x) {}` 里那个形参 x，
        #   它的位置在函数体 `{` **之前**（作用域还没进去），而声明落点在函数体里
        #   ⇒ 不排除掉的话，每个形参都会"自己把自己"报成作用域用错。
        #   自测第一版就是这么红的（x / a / b 全报）。
        decl_idx = set(idx for _n, _s, _k, idx in decls)
        for name, sid, _kind, _idx in decls:
            decl_scope.setdefault(name, set()).add(sid)
        for name, i, pos in uses_of(tokens):
            if i in decl_idx:
                continue
            line = src.count("\n", 0, pos) + 1
            if name in module_names:
                continue
            if any(name in scopes[anc]["names"] for anc in chain(scopes, scope_of_token[i])):
                continue
            kind = "作用域用错" if decl_scope.get(name) else "未声明"
            found.append((f, kind, name, line))
    found.extend(duplicate_decls(prepared))
    return found


def for_head_indices(tokens):
    """`for (...)` 头里出现的 token 下标。

    ★ 用来排除假阳性：`for (const j of alive)` 里那个 `j` 的绑定属于**循环自己**
      （每次迭代新绑一个），同一个函数体里写三个 `for (const j of ...)` 完全合法。
      第一版没排除，app.js 的 `tickJobs` 立刻被报成"重复声明 j"——判据自己的 bug。
    """
    inside = set()
    n = len(tokens)
    for i, (k, t, _) in enumerate(tokens):
        if k == "id" and t == "for":
            j = i + 1
            if j < n and tokens[j][0] == "p" and tokens[j][1] == "(":
                close = matching(tokens, j, "(", ")")
                if close:
                    inside.update(range(j + 1, close))
    return inside


def duplicate_decls(prepared):
    """同一个作用域里把**同一个名字声明了两遍**。

    ★ 这一条是踩出来的：app.js 里 `fmtSec` 在**同一个作用域**声明了两遍，
      后一份胜出 ⇒ 改前一份完全不生效（而"读得到"的判据一条都不会红，
      界面只是显示成 "4 分 60 秒"）。这种坑靠读代码几乎看不出来。

    只算 function / class / let / const / var；**排除形参与 catch 形参**
    （它们与 `var` 同名是合法且常见的写法，报了就是噪声）。
    """
    out = []
    for f, src, tokens, _scopes, _sot, decls in prepared:
        skip = for_head_indices(tokens)
        seen = {}
        for name, sid, kind, idx in decls:
            if kind in ("param", "catch") or idx in skip:
                continue
            seen.setdefault((sid, name), []).append(idx)
        for (sid, name), idxs in sorted(seen.items(), key=lambda kv: kv[1][0]):
            if len(idxs) < 2:
                continue
            line = src.count("\n", 0, tokens[idxs[0]][2]) + 1
            out.append((f, "重复声明", name, line))
    return out


def compress(found):
    """同一个名字同一个文件只报一次（记首次出现的行）。"""
    seen = {}
    for f, kind, name, line in found:
        seen.setdefault((f, kind, name), [line, 0])
        seen[(f, kind, name)][0] = min(seen[(f, kind, name)][0], line)
        seen[(f, kind, name)][1] += 1
    return seen


SELFTEST_PRELUDE = """
const PRELUDE_BOX = document.getElementById("x");
function work() {}
function esc(x) { return String(x); }
function shade(c) { return c; }
const color = "#fff";
const box = PRELUDE_BOX;
"""

SELFTEST_FIX_LEAK = """
function tick(id) {
  try { work(); }
  catch (e) {
    const meta = document.querySelector("#jn-" + id);
    if (meta) meta.textContent = "重连中";
    return;
  }
  box.innerHTML = esc(shade(color));
  if (meta) { meta.innerHTML = "已用 3 秒"; }
}
"""
SELFTEST_FIX_OK = """
function tick(id) {
  const meta = document.querySelector("#jn-" + id);
  try { work(); }
  catch (e) {
    if (meta) meta.textContent = "重连中";
    return;
  }
  box.innerHTML = esc(shade(color));
  if (meta) { meta.innerHTML = "已用 3 秒"; }
}
"""
SELFTEST_FIX_UNDECLARED = """
function paint() {
  box.innerHTML = esc(shade(color));
  return nopeNotAName + 1;
}
"""
# 同一个作用域里同名函数声明两遍（后一份胜出 ⇒ 改前一份不生效）。
SELFTEST_DUP_DECL = """
function fmtSecDup(s) {
  return String(s);
}
function fmtSecDup(s) {
  return String(s) + "!";
}
function callerDup() { return fmtSecDup(3); }
"""
# 负对照：两个**不同名**的同类函数，绝不能报重复。
SELFTEST_NO_DUP = """
function fmtSecA(s) { return String(s); }
function fmtSecB(s) { return String(s) + "!"; }
function callerNoDup() { return fmtSecA(3) + fmtSecB(4); }
"""
# 负对照：`for (...)` 头里的同名声明是**循环自己的**绑定，几个都不算重复。
SELFTEST_FOR_HEAD = """
function tickAll(xs) {
  for (const it of xs) { work(it); }
  for (const it of xs) { work(it); }
  for (let k = 0; k < 3; k++) { work(k); }
}
"""


def selftest():
    """五个内置样例：同一个 bug 的"错写法 / 改对 / 真没声明"，
    外加"同作用域重复声明"的正反两面。

    样例必须配一份 prelude（把 esc/box/color 这类辅助名字声明上），
    否则它们自己就是"未声明"，判据会被噪声淹没 —— 这也正是自测的价值：
    第一版忘了 prelude，三个用例全红，一眼就看出判据没在量它该量的东西。
    """
    print("== 自测：内置样例，判据必须分别红/绿（含负对照） ==")
    import tempfile
    d = tempfile.mkdtemp(prefix="vs_static_")
    pre = os.path.join(d, "prelude.js")
    with open(pre, "w", encoding="utf-8") as fh:
        fh.write(SELFTEST_PRELUDE)

    cases = [
        ("块作用域泄漏（就是第 5 次那个 meta）", SELFTEST_FIX_LEAK, 1, "作用域用错"),
        ("同一段代码改对了", SELFTEST_FIX_OK, 0, None),
        ("真的没声明过", SELFTEST_FIX_UNDECLARED, 1, "未声明"),
        ("同作用域同名函数声明两遍（fmtSec 那次）", SELFTEST_DUP_DECL, 1, "重复声明"),
        ("负对照：两个不同名的同类函数", SELFTEST_NO_DUP, 0, None),
        ("负对照：for (...) 头里的同名声明", SELFTEST_FOR_HEAD, 0, None),
    ]
    ok = True
    for title, src, want_n, want_kind in cases:
        tmp = os.path.join(d, "case.js")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(src)
        # 口径必须和 main() 一致：同名多次出现算**一项**（compress 去重），
        # 否则自测的期望值跟用户看到的数字对不上。
        seen = compress(run([pre, tmp]))
        got_n = len(seen)
        got_kind = next(iter(seen))[1] if seen else None
        got_name = next(iter(seen))[2] if seen else None
        got_line = next(iter(seen))[0] if seen else None
        good = (got_n == want_n) and (want_kind is None or got_kind == want_kind)
        ok = ok and good
        print("  %s %-32s 期望 %d 项/%-10s 实际 %d 项/%-10s %s"
              % ("PASS" if good else "FAIL", title, want_n, want_kind or "-",
                 got_n, got_kind or "-",
                 ("| %s:%s" % (got_name, got_line)) if seen else ""))
    try:
        os.remove(pre)
        os.remove(os.path.join(d, "case.js"))
        os.rmdir(d)
    except OSError:
        pass
    return ok


def scripts_not_scanned():
    """index.html 里 `<script src="/static/xxx.js">` 引入、却不在 FILES 里的脚本。

    新加一个脚本（比如 viz.js）而忘了把它加进扫描名单，症状是**假阳性**：
    app.js 调它提供的函数被判"未声明"，看起来像页面坏了，其实是门禁名单漏了。
    """
    html = os.path.join(HERE, "web", "index.html")
    if not os.path.isfile(html):
        return []
    src = open(html, encoding="utf-8").read()
    refs = re.findall(r'<script[^>]+src\s*=\s*"([^"]+)"', src)
    scanned = set(os.path.basename(f) for f in FILES)
    out = []
    for r in refs:
        if r.startswith("http://") or r.startswith("https://") or r.startswith("//"):
            continue                       # CDN 上的，本来就不该扫
        b = os.path.basename(r.split("?")[0])
        if b.endswith(".js") and b not in scanned:
            out.append(b)
    return out


def main():
    args = [a for a in sys.argv[1:]]
    if args and args[0] == "--selftest":
        ok = selftest()
        print("ALLDONE")
        sys.exit(0 if ok else 1)

    files = args or FILES
    print("== 前端静态自检：读了但**看不到**的名字（按作用域） ==")
    print("（扫描 %s）" % ", ".join(files))

    # ★ 名单自检：index.html 里引入的每一个**本地**脚本都得在扫描名单里。
    #   漏一个 ⇒ 那个文件提供的全局函数会被当成"未声明"报假阳性，
    #   而真 bug 也照样逃过检查（两面都输）。这条只在默认名单下跑。
    if not args:
        miss = scripts_not_scanned()
        if miss:
            print("  ! 这些脚本在 index.html 里被引入，却没进扫描名单：" + ", ".join(miss))
        ok_list = not miss
    else:
        ok_list = True

    ok_self = selftest()
    print("")

    found = run(files)
    seen = compress(found)
    if not seen:
        print("  没有发现可疑标识符")
    for (f, kind, name), (line, cnt) in sorted(seen.items()):
        print("  ! %s:%d  %s  [%s]  （出现 %d 次）" % (f, line, name, kind, cnt))

    print("ALLDONE")
    print("可疑项：%d" % len(seen))
    # 汇总行（供 `_run_regression.sh snapshot` 统计判据数）。
    #   这一关原来只打印"可疑项：N"，没有"通过 N 项，失败 M 项"那一行 ——
    #   结果门禁快照里它永远是 0 条判据，看着像这一关什么都没验。
    #   口径：6 条内置自测 + 1 条名单自检 + 1 条"没有可疑标识符"。
    print("通过 %d 项，失败 %d 项"
          % ((6 if ok_self else 0) + (1 if ok_list else 0) + (1 if not seen else 0),
             (0 if ok_self else 6) + (0 if ok_list else 1) + (len(seen) if seen else 0)))
    bad = len(seen) > 0 or not ok_self or not ok_list
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

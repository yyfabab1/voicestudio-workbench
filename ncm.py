r"""
.ncm 解密：把网易云音乐客户端**已经下载到你这台机器上**的 .ncm
还原成标准 mp3 / flac，让它们能进曲库、能被翻唱。

★★ 边界（写死在第一行，别动）：本模块只做**本地已有文件的格式转换**。
它不下载、不搜索、不抓取网易云曲库，不碰任何账号体系，也不提供任何
"绕过付费"的能力 —— 硬盘上没有 .ncm，这个模块就什么都不做。
转换出来的文件仍然是你自己那一份，跟你在客户端里听的是同一段音频。

------------------------------------------------------------------ 格式
布局按 ncmdump（taurusxin/ncmdump，MIT）公布的逆向结果实现，并在本机
3 个真实文件上逐字节验过（林俊杰 - 曹操 / Côme / Welcome Home）。

有三处跟网上流传的 Python 版本不一样，**抄错任何一处都会全乱**：

  1) meta 的 AES key 是 16 字节 `#14ljk_!\]&0U<'(`
     （hex 23 31 34 6c 6a 6b 5f 21 5c 5d 26 30 55 3c 27 28）。
     网上常见版本写成 `#14ljk_!\]&0U<'` —— 少/错位一个字节，
     症状是**元数据永远解成乱码**，而音频 key 看起来是好的，
     于是很容易误判成"网易云改格式了"（我在这上面绕了很久）。

  2) meta 之后要跳 **5 字节**（crc32 + image version），不是 1 字节。
     少跳 4 字节 ⇒ 音频整体错位 ⇒ 解出来是噪音，且 ffprobe 量不出时长。

  3) 封面区是 cover_frame_len(4) + image_len(4) + 图片 + 剩余填充，
     音频起点 = 跳过 5 字节 + 4 + 4 + cover_frame_len 之后，
     **不是** cover_frame_len 之后（同样差 4 字节）。

字节布局：
    CTENFDAM(8) | 2 | key_len(4) | key(AES/^0x64) | meta_len(4)
    | meta(^0x63 → "163 key(Don't modify):" + base64 → AES → "music:" + JSON)
    | crc32(4) + version(1) | cover_frame_len(4) | image_len(4) | 图片 + 填充
    | 音频数据（RC4-like key box 逐字节异或）

------------------------------------------------------------------ 依赖
**零依赖**：AES 优先用 pycryptodome / cryptography（快），没有就用本文件
自带的 AES-128 实现（纯 Python，用 FIPS-197 向量验过，见 verify_ncm.py）。
理由：这个模块要能在**任意一个** Python 解释器里被 import（server 跑在一个
解释器、门禁跑在另一个），不能赌哪一个装了 Crypto。

音频解密是 RC4-like 流密码，keystream 只跟 (i & 0xff) 有关 ⇒ 周期 256，
可以先算 256 字节再按块异或（有 numpy 时向量化）。**不要**写成逐字节
Python 循环：一首 10MB 的歌要跑十几秒，而向量化是毫秒级。
"""

import base64
import json
import os
import struct

MAGIC = b"CTENFDAM"
CORE_KEY = b"hzHRAmso5kInbaxW"
META_KEY = bytes([0x23, 0x31, 0x34, 0x6C, 0x6A, 0x6B, 0x5F, 0x21,
                  0x5C, 0x5D, 0x26, 0x30, 0x55, 0x3C, 0x27, 0x28])
KEY_PREFIX = b"neteasecloudmusic"
META_PREFIX = b"163 key(Don't modify):"
MUSIC_PREFIX = b"music:"
EXT = ".ncm"


class NcmError(Exception):
    pass


# ============================================================ AES-128(ECB)
# 优先用现成库；没有就用本文件的纯 Python 实现（见文件末尾 _AES128）。

def _lib_ecb_decrypt(key, data):
    try:
        from Crypto.Cipher import AES  # pycryptodome
        return AES.new(key, AES.MODE_ECB).decrypt(data)
    except Exception:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
        d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return d.update(data) + d.finalize()
    except Exception:
        return None


_NP = None


def _numpy():
    """取 numpy（没有就返回 None，走逐字节回退）。

    单独抽成函数是为了**可测**：门禁要把两条路径（向量化 / 回退）的产物
    逐字节对一遍 —— 之前回退分支少算一个偏移，numpy 环境下完全看不出来。
    """
    global _NP
    if _NP is None:
        try:
            import numpy as _m
            _NP = _m
        except Exception:
            _NP = False
    return _NP or None


def _gf_mul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _init_sbox():
    """按 Wikipedia 给出的生成式算 S-box（不手抄 256 个常数，抄错无从发现）。"""
    sbox = [0] * 256
    p = q = 1
    while True:
        # p *= 3 (GF(2^8))
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        # q /= 3 (GF(2^8))
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = (q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6))
             ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))) & 0xFF
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _init_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_MIX = ((14, 11, 13, 9), (9, 14, 11, 13),
            (13, 9, 14, 11), (11, 13, 9, 14))


class _AES128(object):
    """只做解密，够用就行（ncm 里只有两小段 AES 数据：key 和 meta）。"""

    def __init__(self, key):
        if len(key) != 16:
            raise NcmError("AES-128 需要 16 字节密钥")
        w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
        rcon = 1
        for i in range(4, 44):
            t = list(w[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [_SBOX[x] for x in t]
                t[0] ^= rcon
                rcon = (((rcon << 1) ^ 0x1B) & 0xFF) if rcon & 0x80 else rcon << 1
            w.append([w[i - 4][j] ^ t[j] for j in range(4)])
        self.w = w

    def _add_round_key(self, s, rnd):
        w = self.w
        for c in range(4):
            base = c * 4
            wk = w[rnd * 4 + c]
            s[base] ^= wk[0]
            s[base + 1] ^= wk[1]
            s[base + 2] ^= wk[2]
            s[base + 3] ^= wk[3]

    def decrypt_block(self, block):
        s = list(block)
        # 字节 i → row i%4, col i//4
        self._add_round_key(s, 10)
        for rnd in range(9, 0, -1):
            # InvShiftRows：第 r 行循环右移 r
            old = list(s)
            for r in range(1, 4):
                for c in range(4):
                    s[r + 4 * c] = old[r + 4 * ((c - r) % 4)]
            s = [_INV_SBOX[x] for x in s]
            self._add_round_key(s, rnd)
            old = s
            s = [0] * 16
            for c in range(4):
                col = old[c * 4:c * 4 + 4]
                for r in range(4):
                    m = _INV_MIX[r]
                    s[c * 4 + r] = (_gf_mul(col[0], m[0]) ^ _gf_mul(col[1], m[1])
                                    ^ _gf_mul(col[2], m[2]) ^ _gf_mul(col[3], m[3]))
        old = list(s)
        for r in range(1, 4):
            for c in range(4):
                s[r + 4 * c] = old[r + 4 * ((c - r) % 4)]
        s = [_INV_SBOX[x] for x in s]
        self._add_round_key(s, 0)
        return bytes(s)


def _unpad(data):
    """PKCS#7。末尾字节不像合法 padding 就**原样返回**（别自作聪明截断）。"""
    if not data:
        return data
    p = data[-1]
    if 1 <= p <= 16 and len(data) >= p and all(x == p for x in data[-p:]):
        return data[:-p]
    return data


def _ecb_decrypt(key, data):
    """AES-128-ECB 解密 + PKCS#7 去垫。长度不是 16 的倍数直接报错。"""
    if not data or len(data) % 16:
        raise NcmError("AES 数据长度不是 16 的倍数（%d 字节）" % len(data))
    raw = _lib_ecb_decrypt(key, data)
    if raw is None:
        aes = _AES128(key)
        raw = b"".join(aes.decrypt_block(data[i:i + 16])
                       for i in range(0, len(data), 16))
    return _unpad(raw)


def aes_self_test():
    """FIPS-197 的 AES-128 向量。门禁会跑它；返回 True 表示本模块 AES 可用。"""
    key = bytes(range(16))
    pt = bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
                0x88, 0x99, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])
    want = bytes([0x69, 0xC4, 0xE0, 0xD8, 0x6A, 0x7B, 0x04, 0x30,
                  0xD8, 0xCD, 0xB7, 0x80, 0x70, 0xB4, 0xC5, 0x5A])
    aes = _AES128(key)
    return aes.decrypt_block(want) == pt


# ============================================================ ncm 解析

def is_ncm(path):
    try:
        with open(path, "rb") as f:
            return f.read(8) == MAGIC
    except Exception:
        return False


def read_header(path):
    """读 ncm 头：解出音频 key 和元数据，**不动音频数据**（很快）。

    返回 dict：{key, meta, audio_start, audio_len, size, has_meta}
    元数据解不出来不报错（meta 为空） —— 标题可以从文件名来，
    但音频 key 解不出来必须报错，那种文件是真解不了。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise NcmError("不是 ncm 文件（文件头对不上）")
        f.read(2)
        raw = f.read(4)
        if len(raw) < 4:
            raise NcmError("文件被截断（读不到密钥长度）")
        klen = struct.unpack("<I", raw)[0]
        if not (0 < klen <= 8192):
            raise NcmError("密钥长度不对（%d），文件可能已损坏" % klen)
        kdata = f.read(klen)
        if len(kdata) != klen:
            raise NcmError("文件被截断（密钥没读全）")
        try:
            plain = _ecb_decrypt(CORE_KEY, bytes(b ^ 0x64 for b in kdata))
        except Exception as e:
            raise NcmError("密钥解不开：%s" % e)
        if not plain.startswith(KEY_PREFIX):
            raise NcmError("密钥解出来不是 netease 的格式（文件损坏或格式已变）")
        key = plain[len(KEY_PREFIX):]
        if not key:
            raise NcmError("解出来的音频密钥是空的")

        meta = {}
        raw = f.read(4)
        if len(raw) < 4:
            raise NcmError("文件被截断（读不到元数据长度）")
        mlen = struct.unpack("<I", raw)[0]
        if mlen > 0:
            mdata = f.read(mlen)
            if len(mdata) != mlen:
                raise NcmError("文件被截断（元数据没读全）")
            try:
                blob = bytes(b ^ 0x63 for b in mdata)
                if not blob.startswith(META_PREFIX):
                    raise NcmError("元数据前缀对不上")
                dec = base64.b64decode(blob[len(META_PREFIX):])
                plain = _ecb_decrypt(META_KEY, dec)
                txt = plain.decode("utf-8")
                if txt.startswith("music:"):
                    txt = txt[len(MUSIC_PREFIX):]
                meta = json.loads(txt) or {}
            except Exception:
                # 元数据只是标签（歌名/歌手/专辑），不是能不能解的必要条件。
                # 解不出来就降级：**不报错**，调用方用文件名兜底。
                meta = {}

        f.read(5)  # crc32(4) + image version(1)
        raw = f.read(4)
        if len(raw) < 4:
            raise NcmError("文件被截断（读不到封面长度）")
        cover_frame_len = struct.unpack("<I", raw)[0]
        f.read(4)  # image_len
        audio_start = f.tell() + max(0, cover_frame_len)
        if audio_start > size:
            raise NcmError("封面长度（%d 字节）超过了文件本身，文件已损坏"
                           % cover_frame_len)
    artists = meta.get("artist") or []
    if artists and isinstance(artists[0], (list, tuple)):
        artists = [a[0] for a in artists if a]
    return {
        "key": key,
        "meta": meta,
        "title": (meta.get("musicName") or "").strip(),
        "artist": "/".join(str(a) for a in artists).strip(),
        "album": (meta.get("album") or "").strip(),
        "fmt": (meta.get("format") or "").strip().lower(),
        "bitrate": meta.get("bitrate") or 0,
        "dur_ms": meta.get("duration") or 0,
        "audio_start": audio_start,
        "audio_len": size - audio_start,
        "size": size,
        "has_meta": bool(meta),
    }


def _keystream(key):
    """key box → 256 字节密钥流。keystream[i] 只跟 (i & 0xff) 有关。"""
    box = bytearray(range(256))
    last = 0
    off = 0
    klen = len(key)
    for i in range(256):
        swap = box[i]
        c = (swap + last + key[off]) & 0xFF
        off = 0 if off + 1 >= klen else off + 1
        box[i] = box[c]
        box[c] = swap
        last = c
    out = bytearray(256)
    for i in range(256):
        j = (i + 1) & 0xFF
        out[i] = box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]
    return bytes(out)


def _safe_name(s):
    s = (s or "").strip()
    for ch in ('\\', '/', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t'):
        s = s.replace(ch, "_")
    return s.strip().strip(".")[:80] or "unknown"


def probe(path):
    """只看一眼：这是不是 ncm、里面大概是什么歌。不解密、不写盘。"""
    out = {"ok": False, "is_ncm": False, "path": path, "title": "",
           "artist": "", "album": "", "dur": 0.0, "fmt": "", "size": 0,
           "err": ""}
    try:
        out["size"] = os.path.getsize(path)
        if not is_ncm(path):
            out["err"] = "不是 ncm 文件"
            return out
        h = read_header(path)
        out.update({"ok": True, "is_ncm": True, "title": h["title"],
                    "artist": h["artist"], "album": h["album"],
                    "fmt": h["fmt"], "dur": round((h["dur_ms"] or 0) / 1000.0, 1),
                    "audio_len": h["audio_len"]})
    except Exception as e:
        out["err"] = str(e)[:160]
    return out


def decrypt(src, out_dir, out_name=None, keep_meta=True):
    """把一个 .ncm 解成 mp3/flac，落到 out_dir，返回结果 dict。

    失败一律抛 NcmError（**绝不**产出半个文件：先写 .part 再改名）。
    """
    if not is_ncm(src):
        raise NcmError("不是 ncm 文件：%s" % os.path.basename(src))
    h = read_header(src)
    title = h["title"] or _safe_name(os.path.splitext(os.path.basename(src))[0])
    artist = h["artist"]
    stem = out_name or (("%s - %s" % (artist, title)) if artist else title)

    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, _safe_name(stem) + ".part")
    ks = _keystream(h["key"])
    _np = _numpy()
    ks_arr = _np.frombuffer(ks, dtype=_np.uint8) if _np is not None else None

    written = 0
    head = b""
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        fin.seek(h["audio_start"])
        base = 0
        while True:
            chunk = fin.read(1 << 19)
            if not chunk:
                break
            if _np is not None:
                a = _np.frombuffer(chunk, dtype=_np.uint8)
                off = base & 0xFF
                rep = len(a) // 256 + 2
                t = _np.tile(ks_arr, rep)[off:off + len(a)]
                buf = (_np.bitwise_xor(a, t)).tobytes()
            else:
                # 没有 numpy 时的逐字节回退。注意 ks[i] **已经**是"第 i 个输出
                # 字节该异或的值"（+1 在 _keystream 里算过了），这里再 +1 就
                # 整体错位一个字节 —— 症状是解出来的文件魔数全是乱的。
                buf = bytearray(chunk)
                for i in range(len(buf)):
                    buf[i] ^= ks[(base + i) & 0xFF]
                buf = bytes(buf)
            if not head:
                head = buf[:8]
            fout.write(buf)
            base += len(chunk)
            written += len(chunk)
    if written <= 0:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise NcmError("里面没有音频数据（文件是空的或封面长度有问题）")

    if head[:3] == b"ID3":
        ext = ".mp3"
    elif head[:4] == b"fLaC":
        ext = ".flac"
    else:
        ext = ("." + h["fmt"]) if h["fmt"] in ("mp3", "flac", "m4a", "wav") else ".mp3"

    dst = os.path.join(out_dir, _safe_name(stem) + ext)
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(out_dir, _safe_name(stem) + " (%d)" % n + ext)
    os.replace(tmp, dst)
    # 曲库目录是从 stages/ 拼出来的（含 ..），登记进曲库的路径要规范化，
    # 否则同一首歌会因为两种写法被当成两条。
    dst = os.path.abspath(dst)

    return {"ok": True, "path": dst, "src": src, "title": title,
            "artist": artist, "album": h["album"] if keep_meta else "",
            "dur": round((h["dur_ms"] or 0) / 1000.0, 1),
            "bitrate": h["bitrate"], "ext": ext, "bytes": written,
            "has_meta": h["has_meta"]}


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:]]
    if args and args[0] == "--selftest":
        print("AES self test:", "PASS" if aes_self_test() else "FAIL")
        sys.exit(0 if aes_self_test() else 1)
    if len(args) < 2:
        print("用法: ncm.py <输出目录> <ncm文件...>   |   ncm.py --selftest")
        sys.exit(2)
    outd = args[0]
    for p in args[1:]:
        try:
            r = decrypt(p, outd)
            print("OK  %s → %s (%s, %.1fs)" % (os.path.basename(p), r["path"],
                                               r["ext"], r["dur"]))
        except Exception as e:
            print("FAIL %s —— %s" % (os.path.basename(p), e))

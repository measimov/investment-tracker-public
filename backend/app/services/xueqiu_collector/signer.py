"""xueqiu.com 请求签名 `md5__1038`——原 archiver 用 Node 跑 27KB 混淆 JS，这里是纯 Python 移植。

算法（从 `xueqiu_md5_1038.js` 解出，金样见 tests/fixtures/xueqiu_collector/signer_goldens.json，
由原 JS 在固定时间戳下一次性生成，测试不依赖 Node）：

1. 取 `protocol//host + pathname + search`（先剔除已有的 md5__1038 参数），
   做 `encodeURIComponent`；
2. 对编码后的每个字符做 int32 滚动哈希 `h = ((h << 7) - h + 398 + ord(c)) | 0`；
3. payload = `"{h}|0|{now_ms}|1"`，用 LZString `compressToBase64`（自定义字母表、**不补 `=`**）压缩；
4. 以 `md5__1038=<encodeURIComponent(sig)>` 追加到 URL（已有 query 用 `&`，否则 `?`）。

去掉 Node 同时去掉了原实现"每次签名重写同一个 runtime JS 文件"的并发竞争。
时钟可注入（`now_ms`），纯函数。
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

SIGN_PARAM = "md5__1038"
LZ_ALPHABET = "DGi0YA7BemWnQjCl4+bR3f8SKIF9tUz/xhr2oEOgPpac=61ZqwTudLkM5vHyNXsVJ"
HASH_SALT = 398
# encodeURIComponent 不转义的字符集（字母数字之外）
_URI_COMPONENT_SAFE = "-_.!~*'()"


def encode_uri_component(value: str) -> str:
    return quote(value, safe=_URI_COMPONENT_SAFE)


def _to_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def rolling_hash(text: str) -> int:
    h = 0
    for char in text:
        h = _to_int32((h << 7) - h + HASH_SALT + ord(char))
    return h


def lz_compress(uncompressed: str, bits_per_char: int, char_of: Callable[[int], str]) -> str:
    """lz-string `_compress` 的逐行移植（含 >=256 码位分支，签名 payload 只用 ASCII）。"""
    if not uncompressed:
        return ""
    dictionary: dict = {}
    to_create: dict = {}
    w = ""
    enlarge_in = 2
    dict_size = 3
    num_bits = 2
    data: List[str] = []
    data_val = 0
    data_position = 0

    def write_bits(count: int, value: int) -> None:
        nonlocal data_val, data_position
        for _ in range(count):
            data_val = (data_val << 1) | (value & 1)
            if data_position == bits_per_char - 1:
                data_position = 0
                data.append(char_of(data_val))
                data_val = 0
            else:
                data_position += 1
            value >>= 1

    def emit_w() -> None:
        nonlocal enlarge_in, num_bits
        if w in to_create:
            code = ord(w[0])
            if code < 256:
                write_bits(num_bits, 0)
                write_bits(8, code)
            else:
                write_bits(num_bits, 1)
                write_bits(16, code)
            enlarge_in -= 1
            if enlarge_in == 0:
                enlarge_in = 2**num_bits
                num_bits += 1
            del to_create[w]
        else:
            write_bits(num_bits, dictionary[w])
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 2**num_bits
            num_bits += 1

    for c in uncompressed:
        if c not in dictionary:
            dictionary[c] = dict_size
            dict_size += 1
            to_create[c] = True
        wc = w + c
        if wc in dictionary:
            w = wc
        else:
            emit_w()
            dictionary[wc] = dict_size
            dict_size += 1
            w = c

    if w != "":
        emit_w()

    write_bits(num_bits, 2)  # end of stream
    while True:  # flush
        data_val <<= 1
        if data_position == bits_per_char - 1:
            data.append(char_of(data_val))
            break
        data_position += 1
    return "".join(data)


def compress_to_base64(payload: str, *, pad: bool = False) -> str:
    """LZString `compressToBase64`（自定义字母表）。

    站点 JS 调用时带了"不补位"标志（`F.ua(V, true)`）：金样里签名一律没有尾部 `=`，
    标准 lz-string 按 len%4 补 `=`/`==`/`===` 的形态与站点不一致，故默认不补。
    """
    result = lz_compress(payload, 6, lambda index: LZ_ALPHABET[index])
    if not pad:
        return result
    return result + {0: "", 1: "===", 2: "==", 3: "="}[len(result) % 4]


def _strip_sign_param(query: str) -> str:
    if SIGN_PARAM not in query:
        return query
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != SIGN_PARAM]
    return urlencode(pairs)


def signature_for(url: str, *, now_ms: Optional[int] = None) -> str:
    parts = urlsplit(url)
    query = _strip_sign_param(parts.query)
    search = f"?{query}" if query else ""
    base = f"{parts.scheme}://{parts.netloc}{parts.path or '/'}{search}"
    stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    payload = f"{rolling_hash(encode_uri_component(base))}|0|{stamp}|1"
    return compress_to_base64(payload)


def sign_url(url: str, *, now_ms: Optional[int] = None) -> str:
    """返回带 `md5__1038` 参数的 URL。"""
    parts = urlsplit(url)
    query = _strip_sign_param(parts.query)
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    token = f"{SIGN_PARAM}={encode_uri_component(signature_for(clean, now_ms=now_ms))}"
    separator = "&" if query else "?"
    return f"{urlunsplit((parts.scheme, parts.netloc, parts.path, query, ''))}{separator}{token}"

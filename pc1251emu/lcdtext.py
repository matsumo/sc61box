"""液晶RAMを文字に読み戻す(テストと確認用)

液晶の5列ずつを、BASIC ROMの文字の形と照らし合わせる。

* PC-1251・PC-1245: 4504h(数字)からの数字と英字の形。
* PC-1261など: 20hからの文字コードの順に1字5バイト(空白が8045h)。左右が逆に並ぶ。
* PC-1350: 同じ形の表が808Ah(空白)から。7Fhの次がA0hで、80h〜9Fhはない。液晶は
  桁に分かれていないので、6列ずつを1字として読む(4行×25字)。

2行以上の液晶では、行の終わりに改行を入れる。
"""

_EXTRA = {
    bytes(5): " ",
}


def _table_1261(mem) -> dict[bytes, str]:
    """PC-1261などの文字の形の表。20h〜7BhはASCIIの順で、7Ch番目から半角カナ(A1h〜DFh)、
    C6h・C7h番目がπ・√"""
    t = dict(_EXTRA)
    base = 0x7FA5

    def glyph(index: int) -> bytes:
        return bytes(reversed(mem[base + 5 * index : base + 5 * index + 5]))

    for code in range(0x21, 0x7C):
        t.setdefault(glyph(code), "¥" if code == 0x5C else chr(code))
    for code in range(0xA1, 0xE0):
        t.setdefault(glyph(code - 0x25), bytes([code]).decode("cp932"))
    t.setdefault(glyph(0xC6), "π")
    t.setdefault(glyph(0xC7), "√")
    return t


def _table_1350(mem) -> dict[bytes, str]:
    """PC-1350の文字の形の表。ABh・ACh番目がπ・√"""
    t = dict(_EXTRA)
    base = 0x808A

    def glyph(index: int) -> bytes:
        return bytes(reversed(mem[base + 5 * index : base + 5 * index + 5]))

    for code in range(0x21, 0x80):
        t.setdefault(glyph(code - 0x20), "¥" if code == 0x5C else chr(code))
    for code in range(0xA1, 0xE0):
        t.setdefault(glyph(code - 0x40), bytes([code]).decode("cp932"))
    t.setdefault(glyph(0xAB), "π")
    t.setdefault(glyph(0xAC), "√")
    return t


_A = bytes.fromhex("7c1211127c")  # 「A」の形


def _table(mem) -> dict[bytes, str]:
    if mem[0x812F : 0x812F + 5] == _A:  # PC-1350のROM
        return _table_1350(mem)
    if mem[0x80EA : 0x80EA + 5] == _A:  # PC-1261などのROM
        return _table_1261(mem)
    t = dict(_EXTRA)
    base = 0x43C4
    for code in range(0x40, 0x4A):
        t[bytes(mem[base + 5 * code : base + 5 * code + 5])] = chr(ord("0") + code - 0x40)
    for code in range(0x51, 0x6B):
        t[bytes(mem[base + 5 * code : base + 5 * code + 5])] = chr(ord("A") + code - 0x51)
    return t


def glyphs(mem) -> dict[str, bytes]:
    """ROMの文字の形を、字から引けるようにする。{字: 5列(1バイトが縦の1列、ビット0が上)}。
    PC-1251・PC-1245は11h〜1Fhの形が4469hから、30h〜6Ahの形が44B4hから並ぶ"""
    if bytes(mem[0x812F:0x8134]) == _A or bytes(mem[0x80EA:0x80EF]) == _A:
        out: dict[str, bytes] = {}
        for g, ch in _table(mem).items():
            out.setdefault(ch, g)
        return out
    from .basictext import CHARS

    out = {" ": bytes(5)}
    for code, ch in CHARS.items():
        if len(ch) == 1 and (0x11 <= code < 0x20 or 0x30 <= code < 0x6B):
            at = (0x4414 if code < 0x20 else 0x43C4) + 5 * code
            out.setdefault(ch, bytes(mem[at : at + 5]))
    return out


def lcd_text(machine, unknown: str = "?") -> str:
    t = getattr(machine, "_glyphs", None)
    if t is None:
        t = machine._glyphs = _table(machine.mem)
    cols = machine.columns()
    if getattr(machine.model, "family", "") == "1350":
        rows = []
        for band in range(4):
            row = cols[band * 150 : (band + 1) * 150]
            rows.append("".join(t.get(bytes(row[i : i + 5]), unknown) for i in range(0, 150, 6)))
        return "\n".join(rows)
    text = "".join(t.get(bytes(cols[i : i + 5]), unknown) for i in range(0, len(cols), 5))
    lines = getattr(machine.model, "lines", 1)
    if lines > 1:
        n = len(text) // lines
        text = "\n".join(text[i * n : (i + 1) * n] for i in range(lines))
    return text


def learn(machine, text: str) -> None:
    """表示中の文字列から、記号のパターンを覚える(テスト用)"""
    t = getattr(machine, "_glyphs", None) or _table(machine.mem)
    cols = machine.columns()
    for i, ch in enumerate(text[: len(cols) // 5]):
        t.setdefault(bytes(cols[5 * i : 5 * i + 5]), ch)
    machine._glyphs = t

"""プリンタ(PC-1251・PC-1245はCE-125、PC-1261・PC-1350はCE-126Pにあたるもの)

本体は11ピンの端子から、1バイトを下位のビットから1ビットずつ送る。データの線にビットを
出してから合図の線を上げ、プリンタが応答の線(INBのビット6)を動かすのを待つ。
プリンタはこの合図の立ち上がりでデータの線を読む。最初の1バイトは命令(LPRINTなど)で、
そのあとに字が続く。字はおおむねASCIIとJIS X 0201のカナで、0Dで改行する。

紙の幅や印字の遅さ、紙送りのキーは再現していない。
"""

# プリンタの字の表のうち、ASCIIと違うところ(PC-1251・PC-1245・PC-1261と、PC-1350)。
# PC-1350はゼロを斜線つきのF0で、英字のOを30で、πを19で、√を18で送る
# (BASIC ROMのA4E4〜A546で置き換えている)
CHARS_1251 = {0x5B: "√", 0x5C: "¥", 0x5D: "π"}
CHARS_1350 = {0x30: "O", 0xF0: "0", 0x19: "π", 0x18: "√", 0x5C: "¥", 0x0B: "¥", 0x9F: "~"}


def char(code: int, table: dict[int, str]) -> str:
    """プリンタに届いたバイトを字にする。表にない制御の字は空、わからないものは「?」"""
    if code in table:
        return table[code]
    if code < 0x20:
        return ""
    if code < 0x7F:
        return chr(code)
    if 0xA1 <= code < 0xE0:
        return bytes([code]).decode("cp932")
    return "?"


class Printer:
    def __init__(self, table: dict[int, str] | None = None) -> None:
        self.table = CHARS_1251 if table is None else table
        self.lines: list[str] = []  # 印字し終わった行(行末の空白は落とす)
        self.line = ""  # 印字している途中の行
        self.count = 0  # 印字し終わった行の数(消したあとも数える)
        self._bits = 0
        self._n = 0

    def restart(self) -> None:
        """1バイトの途中で送り方が変わったときに、読みかけのビットを捨てる"""
        self._bits = self._n = 0

    def bit(self, value: int, command: bool) -> None:
        """合図の立ち上がりで、データの線のビットを読む。commandなら命令のバイト"""
        self._bits |= (value & 1) << self._n
        self._n += 1
        if self._n == 8:
            code = self._bits
            self.restart()
            if not command:
                self.put(code)

    def put(self, code: int) -> None:
        if code == 0x0D:
            self.lines.append(self.line.rstrip())
            self.line = ""
            self.count += 1
        else:
            self.line += char(code, self.table)

    def text(self) -> str:
        """印字したものを1つの文字列にする(途中の行も含める)"""
        rows = self.lines + ([self.line.rstrip()] if self.line.strip() else [])
        return "".join(r + "\n" for r in rows)

    def clear(self) -> None:
        self.lines = []
        self.line = ""

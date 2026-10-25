"""マシン語の作業欄(ROMなしで、マシン語を打ち込み、実行し、1命令ずつ調べる)

ROMのファイルがなくても、CPU、液晶、キー、ブザー、タイマはエミュレータが動かせる。
BASICとROMのルーチンの代わりに、この作業欄が次のことを受け持つ。

- 16進ダンプの打ち込み(番地、8バイト、チェックサムの行。本のダンプリストと同じ形)
- 打ち込んだマシン語の逆アセンブル
- 実行(戻り先を積んでから飛ぶ。RTNで戻ると終わる)、一時停止、1命令ずつの実行、止める所
- レジスタと内部RAMの表示(前に止まったときから変わったところに色をつける)

Benchは画面を持たない部分で、試験から直接使える。描くのはBenchView。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pygame

from . import codemap, disasm, norom, romsyms
from .i18n import language, tr
from .overlay import load_help_font, wrap_text

SENTINEL = 0x0000  # 実行を始めるときに積む戻り先。ここへ戻ったら終わり
STACK_TOP = 0x5C  # 実行を始めるときのR(内部RAMの5C〜5Fは入出力のポートの値)
ROW = 8  # ダンプの1行のバイト数(本のダンプリストと同じ)


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


@dataclass
class PasteResult:
    written: int = 0  # 書いたバイト数
    first: int | None = None  # 最初の番地
    bad_sums: list[int] = field(default_factory=list)  # チェックサムが合わなかった行の番地
    skipped: int = 0  # 読めなかった行の数
    outside: int = 0  # RAMの外で書けなかったバイト数


@dataclass
class Edit:
    """取り消せる書き込み1回分(1バイトの打ち込みか、1回の貼り付け)。書く前と書いたあとの
    メモリを比べて、変わったところだけを持つ。番地はメモリの実体の番地(鏡の番地に書いても、
    同じRAMは1つにまとまる)。before・afterは書く前と書いたあとのカーソルと開始番地"""

    # 知らせに出す名前(「C003番地の打ち込み」など)。言語を切り替えても出すときの言語に
    # なるように、名前を作る関数を持つ
    what: Callable[[], str]
    mem: dict[int, tuple[int, int]] = field(default_factory=dict)  # 実体の番地: (前, 後)
    marks: dict[int, tuple[int, int]] = field(default_factory=dict)  # 書いた印: (前, 後)
    before: tuple = ()
    after: tuple = ()
    at: int = 0  # 打ち込んだ番地(打ち込みのとき)
    partial: bool = False  # 上の桁だけを打った打ち込み(続けて下の桁を打てば同じ1回にする)


UNDO_MAX = 500  # 取り消せる回数


HEX4 = re.compile(r"[0-9A-Fa-f]{4}:?")
HEXS = re.compile(r"(?:[0-9A-Fa-f]{2})+")


def parse_dump(text: str) -> tuple[list[tuple[int | None, bytes, int | None]], int]:
    """貼り付けた文字を(番地, バイト, チェックサム)の行にする。番地がNoneの行は前の行の続き。

    読める形
    - 本のダンプリスト: 「C000 027F10F800523700 12」(続きの行は番地なし)
    - エミュレータの.hex: 「C000 02 7F 10 F8 ; 注釈」
    """
    rows: list[tuple[int | None, bytes, int | None]] = []
    skipped = 0
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        toks = line.split()
        addr = None
        if len(toks) >= 2 and HEX4.fullmatch(toks[0]):
            addr = int(toks[0][:4], 16)
            toks = toks[1:]
        if not toks or not all(HEXS.fullmatch(t) for t in toks):
            skipped += 1
            continue
        total = None
        if len(toks) >= 2 and len(toks[0]) > 2 and len(toks[-1]) == 2:
            total = int(toks[-1], 16)  # 「16字 チェックサム」の形
            toks = toks[:-1]
        rows.append((addr, bytes.fromhex("".join(toks)), total))
    return rows, skipped


NUM = r"(?:&[0-9A-Fa-f]{1,4}|\d+)"
POKE = re.compile(rf"POKE\s*({NUM}(?:\s*,\s*{NUM})+)$", re.IGNORECASE)
CALL = re.compile(r"CALL\s*&([0-9A-Fa-f]{1,4})", re.IGNORECASE)


def _num(t: str) -> int:
    t = t.strip()
    return int(t[1:], 16) if t.startswith("&") else int(t)


def basic_hints(text: str) -> tuple[list[tuple[int, bytes]], int | None]:
    """呼び出し用のBASICから、ROMなしでも使えるものを拾う。(数だけを書くPOKE、最後のCALLの
    番地)。IFのあとの文は、条件しだいなので拾わない。BASICそのものは実行しない"""
    pokes: list[tuple[int, bytes]] = []
    start = None
    for line in text.splitlines():
        body = re.sub(r"^\s*\d+\s*", "", line)
        if '"' in body:
            body = re.sub(r'"[^"]*"', '""', body)  # 文字列の中の「:」で切らない
        for stmt in body.split(":"):
            stmt = stmt.strip()
            if re.match(r"IF\b", stmt, re.IGNORECASE):
                break
            hit = POKE.match(stmt)
            if hit:
                vals = [_num(v) for v in hit[1].split(",")]
                if all(0 <= v <= 0xFF for v in vals[1:]) and 0 <= vals[0] <= 0xFFFF:
                    pokes.append((vals[0], bytes(vals[1:])))
            for hit in CALL.finditer(stmt):
                start = int(hit[1], 16)
    return pokes, start


CALL_ANY = re.compile(rf"CALL\s*({NUM})", re.IGNORECASE)


def basic_calls(text: str) -> list[int]:
    """BASICのCALLの番地をすべて拾う(IFのあとも。逆アセンブルで命令の入口として使う)"""
    found = set()
    for hit in CALL_ANY.finditer(text):
        v = _num(hit[1])
        if 0 <= v <= 0xFFFF:
            found.add(v)
    return sorted(found)


MAP_EVERY = 0.3  # 実行中に、命令とデータの見分けを作り直す間隔(秒)


class Bench:
    """作業欄の状態と操作(画面を持たない)。

    ROMなしの本体では、作業欄からマシン語を実行する(編集中・実行中・一時停止中)。
    ROMのある本体では、本体はいつもROMで動いていて(実行中)、マシン語はBASICのCALLで呼ぶ。
    作業欄は、止める所で止めたり、一時停止して1命令ずつ進めたりする道具になる。ROMの中にも
    止める所を置け、1命令ずつ進められる"""

    def __init__(self, m, start: int | None = None) -> None:
        self.m = m
        self.rom = not getattr(m, "norom", False)  # ROMのある本体か
        norom.install(m)  # ROMの代わりの文字の形(ROMなしの本体のとき)
        lo, hi = m.model.ram
        self.ram = (lo, hi)
        if start is None:
            start = 0xC000 if lo <= 0xC000 < hi else lo
        self.start = start  # 実行を始める番地
        # 開始番地をどう決めたか。""はまだ決めていない(最初に打った番地にする)、"auto"は
        # 貼り付けやプログラムの読み込みで決めた(次の貼り付けでも変わる)、"user"は自分で
        # 決めた(カーソルの移動、打ち込み、貼り付けでは変わらない)
        self.start_by = ""
        self.start_buf: str | None = None  # 開始番地の欄に打っている途中の字(打っていなければNone)
        # edit(編集中)、run(実行中)、pause(一時停止中)。ROMのある本体はrunから始まり、
        # editにはならない
        self.state = "run" if self.rom else "edit"
        self.reason = ""  # 止まった理由など、作業欄の下に出す知らせ
        self.breaks: set[int] = set()
        self.temp_stop: int | None = None  # 呼び出しを飛び越すときの止める所
        self.temp_r = 0  # 呼び出しを飛び越す前のR(戻ってきたら、Rはこれと同じ)
        # 次に実行を進めるときに、止めずに1命令だけ実行する番地(再帰の内側の戻り先)
        self.pass_pc: int | None = None
        self.clear_lcd = True  # 実行の前に液晶を消して表示をつける
        # ダンプのカーソル。field "addr"は行の番地の4桁(nibが0〜3)、"data"はバイト(nibが0か1)
        self.row = start  # カーソルのある行の番地
        self.col = 0  # 行の中のバイトの位置(0〜7。8は行の終わり)
        self.nib = 0
        self.field = "addr"
        self.addr_done = False  # 番地の4桁を打ち終えた直後(続くSpaceは読み飛ばす)
        self.top_rows = 0  # カーソルの行が、表示の何行目にあるか
        self.snap: bytes = bytes(m.mem)  # 実行を始めたときのメモリ(変わったところに色をつける)
        self.changed: set[int] = set()
        self.c0 = 0  # 実行を始めたときのサイクル
        self.prev_regs = bytes(m.cpu.ram)  # 前に止まったときの内部RAM
        self.prev_ptr = self.ptrs()
        self.ran = False  # 1度でも実行したか(レジスタの色づけに使う)
        self._guard: bytearray | None = None
        # 作業欄から書いた番地(打ち込み、貼り付け、ファイルの読み込み)。書き出す範囲に使う。
        # 0のバイトも書いたものとして数える(命令やデータの0、置き場所をあけるための0)
        self.written = bytearray(0x10000)
        # 書き込みの取り消しとやり直し。プログラムの読み込みと状態の復元で空にする
        self.undos: list[Edit] = []
        self.redos: list[Edit] = []
        # 命令とデータの見分けに使うもの。BASICのCALLの番地、実行した番地(命令を読んだPC)、
        # 自分で決めた指定(番地: "data"か"code"。その番地からデータか命令)
        self.calls: list[int] = []
        self.executed = bytearray(0x10000)
        self.marks: dict[int, str] = {}
        # 読み込んだプログラムの# data: の範囲。ダンプを書くと、その範囲の指定を消してから
        # 置き直す(ROMのある本体では、ダンプは読み込みのあとで書く)
        self.prog_data: list[tuple[int, int | None]] = []
        self._map: codemap.CodeMap | None = None
        self._map_key: tuple = ()
        self._map_at = 0.0

    # ---- メモリ ----
    def read(self, a: int) -> int:
        return self.m.read(a & 0xFFFF)

    def blocked(self, pc: int) -> str:
        """番地pcの命令を実行できなければ、その理由(できれば空)。RAMの外は、まねられる
        ROMのルーチンの入口のほかは実行できない(ROMなしではROMの中身がない)"""
        if self.rom or self.in_ram(pc) or pc in norom.ROUTINES:
            return ""
        return self._outside(pc)

    def in_ram(self, a: int) -> bool:
        lo, hi = self.ram
        mirror = self.m.model.mirror
        return lo <= a < hi or bool(mirror and mirror[0] <= a < mirror[1])

    def write(self, a: int, v: int) -> bool:
        """番地aにvを書き、書いた印をつける"""
        a &= 0xFFFF
        if not self.m.writable(a):
            return False
        self.m.write(a, v & 0xFF)
        at = self._mark_at(a)
        self.written[at] = 1
        self._forget_run(at)  # 書き換えた番地で前に実行した命令は、もう同じではない
        return True

    def _forget_run(self, at: int) -> None:
        """RAMの番地atで実行した印を消す。鏡の番地から実行した印(CPUは鏡の番地で覚える)も
        消す"""
        self.executed[at] = 0
        mirror = self.m.model.mirror
        if mirror and mirror[0] <= at - mirror[2] < mirror[1]:
            self.executed[at - mirror[2]] = 0

    def _mark_at(self, a: int) -> int:
        """書いた印を置く番地。鏡の番地に書いたものは、RAMの番地に書いたものとして数える"""
        mirror = self.m.model.mirror
        if mirror and mirror[0] <= a < mirror[1]:
            return a + mirror[2]
        return a

    # ---- 取り消しとやり直し ----
    def _view(self) -> tuple:
        return (self.row, self.col, self.nib, self.field, self.top_rows, self.start, self.start_by)

    def _set_view(self, view: tuple) -> None:
        self.row, self.col, self.nib, self.field, self.top_rows, start, start_by = view
        self.addr_done = False
        if self.start_by != "user":  # 自分で決めた開始番地は、取り消しでは戻さない
            self.start, self.start_by = start, start_by

    def _begin(self, what: Callable[[], str], edit: Edit | None = None) -> Edit:
        """書き込みを始める。書く前のメモリと書いた印を控えておく(editがあれば、その続き)"""
        self._before = (bytes(self.m.mem), bytes(self.written))
        return edit if edit is not None else Edit(what, before=self._view())

    def _commit(self, edit: Edit) -> None:
        """書き終えた書き込みを、取り消せるものとして積む。前からの続きなら、書く前の値は
        最初のものを残す。何も変わらなければ積まない"""
        mem0, marks0 = self._before
        for now, old, diff in ((self.m.mem, mem0, edit.mem), (self.written, marks0, edit.marks)):
            for i in [i for i, (x, y) in enumerate(zip(old, now, strict=True)) if x != y]:
                first = diff[i][0] if i in diff else old[i]
                diff[i] = (first, now[i])
            for i in [i for i, (x, y) in diff.items() if x == y]:
                del diff[i]
        edit.after = self._view()
        if not edit.mem and not edit.marks:
            return
        self.undos.append(edit)
        del self.undos[:-UNDO_MAX]
        self.redos.clear()

    _before: tuple[bytes, bytes] = (b"", b"")

    def forget_edits(self) -> None:
        """取り消しとやり直しの履歴を捨てる(プログラムを読み込んだときなど)"""
        self.undos.clear()
        self.redos.clear()

    def _apply(self, edit: Edit, new: bool) -> None:
        for a, (old, val) in edit.mem.items():
            self.m.write(a, val if new else old)  # 実体の番地に書く
            self.changed.discard(a)
            self._forget_run(a)
        for a, (old, val) in edit.marks.items():
            self.written[a] = val if new else old
        self._set_view(edit.after if new else edit.before)

    def can_undo(self) -> bool:
        return bool(self.undos) and not (self.state == "run" and not self.rom)

    def can_redo(self) -> bool:
        return bool(self.redos) and not (self.state == "run" and not self.rom)

    def undo_note(self, redo: bool = False) -> tuple[str, bool]:
        """取り消し(redoならやり直し)の説明と、いまできるか。できなければ説明は理由"""
        stack = self.redos if redo else self.undos
        if self.can_redo() if redo else self.can_undo():
            return self.describe(stack[-1], not redo), True
        if stack:
            return (tr("実行中はやり直せません") if redo else tr("実行中は取り消せません")), False
        if redo:
            return tr("やり直せる書き込みはありません"), False
        return tr("取り消せる書き込みはありません"), False

    @staticmethod
    def describe(edit: Edit, undo: bool) -> str:
        """ボタンやメニューに出す説明(取り消すか、やり直すと何が起きるか)。1バイトの
        打ち込みなら値の変化を書く(「C003番地をABから00に戻す」)"""
        if edit.at and len(edit.mem) == 1:
            old, new = next(iter(edit.mem.values()))
            if undo:
                return tr("{addr:04X}番地を{new:02X}から{old:02X}に戻す",
                          addr=edit.at, new=new, old=old)  # fmt: skip
            return tr(
                "{addr:04X}番地を{old:02X}から{new:02X}にする", addr=edit.at, new=new, old=old
            )
        if undo:
            return tr("{what}を取り消す", what=edit.what())
        return tr("{what}をやり直す", what=edit.what())

    def undo(self) -> str:
        """直前の書き込みを取り消す。知らせの文を返す"""
        if self.state == "run" and not self.rom:
            return tr("実行中は取り消せません")
        if not self.undos:
            return tr("取り消せる書き込みはありません")
        edit = self.undos.pop()
        self._apply(edit, new=False)
        self.redos.append(edit)
        return tr("{what}を取り消しました", what=edit.what())

    def redo(self) -> str:
        """取り消した書き込みをやり直す。知らせの文を返す"""
        if self.state == "run" and not self.rom:
            return tr("実行中はやり直せません")
        if not self.redos:
            return tr("やり直せる書き込みはありません")
        edit = self.redos.pop()
        self._apply(edit, new=True)
        self.undos.append(edit)
        return tr("{what}をやり直しました", what=edit.what())

    def row_bytes(self, a: int) -> bytes:
        return bytes(self.read(a + i) for i in range(ROW))

    # ---- ダンプの打ち込み ----
    @property
    def cursor(self) -> int:
        return (self.row + min(self.col, ROW - 1)) & 0xFFFF

    def type_hex(self, v: int) -> str:
        """16進表記の1字(0〜15)を打つ。打てなかったら理由を返す"""
        if self.state == "run" and not self.rom:
            return tr("実行中は書き換えられません")
        self.addr_done = False
        if self.field == "addr":
            shift = 12 - 4 * self.nib
            self.row = (self.row & ~(0xF << shift) | v << shift) & 0xFFFF
            if not self.start_by:  # 開始番地をまだ決めていなければ、打った番地にする
                self.start = self.row
            self.nib += 1
            if self.nib == 4:
                self.field, self.col, self.nib = "data", 0, 0
                self.addr_done = True
            return ""
        if self.col >= ROW:
            return tr("行の終わりです。Enterで次の行へ")
        a = self.cursor
        old = self.read(a)
        new = (old & 0x0F | v << 4) if self.nib == 0 else (old & 0xF0 | v)
        if not self.m.writable(a):
            return tr("{addr:04X}番地はRAMの外なので書けません", addr=a)
        # 1バイトの2桁は、続けて打てば1回の打ち込みとして取り消す
        top = self.undos[-1] if self.undos else None
        if self.nib == 1 and top is not None and top.partial and top.at == a:
            edit = self._begin(top.what, self.undos.pop())
        else:
            edit = self._begin(lambda: tr("{addr:04X}番地の打ち込み", addr=a))
            edit.at = a
        edit.partial = self.nib == 0
        self.write(a, new)
        self.changed.discard(a)
        self.start_by = self.start_by or "auto"  # 書き始めたら、開始番地は打った番地で決まり
        if self.nib == 0:
            self.nib = 1
        else:
            self.nib = 0
            self.col += 1
        self._commit(edit)  # 取り消したあとのやり直しで、カーソルを打ったあとの所へ戻すため
        return ""

    def next_line(self) -> None:
        """Enter: 次の行の最初のバイトへ"""
        self.addr_done = False
        if self.field == "addr":
            self.field, self.col, self.nib = "data", 0, 0
            return
        self.move_rows(1)
        self.field, self.col, self.nib = "data", 0, 0

    def to_data(self) -> None:
        """Space: 番地の欄からバイトの欄へ。バイトの欄では次のバイトへ(番地を打ち終えた
        直後のSpaceは、本のダンプリストの区切りなので読み飛ばす)"""
        if self.addr_done:
            self.addr_done = False
            return
        if self.field == "addr":
            self.field, self.col, self.nib = "data", 0, 0
        elif self.col < ROW:
            self.col, self.nib = self.col + 1, 0

    def to_addr(self) -> None:
        """番地を打ち直す(番地の欄の最初の桁へ)"""
        self.addr_done = False
        self.field, self.nib = "addr", 0

    def move_rows(self, n: int, rows_shown: int = 8) -> None:
        self.row = (self.row + ROW * n) & 0xFFFF
        self.top_rows = min(max(self.top_rows + n, 0), max(0, rows_shown - 1))

    def page(self, n: int, rows: int) -> None:
        """表示をn画面(rows行ずつ)動かす。カーソルは画面の中の同じ所に残す"""
        self.addr_done = False
        self.row = (self.row + ROW * rows * n) & 0xFFFF

    def row_home(self) -> None:
        """行の最初のバイトへ。そこにいれば、番地の欄へ"""
        self.addr_done = False
        if self.field == "data" and (self.col or self.nib):
            self.col, self.nib = 0, 0
        else:
            self.field, self.nib = "addr", 0

    def row_end(self) -> None:
        """行の終わり(最後のバイトの後ろ)へ"""
        self.addr_done = False
        self.field, self.col, self.nib = "data", ROW, 0

    def go_end(self, rows_shown: int = 8) -> None:
        """開始番地から書いてあるところの終わりへ(行の頭は開始番地と同じ並び)"""
        ranges = self.export_ranges()
        end = next((hi for lo, hi in ranges if lo <= self.start < hi), self.start + 1)
        last = (end - 1) & 0xFFFF
        row = (last - (last - self.start) % ROW) & 0xFFFF
        self.go(row, rows_shown)
        self.col = (last - row) + 1
        self.top_rows = max(0, rows_shown - 3)

    def left(self, rows_shown: int = 8) -> None:
        """1字戻る。行の最初のバイトの上の桁からは、前の行の最後のバイトの下の桁へ"""
        self.addr_done = False
        if self.field == "addr":
            self.nib = max(0, self.nib - 1)
        elif self.col >= ROW:  # 行の終わり(最後のバイトの後ろ)からは最後のバイトへ
            self.col, self.nib = ROW - 1, 1
        elif self.nib == 1:
            self.nib = 0
        elif self.col > 0:
            self.col, self.nib = self.col - 1, 1
        else:
            self.move_rows(-1, rows_shown)
            self.col, self.nib = ROW - 1, 1

    def right(self, rows_shown: int = 8) -> None:
        """1字進む。行の最後のバイトの下の桁(と行の終わり)からは、次の行の最初のバイトへ"""
        self.addr_done = False
        if self.field == "addr":
            if self.nib < 3:
                self.nib += 1
            else:
                self.field, self.col, self.nib = "data", 0, 0
        elif self.col >= ROW or (self.col == ROW - 1 and self.nib == 1):
            self.move_rows(1, rows_shown)
            self.col, self.nib = 0, 0
        elif self.nib == 0:
            self.nib = 1
        else:
            self.col, self.nib = self.col + 1, 0

    def place(self, addr: int, rows_shown: int = 8) -> None:
        """カーソルを番地addrのバイトに置く。見えている行にあれば行の並びはそのまま、
        なければ開始番地と同じ並びの行を、表示の3行目に出す"""
        addr &= 0xFFFF
        first = (self.row - ROW * self.top_rows) & 0xFFFF
        off = (addr - first) & 0xFFFF
        self.addr_done = False
        if off < ROW * rows_shown:
            self.row, self.top_rows = (first + off // ROW * ROW) & 0xFFFF, off // ROW
        else:
            self.row = (addr - (addr - self.start) % ROW) & 0xFFFF
            self.top_rows = min(2, rows_shown - 1)
        self.col, self.nib, self.field = (addr - self.row) & 0xFFFF, 0, "data"

    def go(self, addr: int, rows_shown: int = 8) -> None:
        """その番地の行へ(行の頭は番地そのもの)"""
        self.row, self.col, self.nib, self.field = addr & 0xFFFF, 0, 0, "data"
        self.top_rows = min(2, rows_shown - 1)

    def paste(self, text: str) -> PasteResult:
        """本のダンプリストや.hexの行を貼り付けて書く"""
        res = PasteResult()
        rows, res.skipped = parse_dump(text)
        at = self.cursor if self.field == "data" else self.row
        edit = self._begin(lambda: tr("貼り付け"))
        for addr, data, total in rows:
            if addr is not None:
                at = addr
                if res.first is None:
                    res.first = addr
            for i, b in enumerate(data):
                if self.write(at + i, b):
                    res.written += 1
                    self.changed.discard((at + i) & 0xFFFF)
                else:
                    res.outside += 1
            if total is not None and checksum(data) != total:
                res.bad_sums.append(at)
            at = (at + len(data)) & 0xFFFF
        if res.first is not None:
            if self.start_by != "user":
                self.start, self.start_by = res.first, "auto"
            self.go(res.first)
        if res.written:
            n = res.written
            edit.what = lambda: tr("貼り付け({n}バイト)", n=n)
            self._commit(edit)
        return res

    def _drop_marks(self, blocks: list[tuple[int, bytes]]) -> None:
        """読み込んだプログラムの範囲にある、自分で決めた指定を消す(前のプログラムのもの)。
        読み込んだプログラムの# data: の範囲は置き直す。鏡の番地に書いたものは、RAMの番地に
        書いたものとして扱う(指定はRAMの番地に置く)"""
        spans = self._real_spans(blocks)
        for lo, hi in spans:
            for a in [a for a in self.marks if lo <= a < hi]:
                del self.marks[a]
        # 前のプログラムの、手前から続くデータの指定は、読み込んだ所の手前で切る(読み込んだ
        # 所から先は推し量り)。いま読み込んでいるプログラムの# data: の指定は切らない
        own = {lo for lo, _hi in self.prog_data}
        for lo, _hi in spans:
            before = [a for a in self.marks if a < lo]
            if before and self.marks[max(before)] == "data" and max(before) not in own:
                self.marks[lo] = "end"
        self._put_data()

    def _real_spans(self, blocks: list[tuple[int, bytes]]) -> list[tuple[int, int]]:
        """ダンプを書く範囲を、RAMの番地のひと続き(始め, 終わりの次)にする(鏡の番地は
        RAMの番地にし、RAMの外は除く)"""
        spans: list[tuple[int, int]] = []
        for addr, data in blocks:
            for i in range(len(data)):
                a = self._mark_at((addr + i) & 0xFFFF)
                if not self.real_ram(a):
                    continue
                if spans and spans[-1][1] == a:
                    spans[-1] = (spans[-1][0], a + 1)
                else:
                    spans.append((a, a + 1))
        return spans

    def use_data(self, spans: list[tuple[int, int | None]]) -> None:
        """読み込んだプログラムの# data: の範囲を、自分で決めたデータの指定にする。重なる
        範囲と続いている範囲は1つにまとめ、範囲の中にある番地だけの指定は捨てる"""
        # 鏡の番地で書いた範囲は、RAMの番地にする(鏡の境をまたぐ範囲は、RAMの番地の
        # ひと続きごとに分ける)
        real: list[tuple[int, int | None]] = []
        for lo, hi in spans:
            if hi is None:
                real.append((self._mark_at(lo), None))
            else:
                real += self._real_spans([(lo, bytes(max(0, hi - lo)))])
        spans = real
        ranges: list[tuple[int, int]] = []
        for lo, hi in sorted((lo, hi) for lo, hi in spans if hi is not None):
            if ranges and lo <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
            else:
                ranges.append((lo, hi))
        points = sorted({lo for lo, hi in spans if hi is None})
        points = [p for p in points if not any(lo <= p < hi for lo, hi in ranges)]
        self.prog_data = [*ranges, *((p, None) for p in points)]
        self._put_data()

    def _put_data(self) -> None:
        """prog_dataの範囲をデータの指定にする。範囲の終わりには、データをそこで終える印
        ("end")を置く(そこに指定がなければ)"""
        for lo, hi in self.prog_data:
            if not self.real_ram(lo):
                continue
            self.marks[lo] = "data"
            if hi is not None and self.real_ram(hi) and hi not in self.marks:
                self.marks[hi] = "end"
        for lo, hi in self.prog_data:  # 番地だけの指定は、ほかの範囲の終わりの印より先に効く
            if hi is None and self.real_ram(lo):
                self.marks[lo] = "data"

    def data_spans(self) -> list[tuple[int, int]]:
        """自分で決めたデータの範囲(始め, 終わりの次)。書き出しの# data: に使う"""
        cm = self.code_map()
        spans = []
        for a, what in sorted(self.marks.items()):
            if what != "data":
                continue
            end = a
            while end < 0x10000 and cm.why[end] == codemap.USER and cm.state(end)[0] == "data":
                end += 1
                if end in self.marks:
                    break
            if end > a:
                spans.append((a, end))
        return spans

    def note_written(self, blocks: list[tuple[int, bytes]]) -> None:
        """ほかの経路(ROMのある本体の読み込み、--load)でRAMに書いたダンプを、作業欄から
        書いたものとして覚える(書き出す範囲に入れる)。RAMに入った番地だけを数える"""
        self._drop_marks(blocks)
        mirror = self.m.model.mirror
        for addr, data in blocks:
            for i in range(len(data)):
                a = (addr + i) & 0xFFFF
                if not self.m.writable(a):
                    continue
                if mirror and mirror[0] <= a < mirror[1]:
                    a += mirror[2]
                self.written[a] = 1
                self._forget_run(a)

    def set_start(self, blocks: list[tuple[int, bytes]], start: int | None = None) -> None:
        """読み込んだプログラムの開始番地を決める。startがなければ最初のダンプの番地"""
        if start is None and blocks:
            start = blocks[0][0]
        if start is not None:
            self.start, self.start_by = start, "auto"
            self.go(start)

    def load_blocks(self, blocks: list[tuple[int, bytes]], start: int | None = None) -> int:
        """ファイルから読んだダンプを書く。書いたバイト数を返す。読み込む前の書き込みは
        取り消せなくする(読み込みは「プログラムを読み込む前に戻す」で戻す)"""
        self.forget_edits()
        n = 0
        for addr, data in blocks:
            for i, b in enumerate(data):
                n += self.write(addr + i, b)
        self._drop_marks(blocks)
        if start is None and blocks:
            start = blocks[0][0]
        if start is not None:
            self.start, self.start_by = start, "auto"
            self.go(start)
        return n

    # ---- 開始番地 ----
    def start_refusal(self, addr: int) -> str:
        """番地addrを開始番地にできなければ、その理由(できれば空)"""
        if self.rom or self.in_ram(addr):
            return ""
        return tr("{addr:04X}番地はRAMの外なので、ROMなしでは開始番地にできません", addr=addr)

    def set_start_at(self, addr: int) -> str:
        """番地addrを開始番地に決める(カーソルの移動や貼り付けでは変わらなくなる)。
        知らせの文を返す。できなければ理由"""
        addr &= 0xFFFF
        self.start_buf = None
        why = self.start_refusal(addr)
        if why:
            return why
        self.start, self.start_by = addr, "user"
        return tr("開始番地を{addr:04X}にしました", addr=addr)

    def paste_start(self, text: str) -> str:
        """開始番地の欄に貼り付ける。16進表記の番地(「C010」「&C010」)なら開始番地にする。
        番地でなければ欄はそのままにして、理由を返す"""
        hit = re.fullmatch(r"&?([0-9A-Fa-f]{1,4})", text.strip())
        if hit is None:
            return tr("貼り付けた文字は番地ではありません(16進表記の4桁で)")
        return self.set_start_at(int(hit[1], 16))

    def type_start(self, v: int) -> str:
        """開始番地の欄に16進表記の1字を打つ。4桁そろえば開始番地にして知らせの文を返す"""
        buf = (self.start_buf or "") + f"{v:X}"
        if len(buf) < 4:
            self.start_buf = buf
            return ""
        return self.set_start_at(int(buf, 16))

    def dump_text(self, start: int, end: int) -> str:
        """start〜end-1を.hexの形(番地、バイト、逆アセンブル)にする"""
        lines = []
        for ins in self.lines_from(start):
            a = ins.addr
            if not start <= a < end:
                break
            size = max(1, min(ins.size, end - a))
            data = " ".join(f"{self.read(a + i):02X}" for i in range(size))
            lines.append(f"{a:04X} {data:<12}; {ins.line()}")
        return "\n".join(lines) + "\n"

    GAP = 64  # 書いた所どうしのあいだがこのバイト数より短ければ、つなげて1つの範囲にする

    def mark_nonzero(self) -> None:
        """書いた番地がわからないRAM(前の版が保存したもの)では、RAMの0でないバイトをすべて
        書いたものとして数える(書き出すときに、0が続いた先のコードを落とさない)"""
        lo, hi = self.ram
        for a in range(lo, hi):
            if self.m.mem[a]:
                self.written[a] = 1

    def export_ranges(self) -> list[tuple[int, int]]:
        """書き出す範囲([始め, 終わり)の並び)。作業欄から書いた番地(読み込んだダンプも含む)と、
        ROMなしの本体では開始番地からused_endまで(作業欄で書いていない、前から残っている
        プログラム)を合わせ、あいだの短いすきまはつなげる。ROMのある本体のRAMにはBASICや
        ROMの作業域も入っているので、書いた番地だけにする"""
        lo, hi = self.ram
        marks = bytearray(self.written[lo:hi])
        if not self.rom and lo <= self.start < hi and self.read(self.start):
            end = min(self.used_end(self.start), hi)
            marks[self.start - lo : end - lo] = b"\x01" * (end - self.start)
        ranges: list[tuple[int, int]] = []
        for i, on in enumerate(marks):
            if not on:
                continue
            a = lo + i
            if ranges and a - ranges[-1][1] < self.GAP:
                ranges[-1] = (ranges[-1][0], a + 1)
            else:
                ranges.append((a, a + 1))
        return ranges

    def export_summary(self) -> str:
        """書き出す範囲の短い説明(「C000〜C026(39バイト)」)。範囲がなければ空"""
        ranges = self.export_ranges()
        if not ranges:
            return ""
        size = sum(hi - lo for lo, hi in ranges)
        where = tr("、").join(span_text(lo, hi) for lo, hi in ranges[:2])
        if len(ranges) > 2:
            where = tr("{where}ほか{n}か所", where=where, n=len(ranges) - 2)
        return tr("{where}({n}バイト)", where=where, n=size)

    def used_end(self, start: int) -> int:
        """startから、0が16バイト続く手前まで(書いた番地がわからないときの、プログラムの
        終わりの目安。RAMの終わりまで)"""
        lo, hi = self.ram
        a, last = start, start
        while a < hi and a - last < 16:
            if self.read(a):
                last = a + 1
            a += 1
        return max(last, start + 1)

    # ---- 状態の保存 ----
    def save(self) -> dict:
        """どこでもセーブに一緒に書く作業欄の状態(開始番地、実行の状態、止める所、書いた範囲、
        命令とデータの見分けに使うもの)"""
        mirror = self.m.model.mirror
        executed = self._spans(self.executed, *self.ram)
        if mirror:
            executed += self._spans(self.executed, mirror[0], mirror[1])
        return {
            "start": self.start,
            "start_by": self.start_by,
            "state": self.state,
            "breaks": sorted(self.breaks),
            "written": self._spans(self.written, *self.ram),
            "row": self.row,
            "calls": list(self.calls),
            "executed": executed,
            "marks": [[a, what] for a, what in sorted(self.marks.items())],
        }

    @staticmethod
    def _spans(marks: bytearray, lo: int, hi: int) -> list[list[int]]:
        """lo〜hi-1のうち、印のついた番地のひと続きを[始め, 終わり]の並びにする"""
        spans, a = [], lo
        while a < hi:
            if marks[a]:
                b = a
                while b < hi and marks[b]:
                    b += 1
                spans.append([a, b])
                a = b
            else:
                a += 1
        return spans

    def restore(self, info: object, why: str | None = None) -> None:
        """saveで書いた状態に戻す。実行中か一時停止中に保存したものは、保存したときのPCで
        一時停止した状態にする(「続ける」で続きから動く)。情報がなければ編集中にする。
        whyは作業欄の下に出す知らせ(なければ「保存した状態に戻しました」)"""
        if why is None:
            why = tr("保存した状態に戻しました")
        info = info if isinstance(info, dict) else {}

        def addr(v: object, default: int) -> int:
            return v if isinstance(v, int) and 0 <= v < 0x10000 else default

        self.start = addr(info.get("start"), self.start)
        by = info.get("start_by", "auto" if "start" in info else "")
        self.start_by = by if isinstance(by, str) and by in ("", "auto", "user") else "auto"
        self.start_buf = None
        self.row = addr(info.get("row"), self.start)
        self.col, self.nib, self.field, self.addr_done, self.top_rows = 0, 0, "data", False, 2
        breaks = info.get("breaks")
        self.breaks = {a for a in breaks if addr(a, -1) >= 0} if isinstance(breaks, list) else set()
        self.forget_edits()
        self.written = bytearray(0x10000)
        self.executed = bytearray(0x10000)
        for name, marks in (("written", self.written), ("executed", self.executed)):
            spans = info.get(name)
            for span in spans if isinstance(spans, list) else []:
                if isinstance(span, list) and len(span) == 2:
                    a, b = addr(span[0], 0), addr(span[1], 0)
                    marks[a:b] = b"\x01" * max(0, b - a)
        calls = info.get("calls")
        self.calls = [a for a in calls if addr(a, -1) >= 0] if isinstance(calls, list) else []
        self.marks = {}
        marks = info.get("marks")
        for item in marks if isinstance(marks, list) else []:
            if isinstance(item, list) and len(item) == 2 and item[1] in ("data", "code", "end"):
                if addr(item[0], -1) >= 0:
                    self.marks[item[0]] = item[1]
        self.prog_data = []
        self._map = None
        self.temp_stop = None
        self.snap = bytes(self.m.mem)
        self.changed = set()
        self.prev_regs = bytes(self.m.cpu.ram)
        self.prev_ptr = self.ptrs()
        self.c0 = self.m.cpu.cycles
        if self.rom:
            # ROMのある本体は、保存したときに一時停止していればそこで止め、ほかは動かしておく
            self.ran = True
            if info.get("state") == "pause":
                self.state = "pause"
                self.reason = (
                    tr("{why}。{addr:04X}番地で一時停止しています", why=why, addr=self.m.cpu.pc)
                    if why
                    else ""
                )
            else:
                self.state, self.reason = "run", why
        elif info.get("state") in ("run", "pause"):
            self.state, self.ran = "pause", True
            pc = self.m.cpu.pc
            self.reason = (
                tr("{why}。{addr:04X}番地から続けられます", why=why, addr=pc) if why else ""
            )
        else:
            self.state = "edit"
            self.reason = why

    # ---- 実行 ----
    def needs_guard(self) -> bool:
        """命令ごとにguardを調べる要があるか。ROMのある本体では、止める所も呼び出しの戻り先も
        なければ要らない(ROMのBASICを、調べずに速く動かす)"""
        return not self.rom or bool(self.breaks) or self.temp_stop is not None

    def guard(self) -> bytearray:
        """命令を読む前に止まる番地。止める所と、ROMなしの本体ではRAMの外"""
        if self._guard is None:
            g = bytearray(b"\x01" * 0x10000)
            lo, hi = self.ram
            g[lo:hi] = bytes(hi - lo)
            mirror = self.m.model.mirror
            if mirror:
                g[mirror[0] : mirror[1]] = bytes(mirror[1] - mirror[0])
            if self.rom:
                g[:] = bytes(0x10000)
            self._base_guard = bytes(g)
            self._guard = g
        g = self._guard
        g[:] = self._base_guard
        for a in self.breaks:
            g[a] = 1
        if self.temp_stop is not None:
            g[self.temp_stop] = 1
        return g

    def ptrs(self) -> tuple[int, int, int, int, int, int, int]:
        cpu = self.m.cpu
        return (cpu.pc, cpu.dp, cpu.p, cpu.q, cpu.r, cpu.c, cpu.z)

    def _prepare(self, addr: int) -> None:
        """addrから実行を始める用意。戻り先を積み、液晶を消して表示をつける"""
        m, cpu = self.m, self.m.cpu
        self.prev_regs = bytes(cpu.ram)
        self.prev_ptr = self.ptrs()
        self.snap = bytes(m.mem)
        self.changed = set()
        if self.clear_lcd:
            for lo, hi in m.model.lcd:
                for a in range(lo, hi):
                    m.write(a, 0)
            m.out_c(m.pc_out | 0x01)
        m.power = True
        m.halted = False
        cpu.r = STACK_TOP
        cpu.push(SENTINEL >> 8)
        cpu.push(SENTINEL & 0xFF)
        cpu.pc = addr
        self.c0 = cpu.cycles
        self.temp_stop = None
        self.ran = True

    def run(self, addr: int | None = None) -> None:
        """開始番地(addr)から実行する"""
        self._prepare(self.start if addr is None else addr)
        self.state = "run"
        self.reason = ""

    def resume(self) -> None:
        """一時停止したところから続ける。止める所にいるなら、その命令を実行してから。
        実行できない番地(ROMなしで使えないROM)で止まっているときは、何もしない"""
        if self.state != "pause" or self._refuse():
            return
        self.prev_regs = bytes(self.m.cpu.ram)
        self.prev_ptr = self.ptrs()
        if self.rom:
            self._mark_start()
        if self.m.cpu.pc in self.breaks:
            self._step1()
            if self.state != "pause":
                return
        self.state = "run"
        self.reason = ""

    def pause(self, why: str | None = None) -> None:
        if self.state == "run":
            self._stopped("pause", tr("一時停止しました") if why is None else why)

    def abandon(self) -> None:
        """実行をやめて編集に戻る(止まっているプログラムの続きは捨てる)。ROMのある本体では
        何もしない(本体はROMで動き続ける)"""
        if self.state != "edit" and not self.rom:
            self.temp_stop = None
            self._stopped("edit", tr("実行を途中でやめて、編集に戻りました(メモリはそのまま)"))

    def step(self) -> None:
        """1命令だけ実行する。編集中なら、開始番地で止まった状態にする。ROMなしで使えない
        番地(RAMの外)で止まっているときは、何も実行しない"""
        if self.state == "edit":
            self._prepare(self.start)
            self.state = "pause"
            self.reason = tr("{addr:04X}番地の最初の命令の前で止まっています", addr=self.start)
            return
        if self.state == "pause" and not self._refuse(step=True):
            self.prev_regs = bytes(self.m.cpu.ram)
            self.prev_ptr = self.ptrs()
            if self.rom:
                self._mark_start()
            self._step1()

    def step_over(self) -> None:
        """呼び出しの命令なら、戻ってくるまで実行する。ほかの命令は1命令"""
        if self.state != "pause":
            self.step()
            return
        pc = self.m.cpu.pc
        if self._refuse(step=True):
            return
        if not disasm.is_call(self.read, pc):
            self.step()
            return
        self.prev_regs = bytes(self.m.cpu.ram)
        self.prev_ptr = self.ptrs()
        if self.rom:
            self._mark_start()
        self.temp_stop = (pc + disasm.length(self.read(pc))) & 0xFFFF
        self.temp_r = self.m.cpu.r
        self._step1()  # 呼び出しの命令を実行してから、戻り先で止まるまで回す
        # ROMのルーチンの代わりを済ませたときは、_step1のうちに戻り先で止まっている
        # (temp_stopが消える)。そうでなければ、戻り先まで実行を続ける
        if self.state == "pause" and self.temp_stop is not None:
            self.state = "run"
            self.reason = ""

    def _mark_start(self) -> None:
        """ROMのある本体で、続けるか1命令進めるところ。ここからのメモリの変化に色をつけ、
        サイクルもここから数える(本体はずっとROMで動いているので、作業欄から実行を始めた
        ところがほかにない)"""
        self.snap, self.changed, self.c0 = bytes(self.m.mem), set(), self.m.cpu.cycles
        self.ran = True

    def _refuse(self, step: bool = False) -> bool:
        """いまのPCの命令を実行できなければ、理由を知らせにしてTrueを返す(状態は変えない)。
        stepなら1命令ずつ進めるとき。ROMのある本体でCPUがキーかタイマを待って止まっている
        (HALT)ときは、進める命令がないので断る"""
        why = self.blocked(self.m.cpu.pc)
        if not why and step and self.rom and self.m.halted:
            why = tr("CPUはキーかタイマを待って止まっています(HALT)。「続ける」で進みます")
        if why:
            self.reason = why
        return bool(why)

    def _step1(self) -> None:
        """いまのPCの命令を1つ実行して、止まるかどうかを調べる。実行できない番地なら何もしない"""
        if self._refuse():
            return
        m = self.m
        if not self.rom:  # ROMのある本体では、キーかタイマを待つHALTを勝手に起こさない
            m.halted = False
        self.pass_pc = None
        m.run(1, None, self.executed)
        self._check(m.cpu.pc, after_step=True)

    def pass_step(self) -> None:
        """pass_pcの命令を、止めずに1つ実行する(再帰の内側の戻り先を通り過ぎる)。実行を進める
        側(App._advance)が、1回ずつ呼ぶ。続く番地も止める所などなら、そこで調べる"""
        self.pass_pc = None
        m = self.m
        if not self.rom:
            m.halted = False
        m.run(1, None, self.executed)
        pc = m.cpu.pc
        if self.guard()[pc]:
            self._check(pc, after_step=False)

    def trapped(self) -> bool:
        """実行中にguardの番地で止まったときに呼ぶ。実行を続けるならTrue(ROMのルーチンの
        代わりを済ませて、呼んだ所へ戻ったとき)"""
        self._check(self.m.cpu.pc, after_step=False)
        return self.state == "run"

    def _check(self, pc: int, after_step: bool) -> None:
        if not self.in_ram(pc) and norom.call(self.m, pc):
            # ROMのルーチンの代わりを済ませて、呼んだ所へ戻った
            pc = self.m.cpu.pc
            if not after_step and pc not in self.breaks and pc != self.temp_stop:
                return
        if pc == SENTINEL and not self.rom:
            cyc = self.m.cpu.cycles - self.c0
            ms = f"{cyc / 192:.1f}"
            self._stopped("edit", tr("RTNで戻りました({n}サイクル、約{ms}ミリ秒)", n=cyc, ms=ms))
            return
        if pc == self.temp_stop:
            if self.m.cpu.r >= self.temp_r:
                self.temp_stop = None
                self._stopped("pause", tr("呼び出しから戻りました"))
                return
            if not after_step and pc not in self.breaks:
                # 再帰で、内側の呼び出しから同じ戻り先へ戻っただけ(Rがまだ深い)。戻り先の
                # 命令は、実行を進める側が止めずに1つ実行して(pass_step)、そのまま続ける。
                # 止める所なら、下で止まる
                self.pass_pc = pc
                return
        why = self.blocked(pc)  # ROMなしで使えない番地(ROMがあれば、どこでも実行できる)
        if why:
            self._stopped("pause", why)
            return
        if pc in self.breaks and not after_step:
            self._stopped("pause", tr("止める所({addr:04X})で止まりました", addr=pc))
            return
        if after_step:
            self.state = "pause"
            self.reason = ""
        else:
            self._stopped("pause", tr("{addr:04X}番地で止まりました", addr=pc))

    @staticmethod
    def _outside(pc: int) -> str:
        """RAMの外の番地pcへ飛んで止まったときの知らせ"""
        name = rom_name(pc)
        if name:
            return tr("ROMの{name}({addr:04X})へ飛んだので止めました。ROMなしでは使えません",
                      name=name, addr=pc)  # fmt: skip
        return tr("RAMの外の{addr:04X}番地へ飛んだので止めました。ROMなしでは使えません", addr=pc)

    def _stopped(self, state: str, why: str) -> None:
        self.pass_pc = None
        self.state = state
        self.reason = why
        if self.rom and not self.ran:
            # ROMのある本体で、作業欄から続けたことがないうちは、どこからの変化か決まらない
            # (ROMはずっと動いている)ので色をつけない
            self.changed = set()
            return
        m, snap = self.m, self.snap
        ranges = [self.ram, *m.model.lcd]
        self.changed = {a for lo, hi in ranges for a in range(lo, hi) if m.mem[a] != snap[a]}

    def ins_at(self, addr: int) -> int:
        """addrを含む行(命令かデータの行)の番地(命令の切れ目がわからなければaddr)"""
        for ins in self.disasm_from(addr, 3, addr):
            if ins.addr <= addr < ins.addr + ins.size:
                return ins.addr
        return addr

    def toggle_break(self, addr: int) -> bool | None:
        """止める所を置くか外す。置いたらTrue、外したらFalse。実行できない番地(ROMなしで
        使えないROM)には置かずにNone"""
        if addr not in self.breaks and self.blocked(addr):
            return None
        if addr in self.breaks:
            self.breaks.discard(addr)
            return False
        self.breaks.add(addr)
        return True

    # ---- 命令とデータの見分け ----
    def mark_refusal(self, addr: int, what: str | None) -> str:
        """番地addrの指定をwhat("data"、"code"、Noneは消す)にできなければ、その理由"""
        if what is None:
            return "" if addr in self.marks else tr("指定はありません")
        if not self.real_ram(addr):
            return tr("RAMの外です")
        if self.marks.get(addr) == what:
            return tr("いまの指定です")
        return ""

    def set_mark(self, addr: int, what: str | None) -> str:
        """番地addrから、データ("data")か命令("code")に決める。Noneなら指定を消して、
        推し量りに戻す。知らせの文を返す(できなければ理由)"""
        addr &= 0xFFFF
        why = self.mark_refusal(addr, what)
        if why:
            return why
        if what is None:
            del self.marks[addr]
            return tr("{addr:04X}番地の指定を消しました", addr=addr)
        self.marks[addr] = what
        if what == "data":
            return tr("{addr:04X}番地からデータにしました", addr=addr)
        return tr("{addr:04X}番地から命令にしました", addr=addr)

    def target_ok(self, t: int) -> bool:
        """飛び先tが、ありそうな番地か(RAM、ROMのある本体ではROM、ROMなしでは名前の
        わかっているROMのルーチンの入口)"""
        if self.in_ram(t):
            return True
        if self.rom:
            bas = self.m.model.bas_addr
            return t < 0x2000 or bas <= t < bas + self.m.model.bas_size
        return t in norom.ROUTINES or any(a == t for a, _ in romsyms.ROUTINES_1251.values())

    def _executed_list(self) -> list[int]:
        """実行した番地(RAMの中。鏡の番地はRAMの番地にする)"""
        lo, hi = self.ram
        out = [lo + i for i, v in enumerate(self.executed[lo:hi]) if v]
        mirror = self.m.model.mirror
        if mirror:
            ex = self.executed[mirror[0] : mirror[1]]
            out += [mirror[0] + i + mirror[2] for i, v in enumerate(ex) if v]
        return out

    def code_map(self) -> codemap.CodeMap:
        """命令とデータの見分け。入口や指定が変わったときと、RAMか実行した番地が変わった
        ときに作り直す(実行中はMAP_EVERY秒に1回まで)"""
        lo, hi = self.ram
        mirror = self.m.model.mirror
        ex = bytes(self.executed[lo:hi])
        if mirror:
            ex += bytes(self.executed[mirror[0] : mirror[1]])
        key = (language(), self.start, self.start_by, tuple(self.calls), frozenset(self.breaks),
               tuple(sorted(self.marks.items())), bytes(self.written[lo:hi]))  # fmt: skip
        mem = (bytes(self.m.mem[lo:hi]), ex)
        cm = self._map
        if cm is not None and key == self._map_key:
            if mem == self._map_mem:
                return cm
            if self.state == "run" and time.monotonic() - self._map_at < MAP_EVERY:
                return cm
        in_ram = self.real_ram
        # 開始番地は、決めたあとだけ入口にする(ROMのある本体の起動したままの開始番地には、
        # BASICのプログラムや変数があるかもしれない)
        entries = [self.start] if self.start_by else []
        entries += [*self.calls, *self.breaks]
        cm = codemap.build(
            self.read,
            in_ram=in_ram,
            target_ok=self.target_ok,
            regions=self.export_ranges(),
            entries=[a for a in entries if in_ram(a)],
            executed=self._executed_list(),
            marks=self.marks,
            ram_end=hi,
        )
        self._map, self._map_key, self._map_mem, self._map_at = cm, key, mem, time.monotonic()
        return cm

    _map_mem: tuple[bytes, bytes] = (b"", b"")

    def real_ram(self, a: int) -> bool:
        """aがRAMの番地か(鏡の番地は含まない)"""
        lo, hi = self.ram
        return lo <= a < hi

    def lines_from(self, addr: int) -> Iterator[disasm.Ins]:
        """addrから順に、命令とデータの行。見分けた所は見分けに従い、ほかは命令として読む
        (見分けた行の頭に重なる命令は、その手前までのデータの行にする)"""
        cm = self.code_map()
        walk: Iterator[disasm.Ins] | None = None
        a = addr & 0xFFFF
        while True:
            ins = cm.line_at(a)
            if ins is None:
                if walk is None:
                    walk = disasm.lines(self.read, a)
                ins = next(walk)
                cut = next((i for i in range(1, ins.size) if cm.is_start(a + i)), 0)
                if cut:
                    ins = codemap.data_line(self.read, a, cut)
                    walk = None
            else:
                walk = None
            yield ins
            a = (a + ins.size) & 0xFFFF

    def disasm_from(self, anchor: int, count: int, focus: int) -> list[disasm.Ins]:
        """count行の逆アセンブル。focusを含む行の2行前から出す。focusが見分けた所にあれば、
        その行の切れ目に合わせる。なければ、開始番地(RAMの外のとき)から命令の切れ目を
        たどれるならその並びを、どれでもなければanchorから読む"""
        cm = self.code_map()
        head = cm.start_of(focus)
        if head is not None:
            heads = [head]
            for _ in range(2):  # 手前の2行も、見分けた所なら行の切れ目に合わせて出す
                prev = cm.start_of((heads[0] - 1) & 0xFFFF)
                if prev is None:
                    break
                heads.insert(0, prev)
            first = heads[0]
        else:
            first = anchor
            start = self.start
            if start <= focus < start + 0x400:
                seq = []
                for ins in self.lines_from(start):
                    seq.append(ins)
                    if ins.addr + ins.size > focus or len(seq) >= 2000:
                        break
                first = seq[-3:][0].addr
        out = []
        for ins in self.lines_from(first):
            out.append(ins)
            if len(out) >= count:
                break
        return out


def rom_name(addr: int) -> str:
    """番地addrのROMのルーチンの名前と働き(なければ空)。働きは画面の言語で書く"""
    for name, (a, what) in romsyms.ROUTINES_1251.items():
        if a == addr:
            return tr("{name}({what})", name=name, what=tr(what))
    return ""


def span_text(lo: int, hi: int) -> str:
    """範囲[lo, hi)の書き方(「C000〜C026」)"""
    return tr("{lo:04X}〜{last:04X}", lo=lo, last=hi - 1)


# ---- 描く ----
MONO_FONTS = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

PANE_W = 470  # 作業欄の幅(拡大率によらない)
BG = (28, 30, 34)
FG = (226, 228, 222)
DIM = (128, 132, 140)
HEAD = (150, 176, 220)
CURSOR = (88, 120, 190)
CHANGED = (240, 170, 80)  # 実行で変わったバイト
REGCHG = (255, 214, 120)  # 前に止まったときから変わったレジスタ
PCLINE = (52, 70, 52)
INSLINE = (44, 52, 72)  # カーソルのある命令(ダンプのバイトと逆アセンブルの行)
BREAK = (210, 70, 70)
OK = (120, 200, 140)
WARN = (236, 120, 110)
BTN = (58, 61, 68)
BTN_ON = (84, 90, 104)
BG_DIM = (44, 46, 52)  # 押せないボタンの地
TIP_BG = (14, 15, 18)
WHITE = (240, 240, 235)
START = (232, 184, 96)  # 開始番地(欄の値と、ダンプと逆アセンブルの印)
UNSURE = (160, 163, 170)  # 命令かどうかわからない行(どこからもたどれない)
DATA_FG = (134, 172, 160)  # データの行


def why_mark(why: str) -> str:
    """逆アセンブルの行の右に出す、命令かデータかを決めたわけの1字"""
    marks = {"executed": tr("実"), "flow": tr("流"), "guessed": tr("推"), "user": tr("決")}
    return marks.get(why, "")


def why_text(cm: codemap.CodeMap, a: int) -> str:
    """番地aの行を命令かデータに決めたわけ(印にマウスを載せたときに出す)"""
    code, data = cm.why[a & 0xFFFF], cm.state(a)[0] == "data"
    if code == codemap.EXEC:
        return tr("実行した命令")
    if code == codemap.FLOW:
        return tr("開始番地、CALLの番地、止める所か実行した命令から、流れをたどって着いた命令")
    if code == codemap.TABLE:
        return tr("DTCの表(行の数は手前のPTCから)")
    if code == codemap.GUESS:
        if data:
            return tr("DTCの表(どこからもたどれないので、命令として読んだときの表)")
        return tr("どこからもたどれないので、命令かどうかわかりません")
    if code == codemap.TEXT:
        return tr("文字が並んでいるので、データとみました")
    if code == codemap.FILL:
        return tr("00かFFが並んでいるので、データとみました")
    if code == codemap.JUNK:
        return tr(
            "命令として読むと、ない命令か、RAMとROMの外へ飛ぶ命令があるので、データとみました"
        )
    if code == codemap.NOEND:
        return tr(
            "命令として読むと、RTNか条件なしのジャンプで終わらないまま途切れるので、"
            "データとみました"
        )
    if code == codemap.USER:
        return tr("自分で決めたデータ") if data else tr("自分で決めた命令")
    return ""


def load_mono(size: int) -> pygame.font.Font:
    for path in MONO_FONTS:
        if os.path.exists(path):
            try:
                return pygame.font.Font(path, size)
            except (OSError, pygame.error):
                continue
    return pygame.font.SysFont("monospace", size)


@dataclass
class Button:
    label: str
    action: str
    tip: str
    rect: pygame.Rect = field(default_factory=lambda: pygame.Rect(0, 0, 0, 0))
    enabled: bool = True  # 押せないときは暗く出し、押しても何もしない(説明は出す)


class BenchView:
    """作業欄を描き、クリックの場所を調べる"""

    LH = 18  # 行の高さ

    def __init__(self, bench: Bench, jfont: pygame.font.Font, cmd: str, shift: str) -> None:
        self.b = bench
        self.mono = load_mono(14)
        self.jfont = jfont
        self.small = load_help_font(13)
        self.cw = self.mono.size("0")[0]
        self.cmd, self.shift = cmd, shift
        self.buttons: list[Button] = []
        self.hits: list[tuple[pygame.Rect, str, int]] = []  # (場所, 種類, 番地)
        self.rows = 8  # ダンプと逆アセンブルの行の数
        self.hover = -1
        self.flash = ""  # 打てなかったときなどの短い知らせ
        self.flash_ok = False  # 済んだことの知らせなら緑、ほかは赤で出す
        self.flash_until = 0.0
        self.followed = -1  # 一時停止中に、ダンプをPCの行へ動かしたときのPC
        self.show_iram = True  # 内部RAMの16進表記を出すか(見出しのクリックで開く・閉じる)
        # パソコンのキーを作業欄で受けているか(ROMのある本体で、作業欄をクリックしたあと)。
        # ROMなしの本体では、実行していないあいだはいつも作業欄で受ける
        self.focus = False
        # 書き出す範囲と、それを求めた時刻(RAMを調べるので、毎フレームは求めない)
        self._ranges: tuple[list[tuple[int, int]], float] = ([], 0.0)
        self.why_at = -1  # マウスを載せている、逆アセンブルの行のわけの印の番地

    def say(self, text: str, seconds: float = 3.0, ok: bool = False) -> None:
        """作業欄の下に短く出す知らせ。okなら済んだことの知らせ(ふつうは打てなかったなど)"""
        self.flash = text
        self.flash_until = time.monotonic() + seconds
        self.flash_ok = ok

    IRAM_ROWS = 6  # 内部RAMの行の数(16バイトずつ)
    STATUS_LINES = 2  # 作業欄のいちばん下の知らせの行の数

    def min_height(self, show_iram: bool | None = None) -> int:
        """作業欄が収まる高さ。内部RAMを閉じていれば、その行の分だけ低い"""
        show = self.show_iram if show_iram is None else show_iram
        return 740 - (0 if show else self.IRAM_ROWS * self.LH)

    def _text(self, dst, x, y, s, color=FG, font=None) -> int:
        f = font or self.mono
        img = f.render(s, True, color)
        dst.blit(img, (x, y))
        return x + img.get_width()

    def button_specs(self) -> list[Button]:
        """いまの状態で押せるボタン。名前は短くし、マウスを載せたときの説明(tip)に、
        いま押すと何が起きるかを具体的に書く(次の命令、呼び先の番地など)"""
        b, c, s = self.b, self.cmd, self.shift
        at = b.ins_at(b.cursor)
        brk = Button(
            tr("● 止める所"), "break",
            tr("カーソルの命令({addr:04X}番地)に止める所を置く。置いてあれば外す({c}B)",
               addr=at, c=c),
        )  # fmt: skip
        back = Button(
            tr("編集へ"), "abandon",
            tr("実行を途中でやめて編集に戻る。書き換わったメモリはそのまま(Esc)"),
        )  # fmt: skip
        if b.state == "run":
            pause = Button(
                tr("■ 一時停止"), "pause", tr("実行を、いまの命令の前で止める({c}.)", c=c)
            )
            return [pause, brk] if b.rom else [pause, brk, back]
        if b.state == "edit":
            return [
                Button(
                    tr("▶ 実行"), "run",
                    tr("開始番地{addr:04X}から実行する。RTNで戻れば終わり({c}G)",
                       addr=b.start, c=c),
                ),
                Button(
                    tr("1命令ずつ"), "step",
                    tr("開始番地{addr:04X}の最初の命令の前で止める。そこから1命令ずつ進める({c}J)",
                       addr=b.start, c=c),
                ),
                brk,
            ]  # fmt: skip
        pc = b.m.cpu.pc
        if b.blocked(pc):
            # ROMなしで使えない番地で止まっている。続けることも1命令進めることもできない
            tip = tr("{addr:04X}番地からは続けられないので、編集に戻る(Esc)", addr=pc)
            return [Button(tr("編集へ"), "abandon", tip)]
        ins = disasm.decode(b.read, pc)
        specs = [
            Button(tr("▶ 続ける"), "resume",
                   tr("{addr:04X}番地から続けて実行する({c}G)", addr=pc, c=c)),
            Button(
                tr("1命令"), "step",
                tr("次の命令「{ins}」({addr:04X}番地)だけを実行して止まる({c}J)",
                   ins=ins.text(), addr=pc, c=c),
            ),
        ]  # fmt: skip
        if disasm.is_call(b.read, pc) and ins.target is not None:
            tip = tr(
                "「{ins}」で{addr:04X}番地を呼び、RTNで戻ってくるまで実行して、次の命令で止まる({s}{c}J)",
                ins=ins.text(), addr=ins.target, s=s, c=c,
            )  # fmt: skip
            specs.append(Button(tr("CALLごと"), "over", tip))
        return specs + [brk] if b.rom else specs + [brk, back]

    def _tip(self, dst: pygame.Surface, rect: pygame.Rect) -> None:
        """マウスを載せたボタンの説明を、ボタンのすぐ下に出す(作業欄の幅で折り返す)"""
        if not 0 <= self.hover < len(self.buttons):
            return
        btn = self.buttons[self.hover]
        width = min(rect.width - 24, 380)
        body, key = btn.tip, ""
        if body.endswith(")") and "(" in body:  # 末尾のショートカットは途中で折り返さない
            cut = body.rindex("(")
            body, key = body[:cut], body[cut:]
        lines = wrap_text(self.jfont, body, width - 16)
        if key and self.jfont.size(lines[-1] + key)[0] > width - 16:
            lines.append(key)
        else:
            lines[-1] += key
        imgs = [self.jfont.render(t, True, WHITE) for t in lines]
        lh = imgs[0].get_height()
        box = pygame.Rect(0, 0, max(i.get_width() for i in imgs) + 16, lh * len(imgs) + 10)
        box.topleft = (btn.rect.x, btn.rect.bottom + 4)
        box.right = min(box.right, rect.right - 6)
        box.x = max(box.x, rect.x + 6)
        pygame.draw.rect(dst, TIP_BG, box, border_radius=5)
        pygame.draw.rect(dst, BTN_ON, box, 1, border_radius=5)
        for k, img in enumerate(imgs):
            dst.blit(img, (box.x + 8, box.y + 5 + lh * k))

    def draw(self, dst: pygame.Surface, rect: pygame.Rect) -> None:
        b = self.b
        dst.fill(BG, rect)
        self.hits = []
        x0, y = rect.x + 12, rect.y + 8
        state = {"edit": tr("編集中"), "run": tr("実行中"), "pause": tr("一時停止中")}[b.state]
        if b.rom:
            title = tr("マシン語の作業欄  {state}", state=state)
            x = self._text(dst, x0, y, title, HEAD, self.jfont)
            # パソコンのキーがどちらへ行くか(一時停止中はいつも作業欄へ)
            if self.focus or b.state == "pause":
                where, color = tr("キーは作業欄へ"), OK
            else:
                where, color = tr("キーは本体へ"), DIM  # 作業欄へ移す方法は、下の知らせに出る
            self._text(dst, x + 12, y + 1, where, color, self.jfont)
            if self.focus and b.state != "pause":
                pygame.draw.rect(dst, BTN_ON, rect, 2)
        else:
            title = tr("マシン語(ROMなし)  {state}", state=state)
            self._text(dst, x0, y, title, HEAD, self.jfont)
        y += 26
        # ボタン
        self.buttons = self.button_specs()
        bx = x0
        for i, btn in enumerate(self.buttons):
            w = self.jfont.size(btn.label)[0] + 16
            if bx > x0 and bx + w > rect.right - 12:  # 入らなければ次の段へ
                bx, y = x0, y + 32
            btn.rect = pygame.Rect(bx, y, w, 26)
            pygame.draw.rect(dst, BTN_ON if i == self.hover else BTN, btn.rect, border_radius=5)
            self._text(dst, bx + 8, y + 3, btn.label, FG, self.jfont)
            bx += w + 6
        if b.state == "pause" and b.blocked(b.m.cpu.pc):
            # 続けられない理由を、ボタンのすぐ横に出す(詳しい理由は下の知らせに出る)
            note = tr("ROMなしでは使えない{addr:04X}番地で止まっています", addr=b.m.cpu.pc)
            if bx + self.jfont.size(note)[0] > rect.right - 12:
                bx, y = x0, y + 32
            self._text(dst, bx + 4, y + 3, note, WARN, self.jfont)
        y += 34
        # 開始番地と液晶の設定
        x = self._start_field(dst, x0, y)
        if b.rom:
            # ROMのある本体では、マシン語はBASICから呼ぶ。液晶はROMが使うので消さない
            note = tr("(BASICで CALL &{addr:04X} と打って呼ぶ)", addr=b.start)
            self._text(dst, x + 12, y, note, DIM, self.jfont)
            y = self._export_line(dst, x0, y + 24)
            return self._draw_rest(dst, rect, x0, y)
        r = pygame.Rect(x + 14, y + 2, 14, 14)
        pygame.draw.rect(dst, FG, r, 1)
        if b.clear_lcd:
            pygame.draw.line(dst, OK, (r.x + 3, r.y + 7), (r.x + 6, r.y + 11), 2)
            pygame.draw.line(dst, OK, (r.x + 6, r.y + 11), (r.x + 11, r.y + 3), 2)
        x = self._text(dst, r.right + 6, y, tr("実行の前に液晶を消す"), DIM, self.jfont)
        self.hits.append((pygame.Rect(r.x, r.y, x - r.x, 18), "clearlcd", 0))
        y = self._export_line(dst, x0, y + 24)
        self._draw_rest(dst, rect, x0, y)

    def _start_field(self, dst: pygame.Surface, x0: int, y: int) -> int:
        """開始番地の欄。クリックすると16進表記4桁で打てる。値は、ダンプと逆アセンブルの
        開始番地の印と同じ色で出す。まだ自分で決めていなければ(自動)を添える"""
        b, cw = self.b, self.cw
        x = self._text(dst, x0, y, tr("開始番地"), DIM, self.jfont) + 10
        box = pygame.Rect(x - 4, y - 1, 4 * cw + 8, 20)
        typing = b.start_buf is not None
        pygame.draw.rect(dst, CURSOR if typing else BTN, box, 1, border_radius=3)
        if typing:
            buf = b.start_buf or ""
            for i in range(4):
                if i == len(buf):
                    pygame.draw.rect(dst, CURSOR, (x + i * cw, y + 1, cw, self.LH - 2))
                self._text(dst, x + i * cw, y + 1, buf[i] if i < len(buf) else "_", FG)
        else:
            self._text(dst, x, y + 1, f"{b.start:04X}", START)
        x = box.right
        if b.start_by != "user" and not typing:
            x = self._text(dst, x + 4, y, tr("(自動)"), DIM, self.jfont)
        self.hits.append((pygame.Rect(x0, y - 1, x - x0, 20), "start", 0))
        return x

    def _undo_buttons(self, dst: pygame.Surface, right: int, y: int) -> None:
        """書き出しの行の右端に、取り消しとやり直しのボタンを置く(実行のボタンとは分ける)。
        押せないときは暗く出し、マウスを載せると理由を出す"""
        b, c, s = self.b, self.cmd, self.shift
        note, on = b.undo_note()
        tip = tr("{note}({key})", note=note, key=f"{c}Z") if on else note
        undo = Button(tr("取り消す"), "undo", tip, enabled=on)
        note, on = b.undo_note(redo=True)
        tip = tr("{note}({key})", note=note, key=f"{s}{c}Z") if on else note
        redo = Button(tr("やり直す"), "redo", tip, enabled=on)
        bx = right
        for btn in (redo, undo):
            w = self.jfont.size(btn.label)[0] + 16
            bx -= w
            btn.rect = pygame.Rect(bx, y - 2, w, 24)
            bx -= 6
        for btn in (undo, redo):
            i = len(self.buttons)
            self.buttons.append(btn)
            color = (BTN_ON if i == self.hover else BTN) if btn.enabled else BG_DIM
            pygame.draw.rect(dst, color, btn.rect, border_radius=5)
            self._text(dst, btn.rect.x + 8, y + 1, btn.label, FG if btn.enabled else DIM,
                       self.jfont)  # fmt: skip

    def _export_line(self, dst: pygame.Surface, x0: int, y: int) -> int:
        """書き出しのボタン(右端に取り消しとやり直しのボタン)、次の行から書き出す範囲と合計の
        バイト数(作業欄の幅で折り返す)。ボタンはいつも左端に置き、範囲がいくつあっても位置が
        変わらないようにする。ボタンを押すと書き出す(⌘Eと同じ)"""
        ranges, at = self._ranges
        if time.monotonic() - at > 0.5:  # RAMを調べるので、0.5秒に1回だけ求める
            ranges = self.b.export_ranges()
            self._ranges = (ranges, time.monotonic())
        self._undo_buttons(dst, x0 + PANE_W - 24, y)
        if not ranges:
            self._text(dst, x0, y, tr("書き出すダンプはまだありません"), DIM, self.jfont)
            # 範囲の1行分をあけておき、取り消しで範囲がなくなってもダンプの位置を変えない
            return y + 26 + 20 + 4
        label = tr(".hexに書き出す {c}E", c=self.cmd)
        btn = pygame.Rect(x0, y - 2, self.jfont.size(label)[0] + 16, 24)
        pygame.draw.rect(dst, BTN, btn, border_radius=5)
        self._text(dst, x0 + 8, y + 1, label, FG, self.jfont)
        self.hits.append((btn, "export", 0))
        size = sum(hi - lo for lo, hi in ranges)
        y += 26
        # 範囲は2行まで。入りきらなければ、残りの数を出す
        width = PANE_W - 24
        if len(ranges) > 1:
            head = tr("範囲({places}か所、合計{n}バイト) ", places=len(ranges), n=size)
        else:
            head = tr("範囲(合計{n}バイト) ", n=size)
        lines, line, comma = [], head, tr("、")
        for k, (lo, hi) in enumerate(ranges):
            part = span_text(lo, hi) + (comma if k < len(ranges) - 1 else "")
            rest = len(ranges) - k - 1
            # 2行目では、このあとに残りの数を書く場所もあけておく
            tail = tr("ほか{n}か所", n=rest) if rest and len(lines) == 1 else ""
            if self.jfont.size(line + part + tail)[0] > width:
                if len(lines) == 1:
                    line += tr("ほか{n}か所", n=len(ranges) - k)
                    break
                lines.append(line.rstrip())
                line = "　　 "
            line += part
        lines.append(line)
        for t in lines:
            self._text(dst, x0, y, t, DIM, self.jfont)
            y += 20
        return y + 4

    def _draw_rest(self, dst: pygame.Surface, rect: pygame.Rect, x0: int, y: int) -> None:
        """ダンプ、逆アセンブル、レジスタ、内部RAM、下の知らせ"""
        b, lh = self.b, self.LH
        # 残りの高さをダンプと逆アセンブルに分ける
        iram = lh * (self.IRAM_ROWS + 1) if self.show_iram else lh
        fixed = y - rect.y + lh * 2 + 8 + lh * 3 + 8 + lh * 4 + iram + 52
        self.rows = max(4, (rect.height - fixed) // (2 * lh))
        b.top_rows = min(b.top_rows, self.rows - 1)
        self._follow_pc()
        y = self._dump(dst, x0, y)
        y = self._disasm(dst, x0, y + 8)
        y = self._regs(dst, x0, y + 8)
        self._status(dst, x0, rect.bottom - 44, rect.width - 24)
        self._tip(dst, rect)
        self._why_tip(dst, rect)

    def _follow_pc(self) -> None:
        """止まるたび(1命令進めたときも)、ダンプのカーソルをPCの命令に置く。そのあと
        カーソルを動かせば、逆アセンブルはカーソルについていく"""
        b = self.b
        pc = b.m.cpu.pc
        if b.state != "pause":
            self.followed = -1
            return
        if pc == self.followed:
            return
        self.followed = pc
        b.place(pc, self.rows)

    def _dump(self, dst, x0: int, y: int) -> int:
        b, cw, lh = self.b, self.cw, self.LH
        # 列の見出しは、バイトとチェックサムの列と同じ位置に1つずつ置く
        for i in range(ROW):
            self._text(dst, x0 + (5 + 3 * i) * cw, y, f"+{i}", DIM)
        sigma = self.mono.render("Σ", True, DIM)
        sx = x0 + (6 + 3 * ROW) * cw + (2 * cw - sigma.get_width()) // 2
        dst.blit(sigma, (sx, y))
        y += lh + 2
        first = (b.row - ROW * b.top_rows) & 0xFFFF
        pc = b.m.cpu.pc if b.state == "pause" else -1
        # カーソルのある命令のバイト(逆アセンブルで色をつけた行と同じ命令)
        at = b.ins_at(b.cursor)
        size = next(b.lines_from(at)).size
        here = {(at + i) & 0xFFFF for i in range(size)}
        show_cursor = b.start_buf is None  # 開始番地の欄に打っているあいだはカーソルを出さない
        for r in range(self.rows):
            a = (first + ROW * r) & 0xFFFF
            cur_row = a == b.row
            # 番地
            if cur_row and b.field == "addr" and show_cursor:
                for i, ch in enumerate(f"{a:04X}"):
                    if i == b.nib:
                        pygame.draw.rect(dst, CURSOR, (x0 + i * cw, y, cw, lh - 2))
                    self._text(dst, x0 + i * cw, y, ch, FG)
            else:
                self._text(dst, x0, y, f"{a:04X}", FG if cur_row else DIM)
            self.hits.append((pygame.Rect(x0, y, 4 * cw, lh), "addr", a))
            data = b.row_bytes(a)
            for i, v in enumerate(data):
                ba = (a + i) & 0xFFFF
                bx = x0 + (5 + 3 * i) * cw
                color = CHANGED if ba in b.changed else FG
                if not b.m.writable(ba):
                    color = DIM
                if ba in here:
                    pygame.draw.rect(dst, INSLINE, (bx - 2, y, 2 * cw + 4, lh - 2))
                if ba == pc:
                    pygame.draw.rect(dst, PCLINE, (bx - 2, y, 2 * cw + 4, lh - 2))
                if cur_row and b.field == "data" and i == b.col and show_cursor:
                    pygame.draw.rect(dst, CURSOR, (bx + b.nib * cw, y, cw, lh - 2))
                if ba == b.start:  # 開始番地のバイトの左の隙間に▶
                    tri = [(bx - cw + 1, y + 3), (bx - cw + 1, y + 13), (bx - 3, y + 8)]
                    pygame.draw.polygon(dst, START, tri)
                self._text(dst, bx, y, f"{v:02X}", color)
                self.hits.append((pygame.Rect(bx, y, 2 * cw, lh), "byte", ba))
            if cur_row and b.field == "data" and b.col >= ROW and show_cursor:
                pygame.draw.rect(dst, CURSOR, (x0 + (5 + 3 * ROW) * cw - cw, y + lh - 4, cw, 2))
            self._text(dst, x0 + (6 + 3 * ROW) * cw, y, f"{checksum(data):02X}", OK)
            y += lh
        return y

    def _disasm(self, dst, x0: int, y: int) -> int:
        b, cw, lh = self.b, self.cw, self.LH
        x = self._text(dst, x0, y, tr("逆アセンブル"), HEAD, self.jfont)
        note = tr("(左の印をクリックで止める所)")
        self._text(dst, max(x0 + 130, x + 12), y + 1, note, DIM, self.jfont)
        y += lh + 2
        pc = b.m.cpu.pc
        focus = b.cursor  # ダンプのカーソル(一時停止したときはPCに置かれる)
        cm = b.code_map()
        tag_w = self.jfont.size(tr("開始"))[0]
        for ins in b.disasm_from(focus, self.rows, focus):
            a = ins.addr
            if a <= b.cursor < a + ins.size:
                pygame.draw.rect(dst, INSLINE, (x0 - 4, y, PANE_W - 24, lh - 1))
            if b.state == "pause" and a == pc:
                pygame.draw.rect(dst, PCLINE, (x0 - 4, y, PANE_W - 24, lh - 1))
                tri = [(x0 + 12, y + 3), (x0 + 12, y + 13), (x0 + 18, y + 8)]
                pygame.draw.polygon(dst, OK, tri)
            elif a == b.start:  # 開始番地の命令(PCの行ならPCの印を出す)
                tri = [(x0 + 12, y + 3), (x0 + 12, y + 13), (x0 + 18, y + 8)]
                pygame.draw.polygon(dst, START, tri)
            if a == b.start:
                tag = self.jfont.render(tr("開始"), True, START)
                dst.blit(tag, (x0 + PANE_W - 32 - tag.get_width(), y - 1))
            if a in b.breaks:
                pygame.draw.circle(dst, BREAK, (x0 + 5, y + 8), 5)
            else:
                pygame.draw.circle(dst, (60, 62, 68), (x0 + 5, y + 8), 5, 1)
            self.hits.append((pygame.Rect(x0 - 4, y, 24, lh), "break", a))
            data = "".join(f"{b.read(a + i):02X}" for i in range(min(ins.size, 4)))
            x = x0 + 3 * cw
            self._text(dst, x, y, f"{a:04X}", DIM)
            self._text(dst, x + 5 * cw, y, f"{data:<8}", DIM)
            if b.blocked(a):
                # ROMなしではROMの中身がない(0が並んでいるだけ)ので、命令としては出さない
                self._text(dst, x + 14 * cw, y, tr("(ROMの中身なし)"), DIM, self.jfont)
            else:
                kind, why = cm.state(a)
                if not b.in_ram(a):
                    color = DIM
                elif ins.name == "DB":
                    color = DATA_FG
                else:
                    color = UNSURE if why == "guessed" else FG
                text = f"{ins.name:<5} {ins.operand}"
                end = self._text(dst, x + 14 * cw, y, text, color)
                if ins.note:  # DTCの表の行の行き先
                    self._text(dst, end + cw, y, ins.note, DIM, self.jfont)
                mark = why_mark(why) if kind in ("head", "data") else ""
                if mark:
                    # わけの1字は、開始番地の「開始」の左に並べる(重ならない)
                    img = self.jfont.render(mark, True, DIM if a != self.why_at else FG)
                    mx = x0 + PANE_W - 32 - tag_w - 8 - img.get_width()
                    dst.blit(img, (mx, y - 1))
                    self.hits.append((pygame.Rect(mx - 2, y, img.get_width() + 4, lh), "why", a))
            self.hits.append((pygame.Rect(x, y, 30 * cw, lh), "goto", a))
            y += lh
        # 右端の印の見方(詳しいわけは、印にマウスを載せると出る)
        legend = tr(
            "{e}:実行した　{f}:たどった　{g}:推し量り　{u}:自分で決めた",
            e=why_mark("executed"), f=why_mark("flow"), g=why_mark("guessed"),
            u=why_mark("user"),
        )  # fmt: skip
        self._text(dst, x0, y + 1, legend, DIM, self.small)
        return y + lh

    def _why_tip(self, dst: pygame.Surface, rect: pygame.Rect) -> None:
        """マウスを載せた、わけの印の説明を、その行のすぐ下に出す(右に寄せる)"""
        if self.why_at < 0:
            return
        hit = next((r for r, kind, a in self.hits if kind == "why" and a == self.why_at), None)
        if hit is None:
            return
        text = why_text(self.b.code_map(), self.why_at)
        if not text:
            return
        width = min(rect.width - 24, 380)
        lines = wrap_text(self.jfont, text, width - 16)
        imgs = [self.jfont.render(t, True, WHITE) for t in lines]
        lh = imgs[0].get_height()
        box = pygame.Rect(0, 0, max(i.get_width() for i in imgs) + 16, lh * len(imgs) + 10)
        box.topright = (min(hit.right + 8, rect.right - 6), hit.bottom + 2)
        box.x = max(box.x, rect.x + 6)
        pygame.draw.rect(dst, TIP_BG, box, border_radius=5)
        pygame.draw.rect(dst, BTN_ON, box, 1, border_radius=5)
        for k, img in enumerate(imgs):
            dst.blit(img, (box.x + 8, box.y + 5 + lh * k))

    def _regs(self, dst, x0: int, y: int) -> int:
        b, cw, lh = self.b, self.cw, self.LH
        cpu = b.m.cpu
        ram, old = cpu.ram, b.prev_regs
        mark = b.state != "run" and b.ran
        x = self._text(dst, x0, y, tr("レジスタ"), HEAD, self.jfont)
        cyc = cpu.cycles - b.c0 if b.ran else 0
        self._text(dst, max(x0 + 80, x + 12), y + 1, tr("サイクル {n}", n=cyc), DIM, self.jfont)
        y += lh + 2

        def item(x: int, label: str, text: str, changed: bool) -> int:
            x = self._text(dst, x, y, label + "=", DIM)
            return self._text(dst, x, y, text, REGCHG if mark and changed else FG) + cw

        names = ["I", "J", "A", "B", "K", "L", "M", "N"]
        idx = [0, 1, 2, 3, 8, 9, 10, 11]
        x = x0
        for nm, i in zip(names, idx, strict=True):
            x = item(x, nm, f"{ram[i]:02X}", ram[i] != old[i])
        y += lh
        x = x0
        for nm, i in (("X", 4), ("Y", 6)):
            v, ov = ram[i] | ram[i + 1] << 8, old[i] | old[i + 1] << 8
            x = item(x, nm, f"{v:04X}", v != ov)
        pc, dp, p, q, r, c, z = b.ptrs()
        opc, odp, op_, oq, orr, oc, oz = b.prev_ptr
        x = item(x, "DP", f"{dp:04X}", dp != odp)
        x = item(x, "PC", f"{pc:04X}", pc != opc)
        y += lh
        x = x0
        x = item(x, "P", f"{p:02X}", p != op_)
        x = item(x, "Q", f"{q:02X}", q != oq)
        x = item(x, "R", f"{r:02X}", r != orr)
        x = item(x, "C", f"{c}", c != oc)
        x = item(x, "Z", f"{z}", z != oz)
        y += lh + 4
        # 見出しをクリックすると、内部RAMの行を開く・閉じる
        mark_s = "▼" if self.show_iram else "▶"
        x = self._text(dst, x0, y, f"{mark_s} " + tr("内部RAM"), HEAD, self.jfont)
        if self.show_iram:
            note = tr("(00〜0Bは I J A B XL XH YL YH K L M N)")
        else:
            note = tr("(クリックで開く)")
        x = self._text(dst, x + 8, y + 1, note, DIM, self.jfont)
        self.hits.append((pygame.Rect(x0, y, x - x0, lh), "iram", 0))
        y += lh + 2
        if not self.show_iram:
            return y
        for row in range(self.IRAM_ROWS):
            base = row * 16
            self._text(dst, x0, y, f"{base:02X}", DIM)
            for i in range(16):
                a = base + i
                color = REGCHG if mark and ram[a] != old[a] else (FG if a < 0x5C else DIM)
                if a == cpu.r and b.ran:  # スタックの頭
                    box = (x0 + (3 + 3 * i) * cw - 2, y, 2 * cw + 4, lh - 2)
                    pygame.draw.rect(dst, (70, 60, 90), box)
                self._text(dst, x0 + (3 + 3 * i) * cw, y, f"{ram[a]:02X}", color)
            y += lh
        return y

    def _status(self, dst, x0: int, y: int, width: int) -> None:
        b = self.b
        if self.flash and time.monotonic() < self.flash_until:
            text, color = self.flash, OK if self.flash_ok else WARN
        elif b.reason:
            text, color = b.reason, OK if b.state == "edit" else REGCHG
        else:
            text, color = self.hint(), DIM
        # 作業欄の幅で折り返し、下のあいたところに入る2行まで出す(入らなければ…で切る)
        lines = wrap_text(self.jfont, text, width) if text else []
        if len(lines) > self.STATUS_LINES:
            last = lines[self.STATUS_LINES - 1]
            while last and self.jfont.size(last + "…")[0] > width:
                last = last[:-1]
            lines = lines[: self.STATUS_LINES - 1] + [last + "…"]
        for k, t in enumerate(lines):
            self._text(dst, x0, y + 20 * k, t, color, self.jfont)

    def hint(self) -> str:
        b, c = self.b, self.cmd
        if b.start_buf is not None:
            return tr("開始番地を16進表記4桁で打つ  {c}Vで貼り付け  Escでやめる", c=c)
        if b.rom and b.state == "run":
            if not self.focus:
                return tr("作業欄をクリックすると、キーで作業欄のダンプを書けます")
            return tr(
                "16進表記で打つ  {c}.で一時停止  {c}Bで止める所  本体のクリックでキーは本体へ", c=c
            )
        if b.rom and b.state == "pause":
            return tr("{c}Gで続ける  {c}Jで次の1命令だけ実行  {s}{c}JでCALLごと", c=c, s=self.shift)
        if b.state == "run":
            return tr("実行中です。キーはPC-1251へ(EscはBRK)。{c}.で一時停止", c=c)
        if b.state == "pause":
            return tr("{c}Gで続ける  {c}Jで次の1命令だけ実行  Escで編集に戻る", c=c)
        if b.field == "addr":
            return tr("番地を4桁打ってSpace。続けて8バイトを打ってEnter(本のダンプリストと同じ)")
        return tr(
            "16進表記で打つ  Enterで次の行  {c}Gで実行  {c}Aでカーソルの所を開始番地に  "
            "{c}Vで貼り付け",
            c=c,
        )

    def hit(self, pos) -> tuple[str, int] | None:
        for i, btn in enumerate(self.buttons):
            if btn.rect.collidepoint(pos):
                return ("button", i)
        for r, kind, a in self.hits:
            if r.collidepoint(pos):
                return (kind, a)
        return None

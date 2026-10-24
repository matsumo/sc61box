"""PC-1251/PC-1245/PC-1261/PC-1350エミュレータの画面とキー操作

uv run pc1251                 # 起動
uv run pc1251 --model 1245    # PC-1245として起動する
uv run pc1251 --model 1261    # PC-1261として起動する(1260、1262も選べる)
uv run pc1251 --model 1350 --card 16  # PC-1350として、16KBのRAMカードを差して起動する
uv run pc1251 --type prog.bas # 起動後にファイルの中身をキー入力する
uv run pc1251 --load dump.txt # 16進ダンプをRAMへ書き込む
uv run pc1251 primes --run    # 置き場所のprimes.basを読み込んで実行する
"""

import json
import os
import re
import subprocess
import sys
import time
import wave
from types import SimpleNamespace
from typing import Any

import pygame
import typer

from . import audio, basictext, display, keytype, lcdtext, programs, recorder, tape, workbench
from .audio import RATE, Buzzer, speed_adjust
from .autotype import Typer
from .keytype import keys_for, keys_for_program
from .machine import CLOCK, MODELS, RomNotFound, new_machine
from .overlay import (
    BAR_H,
    SEPARATOR,
    Bar,
    BarButton,
    MenuItem,
    Paper,
    Popup,
    ProgramMenu,
    draw_halted,
    draw_help,
    draw_recording,
    draw_status,
    load_help_font,
)

FPS = 30
RUSH = 8  # パソコンのキーをROMの入力待ちに届けるあいだの速さ(Typer.rushing)
STATE_DIR = os.path.expanduser("~/.pc1251")  # RAM、状態、作業欄の見え方などを置くところ


UNDO_HINT = "メニューの「読み込む前に戻す」で元に戻せます"


def ram_file(model: str = "1251", card: int = 0, norom: bool = False) -> str:
    """終了時にRAMを保存するファイル。RAMの大きさが違うので機種(とRAMカード)ごとに分ける。
    ROMなしで動かしたときのRAMは、ROMで動かしたときのRAMと混ざらないように別にする"""
    if card:
        model = f"{model}-{card}k"
    if norom:
        return os.path.join(STATE_DIR, f"ram-{model}-norom.bin")
    name = "ram.bin" if model == "1251" else f"ram-{model}.bin"
    return os.path.join(STATE_DIR, name)


def bench_file(ram: str) -> str:
    """終了したときに、RAMと一緒に作業欄の情報(開始番地、止める所、書いた範囲)を保存する
    ファイル"""
    return os.path.splitext(ram)[0] + "-bench.json"


def view_file() -> str:
    """作業欄の見え方(内部RAMを開いておくか)を覚えておくファイル"""
    return os.path.join(STATE_DIR, "bench-view.json")


def state_file(model: str = "1251", card: int = 0, norom: bool = False) -> str:
    """どこでもセーブの保存先(機種とRAMカードごとに1つ。ROMなしは別)"""
    name = f"{model}-{card}k" if card else model
    return os.path.join(STATE_DIR, f"state-{name}{'-norom' if norom else ''}.json")


# そのまま押しっぱなしで伝えるキー
DIRECT = {
    pygame.K_RETURN: "ENTER",
    pygame.K_KP_ENTER: "ENTER",
    pygame.K_SPACE: "SPC",
    pygame.K_UP: "UP",
    pygame.K_DOWN: "DOWN",
    pygame.K_LEFT: "LEFT",
    pygame.K_RIGHT: "RIGHT",
    pygame.K_TAB: "SHIFT",
    pygame.K_LALT: "DEF",
    pygame.K_RALT: "DEF",
    pygame.K_DELETE: "CL",
}
# PC-1350で名前の違うキー。DeleteとInsertはDELとINS、HomeはCLS。
# カナキーはパソコンにないので、F6の隣のF8に置く
DIRECT_1350 = {
    pygame.K_DELETE: "DEL",
    pygame.K_INSERT: "INS",
    pygame.K_HOME: "CLS",
    pygame.K_F8: "KANA",
}
DIGITS = {}
for _i in range(10):
    DIGITS[getattr(pygame, f"K_{_i}")] = str(_i)
    DIGITS[getattr(pygame, f"K_KP{_i}")] = str(_i)
LETTERS = {getattr(pygame, f"K_{_c}"): _c.upper() for _c in "abcdefghijklmnopqrstuvwxyz"}
# 矢印キーを数字キーの8・2・4・6として押すモード(テンキーのないパソコン用)
NUMPAD_ARROWS = {pygame.K_UP: "8", pygame.K_DOWN: "2", pygame.K_LEFT: "4", pygame.K_RIGHT: "6"}
# 記号はキーボードの配列で位置が違うので、打たれた文字(TEXTINPUT)で受けてキー操作に直す

MAC = sys.platform == "darwin"
CMD = "⌘" if MAC else "Ctrl+"  # 画面に出すショートカットの修飾キー
SHIFT = "⇧" if MAC else "Shift+"


def speed_label(speed: float) -> str:
    return "×1/2" if speed == 0.5 else f"×{speed}"


# キーの説明。パソコンのキーとポケコンのキーで対応が違うものだけを書く(英数字、記号、Enter、
# 矢印、Spaceはそのまま打てるので書かない)。エミュレータの操作は操作欄のメニューに出る
HELP = [
    "パソコンのキーとポケコンのキーで違うもの(ほかのキーはそのまま打てます)",
    "Tab: SHIFT   Option(WindowsはAlt): DEF   Delete: CL   Esc: BRK(電源が切れていればON)",
    "Backspace: 1字消す(◀とSHIFT+◀を押す)   \\(バックスラッシュ): ¥",
    f"F1〜F4・{CMD}↑↓: モードスイッチ   F5・{CMD}R: 裏のRESETボタン",
    f"{CMD}K: 矢印キーを数字キーの8・2・4・6にする・戻す(テンキーの代わり)",
    "エミュレータの操作は下の操作欄のメニューから(ショートカットはメニューに出ます)",
]

HELP_NOROM = [
    "ROMなしの作業欄(右)",
    "0〜9・A〜F: ダンプを書く   Enter: 次の行   Space: 番地からバイトへ   Tab: 番地を打ち直す",
    f"{CMD}G: 実行・続ける   {SHIFT}{CMD}G: カーソルから実行   {CMD}.: 一時停止   Esc: 編集に戻る",
    f"{CMD}J: 次の1命令だけ実行   {SHIFT}{CMD}J: CALLの呼び先を戻るまで実行   {CMD}B: 止める所",
    f"{CMD}V: 本のダンプリストや.hexを貼り付け",
    f"PageUp・PageDown: 1画面   Home・End({CMD}←→): 行の頭・終わり   {CMD}↑↓: 開始番地・終わり",
    f"{CMD}E: 開始番地からを.hexに書き出す   {CMD}O: プログラムの一覧(.hexのマシン語だけを使う)",
    "実行中のキーは本体へ(Tab: SHIFT  Option: DEF  Delete: CL  Esc: BRK)",
]

HELP_1350 = [
    "パソコンのキーとポケコンのキーで違うもの(ほかのキーはそのまま打てます)",
    "Tab: SHIFT   Option(WindowsはAlt): DEF   F8: カナ   Esc: BRK(電源が切れていればON)",
    "Delete: DEL   Insert: INS   Home: CLS   Backspace: 1字消す(◀とDELを押す)   \\: ¥",
    f"F1・F2: MODEキーでRUN・PROにする   F3・F4・{CMD}↑↓: 電源スイッチ   F5・{CMD}R: RESET",
    f"{CMD}K: 矢印キーを数字キーの8・2・4・6にする・戻す(テンキーの代わり)",
    "エミュレータの操作は下の操作欄のメニューから(ショートカットはメニューに出ます)",
]


def export_program(m, folder: str, prog: "programs.Program | None" = None) -> str | None:
    """BASICのプログラムを「名前-日時.bas」に書き出してパスを返す。なければNone

    progは最後に読み込んだプログラム。その名前を使い、組になっていたダンプなどの指示を
    書き写す(RAMのマシン語は書き出さない)。機種のフォルダにあったものなら、folderの中の
    同じ名前のフォルダに書く。
    """
    body = basictext.program_text(m.mem, start=m.program_start())
    if not body:
        return None
    name = prog.name if prog else "program"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    when = time.strftime("%Y-%m-%d %H:%M")
    head = [f"# title: {prog.title if prog else 'BASIC'}({when}の書き出し)"]
    if prog:
        if prog.model:
            head.append(f"# model: {prog.model}")
        head += [f"# {link}" for link in prog.links]
        for key in ("setup", "run", "help"):
            head += [f"# {key}: {line}" for line in getattr(prog, key).split("\n") if line]
        if prog.mode:
            head.append(f"# mode: {prog.mode}")
    where = programs.folder_model(prog.paths[0]) if prog and prog.paths else ""
    if where:
        folder = os.path.join(folder, programs.model_dir(where))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}-{stamp}.bas")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head) + "\n" + body)
    return path


def parse_hexdump(path: str) -> list[tuple[int, bytes]]:
    """16進ダンプのファイルを読む(形はprograms.parse_hexと同じ)"""
    return programs.parse_hex(open(path, encoding="utf-8").read())[0]


def _unused(folder: str, stem: str, ext: str) -> str:
    """folderの中に、まだないファイル(stem+ext、あればstem-2+extなど)を空で作って名前を返す。
    作るのと確かめるのを1度に行うので、ほかのエミュレータと同じ名前を取り合わない"""
    path, n = os.path.join(folder, stem + ext), 2
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            path = os.path.join(folder, f"{stem}-{n}{ext}")
            n += 1


def opaque(surf: pygame.Surface) -> pygame.Surface:
    """アルファのない24ビットの写し。窓と同じ形式の絵は、アルファの成分が意味のない値の
    ことがあり、そのままPNGにすると透けてしまう"""
    out = pygame.Surface(surf.get_size(), 0, 24)
    out.blit(surf, (0, 0))
    return out


def _short(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("pc1251-emu")
    except PackageNotFoundError:
        return "?"


class App:
    def __init__(self, args):
        # 頼んだ形式(モノラル44.1kHz)で開き、装置への変換はSDLにまかせる
        # (2チャンネルで開かれると、渡した列が2倍の速さで鳴る)
        pygame.mixer.pre_init(RATE, -16, 1, 512, allowedchanges=0)
        model = getattr(args, "model", "1251")
        card = getattr(args, "card", 0) or 0
        if MODELS[model].family != "1350":
            card = 0
        # ROMがなければ、ROMなしで動かしてマシン語の作業欄を出す(PC-1350はROMなしでは
        # 動かせないので、ここで止まる)
        overseas = bool(getattr(args, "overseas", False))
        norom = bool(getattr(args, "norom", False))
        if not norom:
            try:
                self.m = new_machine(model, card, overseas)
            except RomNotFound:
                if MODELS[model].family == "1350":
                    raise
                norom = True
        if norom:
            self.m = new_machine(model, rom=False)
        self.is1350 = self.m.model.family == "1350"
        self.norom = norom  # ROMなしで動かしているか
        # マシン語の作業欄。ROMなしではいつも右に出し、ROMがあればメニューで出し入れする
        self.bench = workbench.Bench(self.m)
        pygame.init()
        self.bench_view = workbench.BenchView(self.bench, load_help_font(15), CMD, SHIFT)
        self.pane_open = norom or self._view_pref("show_pane") is True
        pygame.display.set_caption(f"SHARP {self.m.model.title}")
        self.out_w, self.out_h = display.out_size(model)
        pane = workbench.PANE_W if self.pane_open else 0
        self.want_scale = args.scale  # 指定された拡大率(画面に入らなければ、これより縮める)
        self.scale = self._fit(args.scale, self.out_w, self.out_h + BAR_H, pane)
        self.body_size = (int(self.out_w * self.scale), int(self.out_h * self.scale))
        self.font: pygame.font.Font | None = None
        # プリンタはいつもつないでおき、印字したときだけ紙を出す
        self.m.connect_printer()
        self.paper = Paper(lcdtext.glyphs(self.m.mem))
        self.paper_open = False
        self.paper_back = 0  # 紙を何行さかのぼって見せているか
        self.printed = 0  # 紙に出した行の数
        # 紙を出してからしまうまでの印字を、1枚の絵として「print-日時.png」に保存する
        self.print_file: str | None = None
        self.print_start = 0  # この紙の最初の行(printer.linesの番号)
        self.print_saved = 0  # 保存したときの行の数
        self.print_time = 0.0  # 最後に印字した時刻
        self.bench_view.show_iram = self._iram_shown()
        self.window = pygame.display.set_mode(self._window_size())
        self.bar = Bar([])
        self.rec_paused = False  # 録画を一時停止しているか
        self.panel = display.Panel(model)
        self.panel.set_fps(FPS, args.persist)
        self.sw_stops = self.panel.switch_stops  # スイッチの段(上から)と、その中心のy
        self.ram_file = ram_file(model, card, norom)
        self.state_file = state_file(model, card, norom)
        self.typer = Typer(self.m, self.set_mode)
        self.typer.on_write = self.write_dump
        self.fast = False  # 読み込み中は時計に合わせず全速で動かす
        self.menu: ProgramMenu | None = None
        self.status = ""  # 画面の下に出す知らせ
        self.tape_out = tape.TapeOut()  # CSAVEの音を見張ってwavに書き出す
        self.status_until = 0.0
        self.loading = ""
        self.load_help = ""  # 読み込み終わりに出す説明(# help:)
        self.undo_state: dict | None = None  # 最後にプログラムを読み込む直前の状態
        self.undo_title = ""
        self.undo_prog: tuple = (None, "", "")  # 読み込む直前の(last_prog, loading, load_help)
        self.undo_bench: dict | None = None  # 読み込む直前の作業欄
        self.load_total = 1
        self.load_typing = False  # 打ち込みで入れているところか(進み具合の表示用)
        self.popup: Popup | None = None  # 右クリックのメニュー
        self.reset_frames = 0  # RESETボタンを押している残りのフレーム
        self.speaker = None if args.mute else audio.open_output()
        self.buzzer = Buzzer()
        self.recorder: recorder.Recorder | None = None
        self.rec_buzzer = Buzzer()  # 録画の音は鳴らす音とは別に作る
        self.overshoot = 0  # 前のフレームで進みすぎたサイクル
        self.rush_spans: list[tuple[int, int]] = []  # このフレームで速く回したサイクルの範囲
        self.turbo: float = 1  # 速さの倍率(SPEEDSのどれか)
        self.numpad_arrows = bool(getattr(args, "numpad", False))
        self.dragging_switch = False
        self.mouse_key: str | None = None
        self.brk_by: set[str] = set()  # BRKキーを押している入力元(EscキーとマウスでBRK)
        self.help = 0  # キーの説明を出しておく残りのフレーム
        if self.norom:
            self.say("ROMがないので、ROMなしでマシン語だけを動かします(右の作業欄)", 8)
        else:
            self.say(f"操作は下の操作欄のボタンから。キーの説明は{CMD}/", 8)
        self.save_ram = not args.fresh
        if self.save_ram and self.m.load_ram(self.ram_file):
            self.m.cpu.reset()
            self.bench.prev_regs = bytes(self.m.cpu.ram)
            self._load_bench_info()
        self.load = args.load
        self.boot_frames = FPS  # 起動してから貼り付けを始めるまで
        if args.type:
            self.typer.add_text(open(args.type, encoding="utf-8").read())
        self.pending: tuple[programs.Program, bool] | None = None
        self.last_prog: programs.Program | None = None  # 書き出しと録画の題名に使う
        if getattr(args, "tape", None):
            self.insert_tape(args.tape)
        if getattr(args, "state", None):
            self.load_state(args.state)
        if getattr(args, "program", None):
            prog = programs.find(args.program, self.m.model.name)
            if prog is None:
                raise FileNotFoundError(f"{args.program}が見つかりません")
            self.pending = (prog, args.run)
        pygame.key.set_repeat()
        self.repeating = False  # pygameのキーのリピートを使っているか(_key_repeat)
        pygame.key.start_text_input()

    @staticmethod
    def _fit(
        scale: float, out_w: int = display.OUT_W, out_h: int = display.OUT_H, pane: int = 0
    ) -> float:
        """拡大率。画面に収まらなければ縮める(paneは右に足す作業欄の幅)"""
        try:
            dw, dh = pygame.display.get_desktop_sizes()[0]
        except (pygame.error, IndexError):
            return scale
        room = min((dw - 80 - pane) / out_w, (dh - 120) / out_h)
        return min(scale, max(0.35, room))

    # ---- キー ----
    def key_down(self, ev) -> None:
        m = self.m
        mods = ev.mod
        if self.menu is not None:
            self.menu_key(ev)
            return
        if self.popup is not None:
            self.popup_key(ev)
            return
        if mods & (pygame.KMOD_META | pygame.KMOD_CTRL) and self.shortcut(ev.key, mods):
            return
        if self.pane_keys():
            # キーを作業欄の打ち込みに使う(本体のキーには送らない)
            if not mods & (pygame.KMOD_META | pygame.KMOD_CTRL):
                self.bench_key(ev)
            return
        actions = {
            pygame.K_F6: self.open_menu,
            pygame.K_F7: self.toggle_arrows,
            pygame.K_F9: lambda: self.change_speed(-1 if mods & pygame.KMOD_SHIFT else 1),
            pygame.K_F10: self.toggle_help,
            pygame.K_F12: self.screenshot,
        }
        if ev.key in actions:
            actions[ev.key]()
            return
        if ev.key == pygame.K_ESCAPE and self.fast:  # 読み込みの途中ならやめる
            self.cancel_loading()
            return
        if ev.key == pygame.K_ESCAPE:
            self.press_brk("key")
            return
        if ev.key == pygame.K_F5:
            m.reset_key = True
            return
        fkeys = {pygame.K_F1: "RUN", pygame.K_F2: "PRO", pygame.K_F3: "RSV", pygame.K_F4: "OFF"}
        if ev.key in fkeys:
            mode = fkeys[ev.key]
            if self.is1350:  # F1・F2はMODEキーでRUN・PROに、F3・F4は電源スイッチ
                mode = "ON" if mode == "RSV" else mode
                if mode in ("RUN", "PRO"):
                    self.typer.add_mode(mode)
                else:
                    self.set_mode(mode)
            elif mode in m.model.modes:  # PC-1245にはRSVがない
                self.set_mode(mode)
            return
        if ev.key == pygame.K_BACKSPACE:
            if self.is1350:
                self.typer.add_live_keys([["LEFT"], ["DEL"]])
            else:
                self.typer.add_live_keys([["LEFT"], ["SHIFT"], ["LEFT"]])
            return
        if mods & (pygame.KMOD_META | pygame.KMOD_CTRL):
            return
        name = self.key_name(ev.key)
        if name is None and not mods & pygame.KMOD_SHIFT:
            name = DIGITS.get(ev.key)  # Shift付きの数字キーは記号になる
        if name:
            self.typer.press_live(name, ev.key)

    def key_name(self, key: int) -> str | None:
        """パソコンのキーを本体のキーの名前にする(数字キーを除く)"""
        if self.numpad_arrows and key in NUMPAD_ARROWS:
            return NUMPAD_ARROWS[key]
        if self.is1350 and key in DIRECT_1350:
            return DIRECT_1350[key]
        return DIRECT.get(key) or LETTERS.get(key)

    def key_up(self, ev) -> None:
        m = self.m
        if ev.key == pygame.K_ESCAPE:
            self.release_brk("key")
        elif ev.key == pygame.K_F5:
            m.reset_key = False
        name = self.key_name(ev.key) or DIGITS.get(ev.key)
        if name:
            self.typer.release_live(name, ev.key)
        if ev.key in NUMPAD_ARROWS:  # 押しているあいだにモードを切り替えたとき
            self.typer.release_live(DIRECT[ev.key], ev.key)
            self.typer.release_live(NUMPAD_ARROWS[ev.key], ev.key)

    def text_input(self, text: str) -> None:
        menu = self.menu
        if menu is not None:  # 一覧では「/」で探し始め、そのあとの字で絞る
            if menu.query is not None:
                menu.search(menu.query + text)
            elif text == "/":
                menu.search("")
            return
        if self.popup is not None:
            return
        if self.pane_keys():
            return  # 作業欄の打ち込みはKEYDOWNで受けている
        # KEYDOWNで押しっぱなしとして伝えた英数字と空白を除いて、まとめて打つ
        rest = "".join(ch for ch in text if not (ch.isascii() and (ch.isalnum() or ch == " ")))
        if rest:
            self.typer.add_live_keys(keys_for(rest, self.m.model.name))

    # ---- ショートカットと右クリックのメニュー ----
    # Fキーはほかのアプリに取られることがあるので、同じ操作を⌘(Windows・LinuxはCtrl)との
    # 組み合わせと右クリックのメニューにも置く
    def shortcut(self, key: int, mods: int = 0) -> bool:
        if (self.norom or self.pane_keys()) and self.bench_shortcut(key, mods):
            return True
        actions = {
            pygame.K_o: self.open_menu,
            pygame.K_t: lambda: self.change_speed(-1 if mods & pygame.KMOD_SHIFT else 1),
            pygame.K_r: self.toggle_recording if mods & pygame.KMOD_SHIFT else self.press_reset,
            pygame.K_p: self.toggle_pause if mods & pygame.KMOD_SHIFT else self.toggle_halt,
            pygame.K_s: self.save_state if mods & pygame.KMOD_SHIFT else self.screenshot,
            pygame.K_l: self.load_state if mods & pygame.KMOD_SHIFT else None,
            pygame.K_e: self.export_program,
            pygame.K_v: self.paste,
            pygame.K_SLASH: self.toggle_help,
            pygame.K_k: self.toggle_arrows,
            pygame.K_d: None if self.norom else self.toggle_pane,
            pygame.K_UP: lambda: self.move_switch(-1),
            pygame.K_DOWN: lambda: self.move_switch(1),
        }
        act = actions.get(key)
        if act is None:
            return False
        act()
        return True

    # ---- マシン語の作業欄(ROMなし) ----
    def bench_shortcut(self, key: int, mods: int) -> bool:
        """作業欄の⌘(Ctrl)のショートカット。扱ったらTrue"""
        shift = bool(mods & pygame.KMOD_SHIFT)
        b, v = self.bench, self.bench_view
        assert b is not None and v is not None
        if b.state != "run" or b.rom:
            # ⌘↑↓(Ctrl+Home・End)で開始番地と書いた所の終わりへ、⌘←→で行の頭と終わりへ
            moves = {
                pygame.K_UP: lambda: b.go(b.start, v.rows),
                pygame.K_HOME: lambda: b.go(b.start, v.rows),
                pygame.K_DOWN: lambda: b.go_end(v.rows),
                pygame.K_END: lambda: b.go_end(v.rows),
                pygame.K_LEFT: b.row_home,
                pygame.K_RIGHT: b.row_end,
            }
            if key in moves:
                moves[key]()
                return True
        actions = {
            pygame.K_g: lambda: self.bench_do("runcursor" if shift else "go"),
            pygame.K_PERIOD: lambda: self.bench_do("pause"),
            pygame.K_j: lambda: self.bench_do("over" if shift else "step"),
            pygame.K_b: lambda: self.bench_do("break"),
            pygame.K_v: self.bench_paste,
            pygame.K_e: self.bench_export,
        }
        act = actions.get(key)
        if act is None:
            return False
        act()
        return True

    def bench_do(self, what: str) -> None:
        """作業欄のボタンとショートカットの操作"""
        b, v = self.bench, self.bench_view
        assert b is not None and v is not None
        if what == "go":
            what = "resume" if b.state == "pause" else "run"
        if what in ("run", "runcursor") and (b.state == "run" or b.rom):
            return  # ROMのある本体では、マシン語はBASICのCALLで呼ぶ
        self.release_all()
        if what == "run":
            b.run()
        elif what == "runcursor":
            b.start = b.cursor
            b.run()
        elif what == "resume":
            b.resume()
        elif what == "pause":
            b.pause()
        elif what == "step":
            b.step()
        elif what == "over":
            b.step_over()
        elif what == "abandon":
            b.abandon()
        elif what == "break":
            a = b.ins_at(b.cursor)
            on = b.toggle_break(a)
            if on is None:
                v.say(f"{a:04X}番地はROMなしでは使えないので、止める所を置けません", 3)
            else:
                v.say(f"{a:04X}番地の止める所を" + ("置きました" if on else "外しました"), 2)
        self.overshoot = 0

    def bench_key(self, ev) -> None:
        """実行していないときのキー。16進表記の字でダンプを書き、矢印で動く"""
        b, v = self.bench, self.bench_view
        assert b is not None and v is not None
        k = ev.key
        if k == pygame.K_ESCAPE:
            if b.rom:  # キーを本体へ戻す
                v.focus = False
            elif b.state == "pause":
                b.abandon()
            return
        ch = (ev.unicode or "").upper()
        msg = ""
        if len(ch) == 1 and ch in "0123456789ABCDEF":
            msg = b.type_hex(int(ch, 16))
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            b.next_line()
        elif k == pygame.K_SPACE:
            b.to_data()
        elif k in (pygame.K_LEFT, pygame.K_BACKSPACE):
            b.left()
        elif k == pygame.K_RIGHT:
            b.right()
        elif k in (pygame.K_UP, pygame.K_DOWN):
            b.move_rows(-1 if k == pygame.K_UP else 1, v.rows)
        elif k in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            b.page(-1 if k == pygame.K_PAGEUP else 1, v.rows)
        elif k == pygame.K_TAB:
            b.to_addr()
        elif k == pygame.K_HOME:
            b.row_home()
        elif k == pygame.K_END:
            b.row_end()
        if msg:
            v.say(msg)

    REPEAT = (350, 40)  # 作業欄のキーのリピート(押してから始まるまで、間隔。ミリ秒)

    def _key_repeat(self) -> None:
        """作業欄にキーを打っているあいだだけ、押し続けたキーをリピートさせる。本体に
        キーを送るとき(ROMで動かしているとき、ROMなしで実行中のとき)は、押したままの
        キーをそのまま伝えるのでリピートしない"""
        want = self.pane_keys() and self.menu is None
        if want != self.repeating:
            if want:
                pygame.key.set_repeat(*self.REPEAT)
            else:
                pygame.key.set_repeat()
            self.repeating = want

    def bench_click(self, pos) -> None:
        b, v = self.bench, self.bench_view
        assert b is not None and v is not None
        hit = v.hit(pos)
        if hit is None:
            return
        kind, a = hit
        if kind == "button":
            self.bench_do(v.buttons[a].action)
        elif kind == "clearlcd":
            b.clear_lcd = not b.clear_lcd
        elif kind == "export":
            self.bench_export()
        elif kind == "iram":
            v.show_iram = not v.show_iram
            self._save_view()
            self.window = pygame.display.set_mode(self._window_size())
        elif kind == "break":
            if b.toggle_break(a) is None:
                v.say(f"{a:04X}番地はROMなしでは使えないので、止める所を置けません", 3)
        elif kind in ("addr", "byte", "goto") and (b.state != "run" or b.rom):
            first = (b.row - 8 * b.top_rows) & 0xFFFF
            off = (a - first) & 0xFFFF
            if kind == "addr":
                b.row, b.top_rows, b.field, b.nib = a, off // 8, "addr", 0
            else:
                b.place(a, v.rows)  # バイトか逆アセンブルの行。行の並びは変えない

    def bench_open(self, prog: programs.Program, run: bool = False) -> None:
        """ROMなしでプログラムを読み込む。マシン語のダンプをRAMに書き、BASICは実行しない。
        呼び出し用のBASICから、数だけを書くPOKE(IFのあとを除く)を拾って書き、最後のCALLの
        番地を開始番地にする(CALLがなければ最初のダンプの番地)"""
        b, m = self.bench, self.m
        assert b is not None
        name = prog.title or prog.name
        if prog.errors:
            self.say(f"{name}を読み込みません。{prog.errors[0]}", 8)
            return
        pokes, start = workbench.basic_hints(f"{prog.basic}\n{prog.run}")
        blocks = prog.blocks + pokes + prog.after
        if not prog.norom_ok:
            why = "BASICとROMのルーチンを使う" if prog.blocks else "BASICの"
            self.say(f"{name}は{why}プログラムなので、ROMなしでは使えません", 6)
            return
        outside = [a for at, data in blocks for a in range(at, at + len(data)) if not m.writable(a)]
        if outside:
            self.say(f"{name}を読み込みません。{outside[0]:04X}番地はRAMの外です", 8)
            return
        self.undo_bench = b.save()  # 実行中・一時停止中なら、その状態のまま覚える
        if b.state != "edit":
            b.abandon()
        self.undo_state, self.undo_title = m.snapshot(), name
        self.undo_prog = (self.last_prog, self.loading, self.load_help)
        n = b.load_blocks(blocks, start)
        self.last_prog = prog
        self.loading, self.load_help = prog.title, prog.norom
        done = f"{name}を読み込みました({n}バイト、開始番地{b.start:04X})。{CMD}Gで実行"
        self.say(f"{done}\n{prog.norom}" if prog.norom else done, 12 if prog.norom else 6)
        if run:
            self.bench_do("run")

    def bench_paste(self) -> None:
        """クリップボードのダンプ(本のダンプリストか.hexの行)を書く"""
        b, v = self.bench, self.bench_view
        assert b is not None and v is not None
        if b.state == "run" and not b.rom:
            return
        res = b.paste(self.clipboard())
        if not res.written:
            v.say("貼り付けた文字にダンプの行がありません", 4)
            return
        msg = f"{res.written}バイトを書きました"
        if res.bad_sums:
            where = "、".join(f"{a:04X}" for a in res.bad_sums[:4])
            msg += f"。チェックサムが合わない行: {where}"
        if res.outside:
            msg += f"。RAMの外の{res.outside}バイトは書けません"
        v.say(msg, 8)

    def bench_export(self) -> None:
        """開始番地からのマシン語を、置き場所に.hex(逆アセンブルつき)で書き出す"""
        b, v = self.bench, self.bench_view
        assert b is not None and v is not None
        ranges = b.export_ranges()
        if not ranges:
            v.say("書き出すダンプがありません", 4)
            return
        where = "、".join(f"{lo:04X}〜{hi - 1:04X}" for lo, hi in ranges)
        try:
            folder = os.path.join(programs.user_dir(), programs.model_dir(self.m.model.name))
            os.makedirs(folder, exist_ok=True)
            path = _unused(folder, f"dump-{time.strftime('%Y%m%d-%H%M%S')}", ".hex")
            # 開始番地は# run:に書く(読み込むと、ROMなしではここから、ROMがあればこのCALLで動く)
            head = f"; {where}(マシン語の作業欄から書き出し)\n# run: CALL &{b.start:04X}\n"
            body = "".join(b.dump_text(lo, hi) for lo, hi in ranges)
            with open(path, "w", encoding="utf-8") as f:
                f.write(head + body)
        except OSError as e:
            v.say(f"書き出せません: {e}", 5)
            return
        # 範囲は作業欄と.hexの頭に出ているので、知らせはファイルの名前と結果を先に、置き場所を
        # あとに書く(作業欄の下の知らせは2行まで)
        size = sum(hi - lo for lo, hi in ranges)
        name = os.path.basename(path)
        v.say(f"{name}を書き出しました({size}バイト)。置き場所: {_short(folder)}", 8)

    def clipboard(self) -> str:
        text = ""
        try:
            if not pygame.scrap.get_init():
                pygame.scrap.init()
            raw = pygame.scrap.get(pygame.SCRAP_TEXT)
            if raw:
                text = raw.decode("utf-8", "ignore").rstrip("\x00")
        except (pygame.error, NotImplementedError):
            pass
        if not text:
            try:
                text = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
            except OSError:
                text = ""
        return text

    SPEEDS = (0.5, 1, 2, 4, 8)

    def change_speed(self, step: int) -> None:
        """速さを1段上げる(step=1)か下げる(step=-1)。両端ではそのまま"""
        i = self.SPEEDS.index(self.turbo) + step
        self.turbo = self.SPEEDS[min(max(i, 0), len(self.SPEEDS) - 1)]
        self.say(f"速さ {speed_label(self.turbo)}", 1.5)

    def toggle_arrows(self) -> None:
        self.numpad_arrows = not self.numpad_arrows
        if self.numpad_arrows:
            self.say("矢印キー → 数字キーの8・2・4・6", 2.5)
        else:
            self.say("矢印キー → ↑↓◀▶", 2.5)

    def toggle_help(self) -> None:
        self.help = 0 if self.help else FPS * 10

    # ---- 操作欄とプリンタ ----
    def _window_size(self) -> tuple[int, int]:
        """窓の大きさ。本体の下に、プリンタの紙(印字したとき)と操作欄を足す"""
        w, h = self.body_size
        if self.paper_open:
            h += self.paper.height
        h += BAR_H
        if self.pane_open:
            return w + workbench.PANE_W, max(h, self.bench_view.min_height())
        return w, h

    def bar_buttons(self) -> list[BarButton]:
        """操作欄のボタン。左に目的ごとのメニュー、続けて実行の一時停止と速さ、右端に録画。
        メニューにある操作をボタンとして重ねては置かない(録画はこのボタンだけで扱う)。
        録画しているあいだは、録画のボタンが録画中の印と経過時間になり、押すと、止めるか
        一時停止するかのメニューがそのすぐ上に出る(実行の一時停止とは別の場所に置く)"""

        def menu(kind: str):
            return lambda: self.open_bar_popup(kind)

        buttons = [
            BarButton("プログラム", menu("program"), short="プログラム", menu="program",
                      tip=f"一覧、書き出し、貼り付け(一覧は{CMD}O)"),
            BarButton("本体", menu("machine"), short="本体", menu="machine",
                      tip="状態の保存と復元、スイッチ、RESET"),
            BarButton("表示・入力", menu("view"), short="表示", menu="view",
                      tip=f"キーの説明({CMD}/)、矢印キーの割り当て、画面の画像、印字"),
        ]  # fmt: skip
        if self.norom:
            pass  # ROMなしでは、実行と一時停止は作業欄のボタンで行う
        elif self.halted:
            tip = f"止めている実行を続ける({CMD}P)"
            buttons.append(BarButton("実行を再開", self.toggle_halt, "play", "再開", tip))
        else:
            tip = f"CPUの実行を止める({CMD}P)。録画は止まりません"
            buttons.append(BarButton("実行を一時停止", self.toggle_halt, "pause", "一時停止", tip))
        speed = speed_label(self.turbo)
        tip = f"実行の速さを選ぶ({CMD}Tで速く、{SHIFT}{CMD}Tで遅く)"
        buttons.append(BarButton(f"速さ {speed}", menu("speed"), "", speed, tip, True, "speed"))
        rec = self.recorder
        if rec is None:
            tip = f"録画を始める({SHIFT}{CMD}R)"
            buttons.append(BarButton("録画", self.toggle_recording, "rec", "録画", tip))
        else:
            sec = int(rec.seconds)
            clock = f"{sec // 60}:{sec % 60:02d}"
            tip = f"録画を止める({SHIFT}{CMD}R)か、一時停止する({SHIFT}{CMD}P)"
            if self.rec_paused:
                label, icon = f"録画を一時停止中 {clock}", "recpause"
            else:
                label, icon = f"録画中 {clock}", "rec"
            buttons.append(BarButton(label, menu("rec"), icon, clock, tip, menu="rec"))
        return buttons

    def bar_info(self) -> str:
        """操作欄の右に出す、いまの設定(速さと一時停止はボタンに出る)"""
        m = self.m
        parts = [m.model.title + (f" {m.card}KBカード" if getattr(m, "card", 0) else "")]
        if self.last_prog is not None:
            parts.append(self.last_prog.title or self.last_prog.name)
        if self.numpad_arrows:
            parts.append("矢印→8246")
        if m.tape is not None:
            parts.append("テープ")
        return "   ".join(parts)

    def cancel_loading(self) -> None:
        """読み込みの打ち込みをやめる。打ち込んだところまではRAMに残る"""
        self.typer.clear()
        self.fast = False
        self.say(f"読み込みをやめました(途中まで入っています)。{UNDO_HINT}", 6)

    def show_paper(self, on: bool) -> None:
        """プリンタの紙を出すかしまう。窓の高さが変わる。しまうときは、まだ保存していない
        印字を保存して、その紙を終わりにする(次の印字は新しい紙とファイルになる)"""
        if on == self.paper_open:
            return
        if not on:
            self.save_printout()
            self.print_file = None
        self.paper_open = on
        self.paper_back = 0
        self.window = pygame.display.set_mode(self._window_size())

    def paper_rows(self) -> list[str]:
        """いまの紙に印字した行(途中の行も含める)"""
        printer = self.m.printer
        if printer is None:
            return []
        rows = printer.lines[self.print_start :]
        return rows + ([printer.line.rstrip()] if printer.line.strip() else [])

    def check_printer(self) -> None:
        """新しく印字した行があれば、紙を出して送る。印字が1秒とぎれたら保存する"""
        printer = self.m.printer
        if printer is None:
            return
        if printer.count != self.printed:
            new, self.printed = printer.count - self.printed, printer.count
            started = not self.paper_open
            if started:  # 新しい紙。ほかの紙のファイルと重ならない名前にする
                self.print_start = len(printer.lines) - new
                self.print_saved = 0
                try:
                    self.print_file = _unused(
                        programs.user_dir(), f"print-{time.strftime('%Y%m%d-%H%M%S')}", ".png"
                    )
                except OSError as e:  # 紙は出すが、印字の絵は保存しない
                    self.print_file = None
                    self.say(f"印字を保存できません: {e}", 5)
                self.show_paper(True)
            self.paper_back = 0
            self.paper.fed(new)
            self.print_time = time.monotonic()
            if started:  # 名前を取っておくため、すぐに1度保存する
                self.save_printout()
        elif self.print_file and time.monotonic() - self.print_time > 1.0:
            self.save_printout()

    def save_printout(self) -> None:
        """いまの紙の印字を、感熱紙の絵にしてprint_fileに保存する(前に保存したあとに
        印字していなければ何もしない)"""
        rows = self.paper_rows()
        if not self.print_file or not rows or len(rows) == self.print_saved:
            return
        img = self.paper.picture(rows)
        img = pygame.transform.scale(img, (img.get_width() * 2, img.get_height() * 2))
        try:
            pygame.image.save(img, self.print_file)
        except (OSError, pygame.error) as e:
            self.say(f"印字を保存できません: {e}", 5)
            self.print_file = None
            return
        self.print_saved = len(rows)
        self.say(f"印字を{_short(self.print_file)}に保存しました", 5)

    def reveal_printout(self) -> None:
        """印字を保存したフォルダを開く"""
        self.save_printout()
        self.reveal(os.path.dirname(self.print_file) if self.print_file else programs.user_dir())

    def scroll_paper(self, step: int) -> None:
        """紙の上でホイールを回すと、前の行を見せる"""
        printer = self.m.printer
        y = pygame.mouse.get_pos()[1]
        h = self.body_size[1]
        if printer is None or not self.paper_open or not h <= y < h + self.paper.height:
            return
        rows = len(self.paper_rows())
        self.paper_back = min(max(self.paper_back + step, 0), max(0, rows - 1))

    def press_reset(self) -> None:
        """裏のRESETボタンを0.3秒押す。ROMなしでは、実行をやめて作業欄に戻る"""
        if self.norom:
            self.bench_do("abandon")
            return
        self.m.reset_key = True
        self.reset_frames = max(1, FPS * 3 // 10)

    def move_switch(self, step: int) -> None:
        order = [name for name, _y in self.sw_stops]
        i = order.index(self.m.mode) if self.m.mode in order else order.index(order[-2])
        self.set_mode(order[min(max(i + step, 0), len(order) - 1)])

    def popup_items(self, kind: str | None = None) -> list[MenuItem]:
        """メニューの項目。kindは操作欄のボタンのメニュー(program、machine、view、speed、
        録画中のrec)か、右クリックのメニュー(context、paper)。なければいま出しているメニュー"""
        if kind is None:
            kind = self.popup.kind if self.popup is not None else "context"
        sep, item = SEPARATOR, MenuItem
        stopped = self.bench.state == "pause"
        pause = item("実行を再開する" if stopped else "実行を一時停止する", f"{CMD}P",
                     self.toggle_halt)  # fmt: skip
        if kind == "program":
            # ROMなしでは、⌘E・⌘Vと同じく作業欄のダンプを書き出す・貼り付ける
            if self.norom:
                export = item("ダンプをファイル(.hex)に書き出す", f"{CMD}E", self.bench_export,
                              self.bench.export_summary())  # fmt: skip
                paste = item("クリップボードからダンプを貼り付ける", f"{CMD}V", self.bench_paste)
            else:
                export = item("プログラムをファイルに書き出す", f"{CMD}E", self.export_program)
                paste = item("クリップボードから打ち込む", f"{CMD}V", self.paste)
            items = [
                item("プログラムの一覧…", f"{CMD}O", self.open_menu),
                export,
                paste,
                item("置き場所のフォルダを開く", "", lambda: self.reveal(programs.user_dir())),
            ]
            if self.load_help:
                show = lambda: self.say(self.load_help, 20)  # noqa: E731
                items += [sep, item("読み込んだプログラムの説明", "", show, self.loading)]
            return items
        if kind == "machine":
            items = [
                item("いまの状態を保存(どこでもセーブ)", f"{SHIFT}{CMD}S", self.save_state),
                item("保存した状態に戻す", f"{SHIFT}{CMD}L", self.load_state, self.saved_when()),
            ]
            if self.undo_state is not None:
                note = f"{self.undo_title}を読み込む前"
                items.append(item("プログラムを読み込む前に戻す", "", self.undo_loading, note))
            items.append(sep)
            for name, _y in self.sw_stops:
                key = f"{CMD}↑↓" if self.m.mode == name else ""
                items.append(item(f"スイッチ {name}", key, lambda n=name: self.set_mode(n)))
            return items + [sep, item("RESETボタンを押す", f"{CMD}R", self.press_reset)]
        if kind == "view":
            arrows = "矢印キーを↑↓◀▶に戻す" if self.numpad_arrows else "矢印キーを8・2・4・6にする"
            items = [
                item("キーの説明", f"{CMD}/", self.toggle_help),
                item(arrows, f"{CMD}K", self.toggle_arrows),
                sep,
                item("画面を画像で保存", f"{CMD}S", self.screenshot),
            ]
            if not self.norom:
                pane = "マシン語の作業欄をしまう" if self.pane_open else "マシン語の作業欄を出す"
                items[2:2] = [item(pane, f"{CMD}D", self.toggle_pane)]
            paper = self.popup_items("paper")
            return items + ([sep, *paper] if paper else [])
        if kind == "rec":
            if self.rec_paused:
                pause = item("録画を再開する", f"{SHIFT}{CMD}P", self.toggle_pause, icon="rec")
            else:
                pause = item("録画を一時停止する", f"{SHIFT}{CMD}P", self.toggle_pause,
                             icon="recpause")  # fmt: skip
            stop = item("録画を止めて保存", f"{SHIFT}{CMD}R", self.toggle_recording, icon="stop")
            return [stop, pause]
        if kind == "speed":
            return [item(f"速さ {speed_label(v)}", "", lambda v=v: self.set_speed(v))
                    for v in self.SPEEDS]  # fmt: skip
        if kind == "paper":
            items = []
            if self.print_file:
                items.append(item("印字の保存先を開く", "", self.reveal_printout))
            if self.paper_open:
                items.append(item("プリンタの紙をしまう", "", lambda: self.show_paper(False)))
            return items
        return [  # 右クリック(場所によらない、よく使うものだけ)
            pause,
            item("プログラムの一覧…", f"{CMD}O", self.open_menu),
            item("画面を画像で保存", f"{CMD}S", self.screenshot),
            item("キーの説明", f"{CMD}/", self.toggle_help),
        ]

    def popup_mark(self) -> str:
        """いま出しているメニューで●をつける項目(スイッチの位置か、いまの速さ)"""
        kind = self.popup.kind if self.popup is not None else ""
        if kind == "speed":
            return f"速さ {speed_label(self.turbo)}"
        return f"スイッチ {self.m.mode}"

    def set_speed(self, speed: float) -> None:
        self.turbo = speed
        self.say(f"速さ {speed_label(speed)}", 1.5)

    def saved_when(self) -> str:
        """保存した状態の日時(「10/22 15:30に保存」)。保存していなければそう書く"""
        try:
            t = os.path.getmtime(self.state_file)
        except OSError:
            return "まだ保存していません"
        return time.strftime("%m/%d %H:%M", time.localtime(t)).lstrip("0") + "に保存"

    def toggle_halt(self) -> None:
        """実行を一時停止する。止めているあいだはCPUも時計も進まない(録画の一時停止とは別)。
        ROMなしでは、作業欄の一時停止と続けるにする(止めた状態を1つにする)"""
        state = self.bench.state
        if state == "run":
            self.bench_do("pause")
            if not self.norom:
                self.say(f"実行を一時停止しました。{CMD}Pか操作欄の「実行を再開」で続けます", 4)
        elif state == "pause":
            self.bench_do("resume")
            if not self.norom and self.bench.state == "run":
                self.say("実行を再開しました", 1.5)
        else:
            self.say(f"実行していません。{CMD}Gで開始番地から実行します", 3)

    @property
    def halted(self) -> bool:
        """ROMのある本体で、実行を一時停止しているか(作業欄の一時停止と同じ状態)。ROMなしの
        本体の実行と一時停止は作業欄が扱うので、いつもFalse"""
        return not self.norom and self.bench.state == "pause"

    @halted.setter
    def halted(self, on: bool) -> None:
        if not self.norom and on != self.halted:
            self.toggle_halt()

    def undo_loading(self) -> None:
        """プログラムを読み込む直前の状態に戻す"""
        state, title = self.undo_state, self.undo_title
        if state is None:
            return
        try:
            self.m.restore(state)
        except ValueError as e:
            self.say(f"読み込む前に戻せません: {e}", 5)
            return
        # 書き出しや説明に使う、読み込んだプログラムの情報も読み込む前のものに戻す
        self.last_prog, self.loading, self.load_help = self.undo_prog
        self.bench.restore(self.undo_bench, f"{title}を読み込む前に戻しました")
        self.undo_state = None
        self._restored(f"{title}を読み込む前に戻しました")

    def open_bar_popup(self, kind: str) -> None:
        """操作欄のボタンのメニューを、そのボタンのすぐ上に出す"""
        i = next((i for i, b in enumerate(self.bar.buttons) if b.menu == kind), -1)
        r = self.bar.rects[i] if 0 <= i < len(self.bar.rects) else None
        if r is None or not r.width:
            r = pygame.Rect(8, self.body_size[1], 0, 0)
        self.popup = Popup((r.x, r.y), kind, above=True, source=i)

    def next_bar_popup(self, step: int) -> None:
        """開いている操作欄のメニューから、左右の隣のメニューのボタンへ移る(←→)"""
        popup = self.popup
        assert popup is not None
        menus = [i for i, b in enumerate(self.bar.buttons) if b.menu and self.bar.rects[i].width]
        if popup.source in menus:
            i = menus[(menus.index(popup.source) + step) % len(menus)]
            self.open_bar_popup(self.bar.buttons[i].menu)

    def open_popup(self, pos) -> None:
        """右クリックのメニュー。プリンタの紙の上なら紙の操作、ほかはよく使う操作だけ"""
        on_paper = self.paper_open and self.body_size[1] <= pos[1] < self._paper_bottom()
        if on_paper and self.popup_items("paper"):
            self.popup = Popup(pos, "paper")
        else:
            self.popup = Popup(pos, "context")

    def _paper_bottom(self) -> int:
        return self.body_size[1] + (self.paper.height if self.paper_open else 0)

    def popup_run(self, i: int) -> None:
        items = self.popup_items()
        act = items[i].action if 0 <= i < len(items) else None
        self.popup = None
        if callable(act):
            act()

    def popup_key(self, ev) -> None:
        popup = self.popup
        assert popup is not None
        if ev.key == pygame.K_ESCAPE:
            self.popup = None
        elif ev.key in (pygame.K_UP, pygame.K_DOWN):
            popup.move(-1 if ev.key == pygame.K_UP else 1, self.popup_items())
        elif ev.key in (pygame.K_LEFT, pygame.K_RIGHT) and popup.source >= 0:
            self.next_bar_popup(-1 if ev.key == pygame.K_LEFT else 1)
        elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and popup.sel >= 0:
            self.popup_run(popup.sel)

    # ---- マウス ----
    def mouse_down(self, pos) -> None:
        if self.menu is not None:
            self.menu_click(pos)
            return
        if self.pane_open and self.popup is None and pos[0] >= self.body_size[0]:
            self.bench_view.focus = True  # ROMのある本体では、キーを作業欄で受ける
            self.bench_click(pos)
            return
        self.bench_view.focus = False
        if self.popup is not None:
            popup = self.popup
            i = popup.hit(pos)
            if i >= 0:
                self.popup_run(i)
                return
            self.popup = None
            # 操作欄のほかのボタンなら、閉じたうえでそのボタンの操作をする(同じボタンなら
            # 閉じるだけ)
            b = self.bar.hit(pos) if pos[1] >= self.body_size[1] else -1
            if b < 0 or b == popup.source:
                return
        if pos[1] >= self.body_size[1]:  # 本体の下の紙と操作欄
            i = self.bar.hit(pos)
            if i >= 0:
                self.bar.buttons[i].action()
            elif self.paper_open and self.paper.close_rect.collidepoint(pos):
                self.show_paper(False)
            elif self.paper_open and self.paper.where_rect.collidepoint(pos):
                self.reveal_printout()
            return
        x = pos[0] / self.scale - display.MARGIN
        y = pos[1] / self.scale - display.MARGIN
        sx0, sy0, sx1, sy1 = self.panel.switch_area
        if sx0 <= x <= sx1 and sy0 <= y <= sy1:
            self.dragging_switch = True
            self.set_switch_from(y)
            return
        name = self.panel.key_at(x, y)
        if name is None:
            return
        self.mouse_key = name
        if name == "BRK":
            self.press_brk("mouse")
        else:
            self.typer.press_mouse(name)

    def press_brk(self, source: str) -> None:
        """BRKキーを押す。sourceはEscキー("key")か、本体の絵のBRKをマウスで("mouse")"""
        self.brk_by.add(source)
        self.m.brk = True

    def release_brk(self, source: str) -> None:
        """BRKキーを離す。もう一方の入力元でまだ押していれば、押したままにする"""
        self.brk_by.discard(source)
        self.m.brk = bool(self.brk_by)

    def mouse_up(self) -> None:
        self.dragging_switch = False
        name, self.mouse_key = self.mouse_key, None
        if name == "BRK":
            self.release_brk("mouse")
        elif name:
            self.typer.release_mouse(name)

    def set_switch_from(self, y: float) -> None:
        mode = min(self.sw_stops, key=lambda s: abs(s[1] - y))[0]
        self.set_mode(mode)

    def set_mode(self, new: str) -> None:
        """スイッチを動かす。PC-1350のスイッチは電源だけで、RUNとPROはMODEキーで切り替える"""
        m = self.m
        if self.is1350:
            if new == "OFF":
                m.power_off()
            elif new == "ON":
                m.power_on()
            elif new in ("RUN", "PRO"):
                if not m.power:
                    m.power_on()
                self.typer.add_mode(new)
            return
        # OFFにしたときはROMが自分で電源を切るので、ここでは切らない
        if new != "OFF" and (m.mode == "OFF" or not m.power):
            m.wake()
        m.mode = new

    # ---- プログラムの読み込み ----
    def open_program(self, prog: programs.Program, run: bool = False) -> None:
        """ダンプをRAMに書き、BASICはPROモードでNEWしてから入れる。runなら実行の命令も打つ。
        読めない行があるときや、RAMの外に書くダンプがあるときは、何もせずに知らせる"""
        if self.norom:
            self.bench_open(prog, run)
            return
        m = self.m
        if not prog.runs_on(m.model.name) and not prog.fits(m.writable):
            self.say(f"{prog.label(m.model.name)}なので、{m.model.title}では読み込みません", 5)
            return
        if prog.errors:
            self.say(f"{prog.title or prog.name}を読み込みません。{prog.errors[0]}", 8)
            return
        dumps = prog.blocks + prog.after
        outside = [a for at, data in dumps for a in range(at, at + len(data)) if not m.writable(a)]
        if outside:
            self.say(
                f"{prog.title or prog.name}を読み込みません。{outside[0]:04X}番地はRAMの外です", 8
            )
            return
        long = keytype.too_long(prog.basic, m.model.name) if prog.basic else None
        if long is not None:
            hit = re.match(r"\s*(\d+)", long)
            num = hit[1] if hit else ""
            where = f"{num}行" if num else f"「{long.strip()[:16]}」の行"
            self.say(
                f"{prog.title or prog.name}を読み込みません。{where}が長すぎて、打ち込むと"
                "欠けます(行番号と中身で79字まで)",
                8,
            )
            return
        data = self._encoded(prog.basic) if prog.basic and not prog.setup else None
        if data is not None and len(data) > m.program_room():
            self.say(
                f"{prog.title or prog.name}を読み込みません。BASICが{len(data)}バイトで、"
                f"RAMに入りません(使えるのは{m.program_room()}バイト)",
                8,
            )
            return
        if not prog.blocks and not prog.basic and not prog.after:
            self.say(f"{prog.name}: 中身がありません", 4)
            return
        self.undo_state, self.undo_title = m.snapshot(), prog.title or prog.name
        self.undo_prog = (self.last_prog, self.loading, self.load_help)
        self.undo_bench = self.bench.save()
        # 作業欄の開始番地は、呼び出し用のBASICの最後のCALLの番地(なければ最初のダンプの
        # 番地)にする。書いた範囲は、ダンプを実際にRAMに書いたとき(write_dump)に覚える
        _pokes, call = workbench.basic_hints(f"{prog.basic}\n{prog.run}")
        self.bench.set_start(prog.blocks + prog.after, call)
        if self.is1350 and not m.power:
            self.set_mode("ON")
        elif not m.power:
            self.set_mode(m.mode if m.mode != "OFF" else "RUN")
        if not prog.setup:
            self.write_dump(prog.blocks)
        back = m.mode if m.mode in ("RUN", "PRO") else "RUN"
        if self.is1350:
            back = "PRO" if "PRO" in m.symbols() else "RUN"
        if prog.basic or prog.setup:
            self.typer.add_mode("PRO")
            self.typer.add_text("NEW\n")
        if prog.setup:
            # BASICの置き場所を動かす命令などを打ってから、空いたところにダンプを書く
            self.typer.add_text(prog.setup + "\n")
            self.typer.add_write(prog.blocks)
        if prog.basic:
            # 打ち込むかわりに、ROMが作るのと同じ中間コードをRAMに置く
            text, title = prog.basic, prog.title or prog.name
            self.typer.add_call(lambda: self.store_basic(text, title))
        if run and prog.run_command:
            self.typer.add_mode(prog.mode.upper() or "RUN")
            for i, line in enumerate(prog.run_command.split("\n")):
                if i:
                    self.typer.add_idle()  # 前の命令が終わって入力待ちに戻るのを待つ
                self.typer.add_text(line + "\n")
        elif prog.basic and not prog.after:
            self.typer.add_mode(back)
        if prog.after:
            self.typer.add_write(prog.after)
        if prog.basic:
            self.last_prog = prog
        self.fast = self.typer.busy
        self.loading = prog.title
        self.load_help = prog.help
        self.load_total = max(1, len(self.typer.queue))
        self.load_typing = False
        if not self.fast:
            if prog.help:
                self.say(prog.help, 20)
            else:
                self.say(f"{prog.title}を読み込みました({prog.size}バイト)")

    def write_dump(self, blocks: list[tuple[int, bytes]]) -> None:
        """ダンプをRAMに書き、作業欄の書いた範囲(書き出しに使う)にも0のバイトを含めて入れる。
        ダンプを書くところ(すぐ書く、# setup:のあと、# after:、--load)は、すべてここを通す"""
        for addr, data in blocks:
            for i, b in enumerate(data):
                self.m.write(addr + i, b)
        self.bench.note_written(blocks)

    def _encoded(self, text: str) -> bytes | None:
        """BASICを中間コードの行の並びにする。打ち込みで欠ける行があるなどで置けなければNone"""
        model = self.m.model.name
        if not keytype.typeable(text, model):
            return None
        return basictext.encode_program(keytype.typed_text(text, model), self.m.mem)

    def store_basic(self, text: str, title: str = "") -> None:
        """BASICのプログラムを、打ち込んだときにROMが作るのと同じ中間コードでRAMに置く。
        入りきらないときや、打ち込みでも欠ける長い行があるときは打ち込み、結果をROMに任せる。
        どちらでも、入れたあとですべての行が入ったかを確かめる(check_basic)"""
        m = self.m
        data = self._encoded(text)
        check = self.typer.call(lambda: self.check_basic(text, title))
        if data is not None and m.store_program(data):
            self.typer.queue[0:0] = [check]
        else:
            keys = keys_for_program(text, m.model.name)
            self.typer.queue[0:0] = [*keys, ["@IDLE"], check]
            self.load_total = len(self.typer.queue)  # ここからは打ち込みの進み具合を出す
            self.load_typing = True

    def check_basic(self, text: str, title: str) -> None:
        """textの行がすべて、欠けずにRAMに入ったかを確かめる。入っていない行や中身が違う行が
        あれば(RAMが足りないなど)、そのあとの実行やダンプの書き込みをやめて知らせる"""
        m = self.m
        want: set[int] = set()
        for line in keytype.typed_text(text, m.model.name).split("\n"):
            hit = re.match(r"\s*(\d+)(.*)", line)
            if hit and hit[2].strip():
                want.add(int(hit[1]))
            elif hit:  # 行番号だけの行は、その行を消す
                want.discard(int(hit[1]))
        start = m.program_start()
        if basictext.is_s1(m.mem):
            have = {n for n, _ in basictext.program_lines_s1(m.mem, start)}
        else:
            have = {n for n, _ in basictext.program_lines(m.mem, start)}
        missing = sorted(want - have)
        if missing:
            self.typer.clear()
            self.fast = False
            self.say(
                f"{title}はRAMに入りきりませんでした({missing[0]}行から{len(missing)}行が"
                f"入っていません)。実行と、そのあとの書き込みはやめました。{UNDO_HINT}",
                10,
            )
            return
        bad = self._differs(text)
        if bad is not None:
            self.typer.clear()
            self.fast = False
            self.say(
                f"{title}の{bad}行が、思ったとおりに入りませんでした。実行と、そのあとの"
                f"書き込みはやめました。{UNDO_HINT}",
                10,
            )

    def _differs(self, text: str) -> int | None:
        """RAMのプログラムが、textを中間コードにしたものと違えば、最初に違う行の番号。
        中間コードにできないとき(行番号がおかしいなど)は比べずにNone"""
        m = self.m
        data = basictext.encode_program(keytype.typed_text(text, m.model.name), m.mem)
        if data is None:
            return None
        start = m.program_start()
        if start + 1 + len(data) >= len(m.mem):
            return None
        want = bytearray(m.mem)
        want[start + 1 : start + 1 + len(data)] = data
        want[start + 1 + len(data)] = 0xFF
        lines = basictext.program_lines_s1 if basictext.is_s1(m.mem) else basictext.program_lines
        got, exp = lines(m.mem, start), lines(want, start)
        for (n, body), (en, ebody) in zip(got, exp, strict=False):
            if (n, body) != (en, ebody):
                return min(n, en)
        if len(got) != len(exp):
            return (got if len(got) > len(exp) else exp)[min(len(got), len(exp))][0]
        return None

    def insert_tape(self, path: str) -> None:
        """wavのテープをセットする。ROMがカセットの入力を読み始めたときに再生が始まる"""
        try:
            self.m.tape = tape.TapeIn(path)
        except (OSError, EOFError, wave.Error) as e:
            self.say(f"{os.path.basename(path)}を読めません({e})", 5)
            return
        self.say(
            f"テープ {os.path.basename(path)} をセットしました。CLOADかCLOAD Mで読み込みます", 6
        )

    def open_file(self, path: str) -> None:
        if path.lower().endswith(".wav"):
            self.insert_tape(path)
            return
        if os.path.splitext(path)[1].lower() in programs.EXTS:
            self.open_program(programs.load_program(path))
            return
        if path.lower().endswith(".bin"):  # 置く番地がわからない
            self.say(
                f"{os.path.basename(path)}は、.basに「# bin: 名前.bin &C300」と書いて読み込みます",
                6,
            )
            return
        try:
            self.typer.add_text(open(path, encoding="utf-8").read())
        except (OSError, UnicodeDecodeError):
            self.say(f"{os.path.basename(path)}を読めません", 4)

    def say(self, text: str, seconds: float = 3.0) -> None:
        self.status = text
        self.status_until = time.monotonic() + seconds

    def open_menu(self) -> None:
        """プログラムの一覧を開く"""
        items = programs.scan()
        if self.norom:  # ROMなしでは、ROMなしで使えるものだけ
            items = [p for p in items if p.norom_ok]
        self.menu = ProgramMenu(programs.for_model(items, self.m.model.name, self.m.writable))

    def menu_key(self, ev) -> None:
        menu = self.menu
        assert menu is not None
        if menu.query is not None and ev.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
            # 探しているあいだ: Escで探すのをやめ、Backspaceで1字消す
            menu.search(None if ev.key == pygame.K_ESCAPE else menu.query[:-1])
        elif ev.key in (pygame.K_ESCAPE, pygame.K_F6):
            self.menu = None
        elif ev.key in (pygame.K_UP, pygame.K_DOWN):
            menu.move(-1 if ev.key == pygame.K_UP else 1)
        elif ev.key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            menu.page(-1 if ev.key == pygame.K_PAGEUP else 1)
        elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and menu.items:
            self.menu = None
            self.open_program(menu.items[menu.sel], run=not ev.mod & pygame.KMOD_SHIFT)
        elif menu.query is not None:  # 探す字はtext_inputで受ける
            return
        elif ev.key == pygame.K_o:
            self.reveal(programs.user_dir())
        elif ev.key == pygame.K_r:
            self.open_menu()

    def menu_click(self, pos) -> None:
        """行を押すと選ぶだけ(選んである行でも読み込まない)。読み込むのは下のボタンかEnter。
        一覧の外を押すと閉じる"""
        menu = self.menu
        assert menu is not None
        act = menu.button_at(pos)
        if act:
            self.menu_action(act)
            return
        i = menu.hit(pos)
        if i >= 0:
            menu.sel = i
        elif not menu.box.collidepoint(pos):
            self.menu = None

    def menu_action(self, act: str) -> None:
        """一覧の下のボタンと、上の探す欄"""
        menu = self.menu
        assert menu is not None
        if act == "search":
            if menu.query is None:
                menu.search("")
        elif act in ("run", "load") and menu.items:
            self.menu = None
            self.open_program(menu.items[menu.sel], run=act == "run")
        elif act == "folder":
            self.reveal(programs.user_dir())
        elif act == "reload":
            self.open_menu()
        elif act == "close":
            self.menu = None

    @staticmethod
    def reveal(path: str) -> None:
        """置き場所をFinder(など)で開く"""
        cmd = {"darwin": ["open"], "win32": ["explorer"]}.get(sys.platform, ["xdg-open"])
        try:
            subprocess.Popen(cmd + [path])
        except OSError:
            pass

    def paste(self) -> None:
        self.typer.add_text(self.clipboard())

    def export_program(self) -> None:
        """RAMにあるBASICのプログラムを、置き場所に.basのファイルとして書き出す"""
        if self.typer.scripted:
            self.say("打ち込みが終わってから書き出してください")
            return
        prog = self.last_prog
        try:
            path = export_program(self.m, programs.user_dir(), prog)
        except OSError as e:
            self.say(f"書き出せません: {e}", 5)
            return
        if path is None:
            self.say("書き出すプログラムがありません")
        else:
            self.say(f"{_short(path)}に書き出しました", 5)

    def save_tape(self, samples) -> None:
        """CSAVEの音を、置き場所に「tape-日時.wav」として書き出す"""
        try:
            path = os.path.join(programs.user_dir(), f"tape-{time.strftime('%Y%m%d-%H%M%S')}.wav")
            tape.write_wav(path, samples)
        except OSError as e:
            self.say(f"テープの音を書き出せません: {e}", 5)
            return
        self.say(f"テープの音を{_short(path)}に書き出しました", 6)

    def save_state(self) -> None:
        """いまの状態(RAM、CPU、ポート、タイマ)を保存する"""
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            self.m.save_state(self.state_file, self._prog_info())
        except OSError as e:
            self.say(f"状態を保存できません: {e}", 5)
            return
        self.say(f"状態を保存しました。{SHIFT}{CMD}Lで戻せます")

    def load_state(self, path: str | None = None) -> None:
        """保存した状態に戻す。pathがなければsave_stateの保存先から"""
        try:
            extra = self.m.load_state(path or self.state_file)
        except FileNotFoundError:
            self.say("保存した状態がありません")
            return
        except (OSError, ValueError) as e:
            self.say(f"状態を読み込めません: {e}", 5)
            return
        self._set_prog_info(extra)
        self._restored("保存した状態に戻しました")

    # 状態のファイルに書く、読み込んだプログラムの情報(書き出しと説明に使う)。元のファイルは
    # 保存したあとで書き換えられるかもしれないので、場所ではなく中身を書いておく
    PROG_TEXT = ("name", "title", "run", "mode", "setup", "help", "model")
    PROG_LISTS = ("paths", "links")

    def _prog_info(self) -> dict:
        prog = self.last_prog
        saved = None
        if prog is not None:
            saved = {k: getattr(prog, k) for k in self.PROG_TEXT + self.PROG_LISTS}
        info = {"program": saved, "loading": self.loading, "help": self.load_help}
        info["bench"] = self.bench.save()
        return info

    def _set_prog_info(self, info: dict | None) -> None:
        """_prog_infoで書いた情報に戻す。なければ、おかしければ、前に読み込んだプログラムの
        情報は使わずに消す(書き出すファイルに、RAMと関係のない名前や# hex:が付かないように)"""
        info = info if isinstance(info, dict) else {}
        saved, prog = info.get("program"), None
        if isinstance(saved, dict):
            text = {k: saved.get(k) for k in self.PROG_TEXT}
            lists = {k: saved.get(k) for k in self.PROG_LISTS}
            if all(isinstance(v, str) for v in text.values()) and all(
                isinstance(v, list) and all(isinstance(x, str) for x in v) for v in lists.values()
            ):
                fields: dict[str, Any] = {**text, **lists}
                prog = programs.Program(**fields)
        loading, load_help = info.get("loading"), info.get("help")
        self.last_prog = prog
        self.loading = loading if isinstance(loading, str) else ""
        self.load_help = load_help if isinstance(load_help, str) else ""
        self.bench.restore(info.get("bench"))

    def _restored(self, message: str) -> None:
        """状態を戻したあと、打ち込みの残りや音を捨てる"""
        self.typer.restored()
        self.fast = False
        self.overshoot = 0
        self.buzzer.hold(self.m.pc_out >> 4)
        if self.speaker is not None:
            self.speaker.clear()
        self.say(message)

    def screenshot(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            n = 1
            while os.path.exists(os.path.join(STATE_DIR, f"shot{n:03d}.png")):
                n += 1
            path = os.path.join(STATE_DIR, f"shot{n:03d}.png")
            pygame.image.save(opaque(self.panel.out), path)
        except (OSError, pygame.error) as e:
            self.say(f"画面の画像を保存できません: {e}", 5)
            return
        self.say(f"{_short(path)}に保存しました")

    # ---- 録画 ----
    def toggle_recording(self) -> None:
        """録画を始める。録画中なら止めて、置き場所に「rec-日時.mp4」として保存する"""
        if self.recorder is None:
            prog = self.last_prog
            title = prog.title if prog else f"SHARP {self.m.model.title}"
            sub = f"SHARP {self.m.model.title} / pc1251-emu {_version()}"
            path = os.path.join(programs.user_dir(), f"rec-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
            try:
                size = (self.panel.out_w, self.panel.out_h)
                self.recorder = recorder.Recorder(path, title, sub, size, self.panel.lcd_box())
            except (OSError, ImportError) as e:
                self.say(f"録画を始められません: {e}", 6)
                return
            self.rec_buzzer = Buzzer()
            self.say(f"録画を始めました。止めて保存するときは、もう一度{SHIFT}{CMD}Rを押します", 4)
            return
        rec, self.recorder = self.recorder, None
        self.rec_paused = False
        self.say("録画を保存しています…", 2)
        try:
            path = rec.close()
        except OSError as e:
            self.say(f"録画を保存できません: {e}", 6)
            return
        self.say(f"録画を{_short(path)}に保存しました({rec.seconds:.0f}秒)", 6)

    def toggle_pause(self) -> None:
        """録画を一時停止する。一時停止中なら続きを録る(止めているあいだは動画に入らない)"""
        if self.recorder is None:
            return
        self.rec_paused = not self.rec_paused
        if not self.rec_paused:
            self.rec_buzzer.hold(self.m.pc_out >> 4)
        self.say("録画を一時停止しました" if self.rec_paused else "録画を続けます", 2)

    def record_frame(self, surf, c0: int, live: bool) -> None:
        """1フレームぶんの絵と音を録画に足す。liveでないとき(読み込み中、電源が切れている
        とき)は無音にする"""
        rec = self.recorder
        assert rec is not None
        m = self.m
        if live and m.cpu.cycles != c0:
            pcm = self._render(self.rec_buzzer, c0, keep_silent=True)
        else:
            self.rec_buzzer.hold(m.pc_out >> 4)
            pcm = bytes(2 * recorder.SPF)
        rec.add(pygame.image.tobytes(surf, "RGB"), pcm)
        if rec.error:
            self.recorder = None
            rec.abort()
            self.say(f"録画を止めました: {rec.error}", 6)

    def _font(self) -> pygame.font.Font:
        if self.font is None:
            self.font = load_help_font(17)
        return self.font

    # ---- 本体 ----
    def handle(self, ev) -> bool:
        if ev.type == pygame.QUIT:
            return False
        if self.help and ev.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN):
            # キーの説明は、何か押すかクリックしたら消す(その操作もそのまま効く)
            if ev.type != pygame.KEYDOWN or ev.key not in (pygame.K_SLASH, pygame.K_F10):
                self.help = 0
        if ev.type == pygame.KEYDOWN:
            self.key_down(ev)
        elif ev.type == pygame.KEYUP:
            self.key_up(ev)
        elif ev.type == pygame.TEXTINPUT:
            self.text_input(ev.text)
        elif ev.type == pygame.MOUSEBUTTONDOWN and (
            ev.button == 3 or (ev.button == 1 and MAC and pygame.key.get_mods() & pygame.KMOD_CTRL)
        ):
            if self.menu is None:
                self.open_popup(ev.pos)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            self.mouse_down(ev.pos)
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            self.mouse_up()
        elif ev.type == pygame.MOUSEWHEEL and self.menu is not None:
            self.menu.scroll(-ev.y)
        elif (
            ev.type == pygame.MOUSEWHEEL
            and self.pane_open
            and self.pane_keys()
            and pygame.mouse.get_pos()[0] >= self.body_size[0]
        ):
            self.bench.page(-ev.y, 1)  # 作業欄の上のホイールで、ダンプを1行ずつ動かす
        elif ev.type == pygame.MOUSEWHEEL and self.paper_open:
            self.scroll_paper(ev.y)
        elif ev.type == pygame.MOUSEMOTION and self.popup is not None:
            self.popup.sel = self.popup.hit(ev.pos)
        elif ev.type == pygame.MOUSEMOTION and not self.dragging_switch:
            self.bar.hover = self.bar.hit(ev.pos)
            if self.pane_open:
                hit = self.bench_view.hit(ev.pos)
                self.bench_view.hover = hit[1] if hit and hit[0] == "button" else -1
        elif ev.type == pygame.MOUSEMOTION and self.dragging_switch:
            self.set_switch_from(ev.pos[1] / self.scale - display.MARGIN)
        elif ev.type == pygame.DROPFILE:
            self.open_file(ev.file)
        elif ev.type == pygame.WINDOWLEAVE:
            self.bar.hover = -1
        elif ev.type == pygame.WINDOWFOCUSLOST:
            self.release_all()
        return True

    def release_all(self) -> None:
        """パソコンのキーとマウスで押しているものを、すべて離す。ほかのウィンドウに移ると
        キーを離したことが届かないので、押したままにならないようにする"""
        self.typer.release_physical()
        self.mouse_up()
        self.brk_by.clear()
        self.m.brk = self.m.reset_key = False

    def frame(self) -> None:
        """1フレームぶん(1/FPS秒)動かして描く"""
        m = self.m
        self._key_repeat()
        if not self.halted:
            self._count_down()
        typing = self.typer.scripted and not self.boot_frames
        speed = self.turbo * (8 if typing else 1)
        c0 = m.cpu.cycles
        m.sound_events.clear()
        if self.halted or (self.norom and self.bench.state != "run"):
            speed = 0
        elif self.fast and self.typer.busy and not self.boot_frames:
            # 読み込み中は時計に合わせず、1フレームの時間いっぱいまで回す
            speed = 0
            end = time.perf_counter() + 0.8 / FPS
            while time.perf_counter() < end and self.typer.busy:
                self.typer.step()
                # 打ったCALLでマシン語が動き出せば、止める所で止まる(読み込みの途中でも)
                if self._run_cpu(CLOCK // 200):
                    break
        elif self.fast:
            self.fast = False
            if self.load_help:
                self.say(self.load_help, 20)
            else:
                self.say(f"{self.loading}を読み込みました")
        speaker = self.speaker if bool(speed) and not typing else None  # 鳴らすときだけ
        adjust = speed_adjust(speaker.level()) if speaker is not None else 1
        self._advance(int(speed * (CLOCK // FPS) * adjust))
        wav = self.tape_out.feed(m.sound_events, m.cpu.cycles, m.pc_out >> 4)
        if wav is not None:
            self.save_tape(wav)
        if speaker is not None:
            keep = speaker.keep_silent
            pcm = self._render(self.buzzer, c0, keep_silent=keep)
            speaker.push(pcm)
        elif self.speaker is not None:
            self.buzzer.hold(m.pc_out >> 4)
            self.speaker.clear()
        if m.tape is not None and m.tape.done(m.cpu.cycles):
            m.tape = None
            self.say("テープが終わりました")
        pressed = set(m.held) | ({"BRK"} if m.brk else set())
        surf = self.panel.draw(
            m.columns(), m.symbols(), m.display_on(), m.mode, pressed, powered=m.power
        )
        if self.recorder is not None and not self.rec_paused:
            self.record_frame(surf, c0, bool(speed) and not typing)
        self._show(surf)

    def _count_down(self) -> None:
        """起動のあとの待ちと、RESETボタンを押している時間を数える"""
        m = self.m
        if self.boot_frames:
            self.boot_frames -= 1
            if self.boot_frames == 0 and self.load:
                blocks = parse_hexdump(self.load)
                self.write_dump(blocks)
                self.bench.set_start(blocks)
            if self.boot_frames == 0 and self.pending:
                prog, run = self.pending
                self.open_program(prog, run)
                self.pending = None
        if self.reset_frames:
            self.reset_frames -= 1
            if self.reset_frames == 0:
                m.reset_key = False

    def _render(self, buzzer: Buzzer, c0: int, keep_silent: bool) -> bytes:
        """このフレーム(サイクルc0〜いま)の音。パソコンのキーを速く届けたあいだ(rush_spans)は
        RUSH倍に縮めて、実際にたった時間の長さの音にする(録画やスピーカーの音が、速く
        回したぶん長く引き延ばされないように。ほかのところの音の高さと長さは変えない)"""
        m = self.m
        out, at = [], c0
        for a, b in [*self.rush_spans, (m.cpu.cycles, m.cpu.cycles)]:
            if a > at:
                out.append(buzzer.render(m.sound_events, at, a, self.turbo, keep_silent))
            if b > a:
                out.append(buzzer.render(m.sound_events, a, b, self.turbo * RUSH, keep_silent))
            at = max(at, b)
        return b"".join(out)

    def _advance(self, cycles: int) -> None:
        """cyclesだけ動かす。前のフレームで命令の途中まではみ出した分は差し引く
        (音の標本の数が再生の速さとそろうように)"""
        m = self.m
        target = m.cpu.cycles + cycles - self.overshoot
        self.overshoot = 0
        self.rush_spans = []
        while (left := target - m.cpu.cycles) > 0:
            rush = False
            if not self.boot_frames:
                self.typer.step()
                rush = self.typer.rushing and bool(cycles)
            before = m.cpu.cycles
            if self._run_cpu(min(left, CLOCK // (1000 if rush else 200))):
                break
            if rush:
                # パソコンのキーをROMの入力待ちに届けているあいだは、RUSH倍の速さで回す
                # (1打鍵ごとのROMの読み取りの待ちを縮めて、打ち込みのもたつきをなくす)
                target += (m.cpu.cycles - before) * (RUSH - 1) // RUSH
                spans = self.rush_spans
                if spans and spans[-1][1] == before:
                    spans[-1] = (spans[-1][0], m.cpu.cycles)
                else:
                    spans.append((before, m.cpu.cycles))
            if m.cpu.cycles == before:  # 電源が切れていて時間が進まない
                break
        else:
            self.overshoot = m.cpu.cycles - target

    def _run_cpu(self, cycles: int) -> bool:
        """CPUを最大cyclesサイクル動かす。作業欄の止める所、呼び出しの戻り先、ROMなしでは
        RAMの外とROMのルーチンの代わりも、ここで調べる(本体を動かすところは、すべてここを
        通す)。作業欄の実行が止まったらTrue"""
        m, bench = self.m, self.bench
        if bench.state == "run" and bench.pass_pc is not None:
            # 再帰の内側の戻り先を、止めずに1命令だけ進める。終わらないプログラムでも、
            # 1回に1命令ずつなので、フレームのサイクルを使い切れば画面に戻る
            bench.pass_step()
            stopped = bench.state != "run"
        else:
            guard = bench.guard() if bench.state == "run" and bench.needs_guard() else None
            # 止まっても、ROMのルーチンの代わりを済ませただけなら(trapped)続ける
            stopped = m.run(cycles, guard) and not bench.trapped()
        if stopped:
            self._bench_stopped()  # 終わったか、止める所かRAMの外で一時停止した
        return stopped

    def _bench_stopped(self) -> None:
        """作業欄の実行が止まった(終わったか、止める所などで一時停止した)。押しているキーを
        離す。ROMのある本体で作業欄をしまっていれば、止まった所を見せるために出す"""
        self.release_all()
        if not self.norom and self.bench.state == "pause":
            self.toggle_pane(True)

    def _show(self, surf: pygame.Surface) -> None:
        """本体の絵を窓に貼り、一覧や知らせを重ねる"""
        self.check_printer()
        out = self.window
        w, h = self.body_size
        body = out.subsurface((0, 0, w, h))
        if self.scale == 1.0:
            body.blit(surf, (0, 0))
        else:
            pygame.transform.smoothscale(surf, (w, h), body)
        font = self._font()
        top = h
        if self.paper_open and self.m.printer is not None:
            rows = self.paper_rows()
            where = []
            if self.print_file:
                folder, name = os.path.split(_short(self.print_file))
                where = ["保存先(クリックで開く)", folder + os.sep, name]
            self.paper.draw(out, top, rows[: len(rows) - self.paper_back], where)
            top += self.paper.height
        self.bar.buttons = self.bar_buttons()
        left = out.subsurface((0, 0, w, out.get_height())) if self.pane_open else out
        self.bar.draw(left, font, top, self.bar_info())
        notice = body  # 知らせを出す場所
        if self.pane_open:
            # 作業欄は右に置き、本体と操作欄の下のあいたところに知らせを出す
            below = top + BAR_H
            if below < out.get_height():
                out.fill((20, 21, 24), (0, below, w, out.get_height() - below))
                notice = out.subsurface((0, below, w, out.get_height() - below))
            pane = pygame.Rect(w, 0, workbench.PANE_W, out.get_height())
            self.bench_view.draw(out, pane)
        if self.help:
            self.help -= 1
            lines = HELP_1350 if self.is1350 else HELP
            draw_help(out, font, HELP_NOROM if self.norom else lines)
        if self.fast:
            done = min(1.0, max(0.0, 1 - len(self.typer.queue) / self.load_total))
            doing = "打ち込み中" if self.load_typing else "読み込み中"
            text = f"{self.loading}を{doing}… {int(done * 100)}%(Escでやめる)"
            draw_status(notice, font, text)
        elif self.status and time.monotonic() < self.status_until:
            draw_status(notice, font, self.status)
        if self.menu is not None:
            self.menu.draw(out, font, self.m.model.name, _short(programs.user_dir()))
        if self.popup is not None:
            self.popup.draw(out, font, self.popup_items(), self.popup_mark())
        if self.recorder is not None:
            draw_recording(out, font, self.recorder.seconds, self.rec_paused)
        if self.halted and not self.pane_open:  # 作業欄を出していれば、作業欄に出る
            draw_halted(out, font, f"実行を一時停止中({CMD}Pで再開)")
        pygame.display.flip()

    @staticmethod
    def _view_prefs() -> dict:
        """作業欄の見え方の覚え(view_file)。なければ空"""
        try:
            with open(view_file(), encoding="utf-8") as f:
                prefs = json.load(f)
        except (OSError, ValueError):
            return {}
        return prefs if isinstance(prefs, dict) else {}

    def _view_pref(self, key: str) -> object:
        return self._view_prefs().get(key)

    def _iram_shown(self) -> bool:
        """作業欄の内部RAMを開いておくか。前に開いた・閉じたものがあればそれに従う。なければ
        開いておくが、開いた作業欄が画面の高さに入らなければ閉じておく"""
        shown = self._view_pref("show_iram")
        if isinstance(shown, bool):
            return shown
        try:
            _dw, dh = pygame.display.get_desktop_sizes()[0]
        except (pygame.error, IndexError):
            return True
        return self.bench_view.min_height(True) <= dh - 120

    def _save_view(self) -> None:
        """作業欄の内部RAMを開いたか閉じたか、ROMのある本体で作業欄を出したかを覚えておく
        (次に起動したときも同じにする)"""
        prefs = self._view_prefs()
        prefs["show_iram"] = self.bench_view.show_iram
        if not self.norom:
            prefs["show_pane"] = self.pane_open
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(view_file(), "w", encoding="utf-8") as f:
                json.dump(prefs, f)
        except OSError:
            pass

    def pane_keys(self) -> bool:
        """パソコンのキーを、本体ではなく作業欄で受けるか。ROMなしでは実行していないあいだ。
        ROMがあれば、作業欄を出していて、作業欄をクリックしたあとか一時停止しているあいだ"""
        b = self.bench
        if self.norom:
            return b.state != "run"
        return self.pane_open and (self.bench_view.focus or b.state == "pause")

    def toggle_pane(self, on: bool | None = None) -> None:
        """ROMのある本体で、マシン語の作業欄を出すかしまう(窓の幅が変わる)"""
        if self.norom:
            return
        on = not self.pane_open if on is None else on
        if on == self.pane_open:
            return
        self.pane_open = on
        self.bench_view.focus = False
        self.release_all()
        self._save_view()
        # 作業欄の幅も合わせて画面に入るように、本体の拡大率を決め直す(しまえば、指定された
        # 拡大率まで戻る)
        pane = workbench.PANE_W if on else 0
        self.scale = self._fit(self.want_scale, self.out_w, self.out_h + BAR_H, pane)
        self.body_size = (int(self.out_w * self.scale), int(self.out_h * self.scale))
        self.window = pygame.display.set_mode(self._window_size())

    def _load_bench_info(self) -> None:
        """終了したときに保存した作業欄の情報を読む。なければ(前の版のRAM)、RAMの0でない
        所を書いた範囲とする"""
        b = self.bench
        assert b is not None
        try:
            with open(bench_file(self.ram_file), encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            info = None
        if isinstance(info, dict):
            info["state"] = "edit"  # CPUは起動し直しているので、続きからは動かせない
            b.restore(info, "")
        elif self.norom:  # ROMのある本体のRAMにはBASICなども入っているので、推し量らない
            b.mark_nonzero()

    def close(self) -> None:
        self.save_printout()
        if self.save_ram:
            if self.m.power and not self.norom:
                self.m.power_off()
            # 作業欄の開始番地、止める所、書いた範囲も、RAMと一緒に覚えておく。保存できなくても
            # (置き場所に同じ名前のファイルがあるなど)、録画の保存と後始末は続ける
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                self.m.save_ram(self.ram_file)
                with open(bench_file(self.ram_file), "w", encoding="utf-8") as f:
                    json.dump({**self.bench.save(), "state": "edit"}, f)
            except OSError as e:
                print(f"RAMを保存できません: {e}", file=sys.stderr)
        if self.recorder is not None:
            self.toggle_recording()  # 録画中に閉じたら、そこまでを保存する
        if self.speaker is not None:
            self.speaker.close()
        pygame.quit()

    def run(self) -> None:
        clock = pygame.time.Clock()
        running = True
        try:
            while running:
                for ev in pygame.event.get():
                    running = self.handle(ev) and running
                self.frame()
                clock.tick(FPS)
        finally:  # 思わぬ誤りで止まっても、RAMと印字は保存する
            self.close()


app_cli = typer.Typer(
    add_completion=False, help="SHARP PC-1251/PC-1245/PC-1261/PC-1350エミュレータ"
)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"pc1251-emu {_version()}")
        raise typer.Exit()


@app_cli.command()
def main(
    program: str | None = typer.Argument(
        None, help="読み込むプログラム(.bas/.hexのパスか、置き場所にある名前)"
    ),
    run: bool = typer.Option(False, "--run", help="読み込んだあと実行する"),
    scale: float = typer.Option(1.0, help="拡大率"),
    type_: str | None = typer.Option(
        None, "--type", help="起動後にこのファイルの中身をキー入力する"
    ),
    load: str | None = typer.Option(None, help="16進ダンプ(番地 データ)をRAMに書き込む"),
    fresh: bool = typer.Option(False, help="保存したRAMを読まず、終了時にも保存しない"),
    mute: bool = typer.Option(False, help="音を出さない"),
    persist: float = typer.Option(1.0, help="液晶の残像の強さ(0で残像なし)"),
    export: str | None = typer.Option(
        None, help="保存したRAMのBASICのプログラムを、このフォルダに.basで書き出して終わる"
    ),
    model: str = typer.Option(
        "1251",
        help="機種(1250、1251、1255、1245、1260、1261、1262、1350)。ROMはcpu-機種.romと"
        "bas-機種.rom(1250と1255は1251の、1260〜1262はどれも1260のROMを使う)",
    ),
    card: int = typer.Option(0, help="PC-1350に差すRAMカードの大きさ(KB)。0、8、16"),
    overseas: bool = typer.Option(
        False, help="PC-1350を輸出向けの配線で動かす(カナの代わりに英小文字、¥の代わりに\\)"
    ),
    numpad: bool = typer.Option(
        False, help="矢印キーを数字キーの8・2・4・6として押す(⌘K/Ctrl+Kで切り替え)"
    ),
    tape_: str | None = typer.Option(
        None, "--tape", help="カセットの音(wav)をセットする。CLOAD・CLOAD Mで読み込める"
    ),
    state: str | None = typer.Option(None, help="保存した状態(⇧⌘Sで書いたもの)から始める"),
    norom: bool = typer.Option(
        False, "--norom", help="ROMを使わず、マシン語の作業欄で動かす(ROMがなければ自動でこうなる)"
    ),
    version_: bool = typer.Option(
        False, "--version", callback=_show_version, is_eager=True, help="版を出して終わる"
    ),
) -> None:
    if model not in MODELS:
        typer.echo(f"機種は{'、'.join(sorted(MODELS))}のどれかです", err=True)
        raise typer.Exit(2)
    if card not in (0, 8, 16):
        typer.echo("RAMカードの大きさは0、8、16のどれかです", err=True)
        raise typer.Exit(2)
    if MODELS[model].family != "1350":
        card = 0
    if export is not None:
        try:
            m = new_machine(model, card)
        except RomNotFound as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        if not m.load_ram(ram_file(model, card)):
            typer.echo(f"{ram_file(model, card)}がありません", err=True)
            raise typer.Exit(1)
        path = export_program(m, export)
        typer.echo(path or "書き出すプログラムがありません")
        return
    args = SimpleNamespace(
        scale=scale,
        type=type_,
        load=load,
        fresh=fresh,
        mute=mute,
        persist=persist,
        program=program,
        run=run,
        model=model,
        card=card,
        overseas=overseas,
        numpad=numpad,
        tape=tape_,
        state=state,
        norom=norom,
    )
    try:
        App(args).run()
    except (RomNotFound, FileNotFoundError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def cli() -> None:
    app_cli()


if __name__ == "__main__":
    cli()

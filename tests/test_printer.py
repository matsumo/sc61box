"""プリンタをつなぐと、LPRINTとLLISTの印字が受け取れる"""

import pygame
import pytest
from helpers import boot, finish, make_app, settle, typ

from pc1251emu import programs
from pc1251emu.machine import CLOCK


def _printed(model, src, command="RUN"):
    m, t = boot(model)
    m.connect_printer()
    if model in ("1251", "1245"):  # 電源を入れたときにプリンタを調べる
        m.power_off()
        m.mode = "RUN"
        m.wake()
        m.run(CLOCK)
    typ(m, t, src, mode="PRO")
    typ(m, t, command + "\n", wait=3, mode="PRO" if command == "LLIST" else "RUN")
    assert m.printer is not None
    return m.printer.text()


@pytest.mark.parametrize("model", ["1251", "1245", "1261", "1350"])
def test_lprint(model):
    src = '10 LPRINT "AB0O";12\n20 LPRINT "π√"\n'
    assert _printed(model, src) == "AB0O12.\nπ√\n"


@pytest.mark.parametrize("model", ["1251", "1261", "1350"])
def test_llist(model):
    text = _printed(model, '10 PRINT "OK"\n20 A=10\n', "LLIST")
    assert [line.split(":", 1)[1] for line in text.splitlines()] == ['PRINT "OK"', "A=10"]


def test_kana_on_s1():
    assert _printed("1261", '10 LPRINT "ｱｲｳ"\n') == "ｱｲｳ\n"


def test_no_printer_skips_lprint():
    """プリンタがなければ、LPRINTは何もせずに次へ進む"""
    m, t = boot()
    typ(m, t, '10 LPRINT "A"\n20 PRINT 7\n', mode="PRO")
    assert typ(m, t, "RUN\n", mode="RUN").strip().rstrip("?.") == "7"
    assert m.printer is None


def test_paper_appears_when_printing(tmp_path, monkeypatch):
    """プリンタはいつもつながっていて、印字したときだけ窓の下に紙が出る。印字は紙の絵として
    保存でき、紙はしまえる"""
    monkeypatch.setenv("PC1251_PROGRAMS", str(tmp_path))
    app = make_app()
    h = app.window.get_height()
    assert app.m.printer is not None and not app.paper_open
    prog = programs.Program(name="p", title="p", basic='10 LPRINT "HELLO"\n', run="RUN")
    app.open_program(prog, run=True)
    settle(app)
    for _ in range(60):
        app.frame()
    assert app.m.printer.lines == ["HELLO"]
    assert app.paper_open and app.window.get_height() == h + app.paper.height
    app.save_printout()
    saved = list(tmp_path.glob("print-*.png"))
    assert len(saved) == 1
    img = pygame.image.load(str(saved[0]))
    assert img.get_width() == app.paper.width * 2
    dark = sum(
        1
        for x in range(img.get_width())
        for y in range(img.get_height())
        if sum(img.get_at((x, y))[:3]) < 300
    )
    assert dark > 100  # 点が打ってある
    app.handle(
        pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=app.paper.close_rect.center)
    )
    assert not app.paper_open and app.window.get_height() == h
    finish(app.m, app.typer)
    app.close()


def test_glyphs_from_rom():
    """紙に打つ字の形はROMの文字の表から取る"""
    from pc1251emu.lcdtext import glyphs

    for model in ("1251", "1261", "1350"):
        m, _ = boot(model)
        g = glyphs(m.mem)
        assert all(ch in g for ch in 'AZ09.:"π√¥'), model
        assert g["A"] == bytes.fromhex("7c1211127c")


def test_printout_saved_automatically(tmp_path, monkeypatch):
    """印字がとぎれると、紙の絵が置き場所に保存され、紙の左に保存先が出る。紙をしまうと
    その紙は終わり、次の印字は新しいファイルになる"""
    import time

    monkeypatch.setenv("PC1251_PROGRAMS", str(tmp_path))
    app = make_app()
    prog = programs.Program(name="p", title="p", basic='10 LPRINT "ONE"\n', run="RUN")
    app.open_program(prog, run=True)
    settle(app)
    first = app.print_file
    assert first and first.startswith(str(tmp_path)) and app.paper.where_rect.width > 0
    assert open(first, "rb").read(4) == b"\x89PNG"  # 新しい紙はすぐに保存する
    assert app.m.printer is not None
    app.m.printer.put(ord("X"))  # 印字を足し、1秒より前に印字したことにする
    app.m.printer.put(0x0D)
    app.frame()
    app.print_time -= 2
    app.frame()
    assert app.print_saved == 2 and "保存しました" in app.status
    app.show_paper(False)
    assert app.print_file is None
    time.sleep(1.1)  # ファイルの名前(秒まで)が変わるように
    app.open_program(prog, run=True)
    settle(app)
    assert app.print_file != first and app.paper_rows() == ["ONE"]
    app.close()  # 閉じるときも保存する
    assert len(list(tmp_path.glob("print-*.png"))) == 2


def test_print_files_do_not_overwrite(tmp_path, monkeypatch):
    """同じ秒に新しい紙になっても、前の紙のファイルを上書きしない"""
    from pc1251emu import app as app_module

    monkeypatch.setenv("PC1251_PROGRAMS", str(tmp_path))
    monkeypatch.setattr(app_module.time, "strftime", lambda fmt, *a: "20261022-000000")
    app = make_app()
    for word in ("ONE", "TWO"):
        prog = programs.Program(name="p", title="p", basic=f'10 LPRINT "{word}"\n', run="RUN")
        app.open_program(prog, run=True)
        settle(app)
        app.show_paper(False)
    names = sorted(p.name for p in tmp_path.glob("print-*.png"))
    assert names == ["print-20261022-000000-2.png", "print-20261022-000000.png"]
    app.close()


def test_print_file_name_is_reserved(tmp_path):
    """印字の名前は、確かめると同時にファイルを作って取る(ほかのエミュレータと重ならない)"""
    from pc1251emu.app import _unused

    first = _unused(str(tmp_path), "print-x", ".png")
    second = _unused(str(tmp_path), "print-x", ".png")
    assert first != second and (tmp_path / "print-x.png").exists()


def test_printout_picture_is_opaque():
    """印字の絵はアルファのない形式で作る。窓がアルファの成分のある形式でも、字のところが
    透けない"""
    app = make_app()
    img = app.paper.picture(["AB"])
    assert img.get_bitsize() == 24 and img.get_masks()[3] == 0
    assert any(sum(img.get_at((x, y))[:3]) < 400 for x in range(14, 40) for y in range(20, 40))
    app.close()


def test_opaque_drops_meaningless_alpha():
    """アルファの成分があってSRCALPHAでない絵(macOSの窓の形式)は、写すと透けずに色が残る"""
    from pc1251emu.app import opaque

    masks = (0xFF0000, 0xFF00, 0xFF, 0xFF000000)
    src = pygame.Surface((4, 4), pygame.SRCALPHA)
    src.fill((36, 38, 70, 220))
    dst = pygame.Surface((8, 8), 0, 32, masks)
    dst.fill((238, 237, 229))
    dst.blit(src, (2, 2))
    assert dst.get_at((3, 3))[3] == 0  # この形式では、字のところのアルファが0になる
    out = opaque(dst)
    assert out.get_at((3, 3))[:3] == dst.get_at((3, 3))[:3] and out.get_masks()[3] == 0

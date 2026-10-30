"""PyInstaller 用エントリポイント

`sc61box.app` をパッケージとして読み込み、CLI/アプリを起動する。
"""

from sc61box.app import cli

if __name__ == "__main__":
    cli()

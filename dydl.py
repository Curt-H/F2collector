"""F2 下载工具入口。

用法：
    python dydl.py                                  # 交互向导
    python dydl.py -p douyin -u <链接> -M post      # 直接生成配置并下载
    python dydl.py -p douyin -u <链接> -M post --gen-only   # 只生成配置
    python dydl.py cookie -p douyin                 # cookie 抓取/粘贴
"""

from f2dl import setup_streams

setup_streams()

from f2dl.cli import main  # noqa: E402

if __name__ == "__main__":
    main()


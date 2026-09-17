# f2dl

A Douyin / Weibo media downloader built on top of [f2](https://github.com/Johnserf-Seed/f2). **The f2 engine is vendored into this repository with local enhancements — no `pip install f2` required, just clone and run.**

## Features

- **Two platforms**: Douyin (10 modes) and Weibo (post); the f2 engine ships with the repo
- **Media only**: no captions / covers / music / lyrics by default — just images and videos
- **Flat layout**: no per-post folders; media files are archived directly under each author's directory
- **Auto-generated configs**: task configs are built from f2's official defaults and persisted to `dy\conf\`
- **Incremental updates**: every task records a `last_run` timestamp; on Douyin the fetch window is narrowed to `last_run|today`, and already-downloaded items are skipped by the engine
- **Automatic retries**: network failures re-run the whole task (resume + skip completed), up to 2 retries
- **Weibo Live Photo**: built-in patch downloads both the static frame `.jpg` and the motion video `_live_N.mov` (not implemented upstream)
- **Cookie management**: grab from a browser (Chrome/Edge/Firefox...), paste manually, or import from an old f2 config; configure once, reuse forever
- **Interactive wizard**: run without arguments for a menu-driven flow (new task / re-run tasks / cookie management)
- **Bark-free**: upstream Bark push notifications are disabled by default (avoids 405 noise when no key is set)

## Install

Requires Python 3.11+

```powershell
git clone https://github.com/<you>/f2dl.git
cd f2dl
pip install -r requirements.txt
```

## Usage

```powershell
python dydl.py                                  # interactive wizard
python dydl.py cookie -p douyin                 # grab Douyin cookie from browser (close the browser first)
python dydl.py cookie -p douyin --import-f2     # import cookie from an old f2 config
python dydl.py -p douyin -u "https://www.douyin.com/user/MS4wLjAB..." -M post
python dydl.py -p weibo  -u "https://weibo.com/u/1234567890" -M post
python dydl.py -p douyin -u "<link>" -M post --gen-only   # generate config only
```

- Task configs live in `dy\conf\<task>.yaml` — **re-running the same command performs an incremental update**
- Downloads go to `Download\dy\`
- Advanced options (download dir, naming, concurrency...) are available in the wizard's advanced submenu

## First run

1. Run `python dydl.py cookie -p douyin` and follow the prompt to grab the cookie from your browser (close that browser first)
2. Every task will then reuse the stored cookie automatically

## Layout

| Path | Description |
|------|-------------|
| `f2/` | Vendored f2 engine (with local patches, do not edit) |
| `f2dl/` | The tool itself |
| `dy\` | Runtime dirs: cookies, task configs (contain personal data — gitignored) |
| `Download\` | Download output |
| `logs\` | f2 runtime logs (for troubleshooting) |

## Credits & License

The core engine comes from [f2](https://github.com/Johnserf-Seed/f2) (Apache-2.0), vendored with local modifications — see [NOTICE](NOTICE). This project is also released under the [Apache-2.0](LICENSE) license.

> Respect the target platforms' terms of service. Only download content you are allowed to keep. Not for commercial use.

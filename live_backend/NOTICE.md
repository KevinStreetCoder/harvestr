# Vendored: StreaMonitor

This directory contains a vendored copy of **StreaMonitor**
(https://github.com/lossless1/StreaMonitor), used by Harvestr's Live-mode
backend to poll cam sites and record public broadcasts.

## License

StreaMonitor is licensed under **GPL-3.0** — see `LICENSE` in this directory.

The presence of this GPLv3 code inside Harvestr means that the Harvestr
distribution as a whole, when combined with these files, must comply with
GPL-3.0 terms:

- You may run, copy, distribute, study, change and improve the software.
- Any distribution of the combined work must make the full source (including
  your modifications to Harvestr) available under the same GPL-3.0 license.
- The original copyright notices and license file MUST remain intact.

## Why vendored

StreaMonitor has 18+ cam-site modules (Chaturbate, StripChat, CamSoda, Cam4,
BongaCams, Flirt4Free, Cherry.tv, Streamate, MyFreeCams, ManyVids, FanslyLive,
AmateurTV, CamsCom, DreamCam, SexChatHU, XLoveCam, plus VR variants) that
each implement careful reverse-engineered HLS extraction. Asking Harvestr
users to clone a second repo just to enable Live mode was a friction point.
Vendoring removes that friction.

## Updates

To bump this vendored copy, run (from a shell with git installed):

```bash
# From the Harvestr root:
rm -rf live_backend/streamonitor live_backend/parameters.py
cp -r /path/to/fresh/StreaMonitor/streamonitor live_backend/
cp /path/to/fresh/StreaMonitor/parameters.py live_backend/
```

The `live_backend/LICENSE` file should not need to change unless upstream
changes its license (unlikely).

## What was NOT vendored

- `__pycache__` directories (regenerated)
- Top-level scripts like `Downloader.py`, `Controller.py` (Harvestr has its
  own entrypoint — `live_recording.LiveManager`)
- Docker / CI configs
- User-specific things: `config.json`, `cookies/`, `logs/`, `downloads/`,
  `*.lock` files

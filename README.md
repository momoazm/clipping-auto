# clipping-auto

**One-liner:** Standalone daily clipping pipeline — auto-finds source videos and produces
short clips for upload.

- **Status:** Active — cloud-scheduled (GitHub-hosted), daily to YouTube @itsmomoclips + IG.
- **2026-07-08:** Download fix — WARP-alone was getting YouTube-bot-walled (killed posting for
  >1 day); added the free BgUtils PO-token provider (Docker localhost:4416 + yt-dlp plugin),
  verified via dry-run. `reframe_crop.py` is now motion-aware (follows the action in faceless
  shots + favors the moving/talking subject instead of blurred letterbox).
- **2026-07-12:** Weekly IG style experiment — clip 01 of whichever daily run happens first each
  ISO week tries a rotated caption style (`hormozi`/`brand`/`clean`) instead of the default;
  `check_style_experiment.py` (new Monday cron, `style_experiment.yml`) compares it against
  recent posts via Zernio analytics and WhatsApps Moemen (`send_whatsapp.py`, CallMeBot) if it
  clearly won. Never auto-applies a winning style — notification only.
- **Rules / how-to:** [CLAUDE.md](CLAUDE.md)
- **Key dates:** —

# Offline live preview

A self-contained snapshot of the review screen — no backend, no build step. Use it to
show the split-screen (fields left, PDF right, click-to-highlight) anywhere, anytime.

```bash
cd demo/live-preview
python3 build.py                 # renders page.png (needs: pip install pymupdf)
python3 -m http.server 8080      # then open http://localhost:8080
```

This is a **static snapshot** — the field values are the real `prepare('ref_1001')`
output baked in, but editing/submitting doesn't round-trip. For the live, interactive
stack (edits + submit hitting the API), run the real backend + frontend — see the repo
root `README.md` / `CLAUDE.md §11`.

## macOS note

If the shell can't read this folder (`Operation not permitted`), macOS is blocking
`Documents` access for your terminal. Fix once:
**System Settings → Privacy & Security → Full Disk Access → enable your terminal app**,
then restart it. (Or copy this `live-preview/` folder somewhere unprotected like `/tmp`
and run it there.)

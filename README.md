# ABET PROMO ENGINE (100% cloud, $0)

Cloud-only rendering + hosting for the ABET Sports Desk promo avatar.

**No local machine is ever used for rendering.** Free GitHub Actions runners render
SadTalker clips in parallel, and Cloudflare Pages hosts the live result.

## Architecture

```
GitHub Actions (free runners, up to 10 parallel shards)
  edge-tts -> SadTalker (CPU) -> ffmpeg chromakey -> VP9-alpha webm
        |
        v
  artifacts  --(deploy job)-->  Cloudflare Pages (abet-promo.pages.dev)
                                  index.html  (full player)
                                  embed.html  (for dashboards)
                                  clips/*.webm
                                  state.json  (playlist)
```

## Run a batch (step 1: render)

1. Repo > Actions > **SCARLETT clip batch (free cloud)** > Run workflow
2. Inputs:
   - `lines_json`: `[{"text":"...","name":"clip_name","talent":"a","title":"COLD OPEN"}]`
   - `portrait_url`: optional. Defaults to `portraits/SCARLETT_A.jpg` (add your own under `portraits/`)
   - `deploy`: `no` = just artifacts, `yes` = render AND deploy to Pages
3. Rendered clips land as artifacts per shard (`clips-shard-0..9`).

## Deploy to Pages (step 2, automated when `deploy=yes`)

Set repo secret once:

```
gh secret set CLOUDFLARE_API_TOKEN   # token with Pages:Edit permission
```

The deploy job merges all shard artifacts, builds `state.json`, and runs
`wrangler pages deploy` to the `abet-promo` project. No KV writes are used
(KV daily write quota is shared with production ABET scraper traffic).

## Embed in ANY dashboard

The player is a static page. Embed with one line — it fills whatever box you give it:

```html
<iframe src="https://abet-promo.pages.dev/embed.html"
        width="480" height="270" style="border:0;border-radius:12px"
        allow="autoplay" loading="lazy"></iframe>
```

React:

```jsx
export function AbetDesk({ width = 480, height = 270 }) {
  return (
    <iframe
      src="https://abet-promo.pages.dev/embed.html"
      width={width} height={height}
      style={{ border: 0, borderRadius: 12 }}
      allow="autoplay" loading="lazy" title="ABET Sports Desk"
    />
  );
}
```

Sound behavior: muted by default (browser autoplay policy). Click the SOUND
button inside the frame, or add `?sound=1` in the URL if the host page has
user-gesture context.

## Files

- `render_batch.py` — the engine (edge-tts -> SadTalker -> chromakey -> webm; optional KV upload)
- `lines_example.json` — sample batch input
- `site/` — player pages deployed to Pages
- `.github/workflows/render.yml` — the free render farm + optional deploy

#!/usr/bin/env python3
# render_batch.py - FREE-CLOUD avatar clip render engine (Colab / GitHub Actions / any VM).
# Pipeline per line: edge-tts wav -> SadTalker lip-sync render -> ffmpeg chromakey -> VP9-alpha webm.
# Then push to Cloudflare KV (via wrangler CLI, works wherever `npx wrangler` is authed)
# so clips land in the promo-frame worker's namespace. R2 path kept as fallback.
#
# Usage:
#   python render_batch.py --lines lines.json --portrait scarlett.jpg --outdir out
# lines.json: [{"text": "Are you a loser?", "name": "loser_hook"}, ...]
# KV envs: CF_KV_NAMESPACE_ID (default = promo-frame PROMO_STATE2 ns below), CLIP_PREFIX (default "clips/")
# R2 envs (optional legacy): R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PREFIX
import argparse, json, os, subprocess, sys, tempfile, glob, shutil

ROOT = os.path.abspath(os.path.dirname(__file__) or ".")
ST = os.path.join(ROOT, "SadTalker")
CKPT = os.path.join(ST, "checkpoints")

CHECKPOINTS = {
    "SadTalker_V0.0.2_256.safetensors": "https://huggingface.co/vinthony/SadTalker-V002rc/resolve/main/SadTalker_V0.0.2_256.safetensors",
    "SadTalker_V0.0.2_512.safetensors": "https://huggingface.co/vinthony/SadTalker-V002rc/resolve/main/SadTalker_V0.0.2_512.safetensors",
    "mapping_00109-model.pth.tar": "https://huggingface.co/vinthony/SadTalker-V002rc/resolve/main/mapping_00109-model.pth.tar",
    "mapping_00229-model.pth.tar": "https://huggingface.co/vinthony/SadTalker-V002rc/resolve/main/mapping_00229-model.pth.tar",
    "wav2lip.pth": "https://huggingface.co/vinthony/SadTalker-V002rc/resolve/main/wav2lip.pth",
}

def log(*a): print("[render_batch]", *a, flush=True)

def sh(cmd, **kw):
    log("$", " ".join(cmd) if isinstance(cmd, list) else cmd)
    subprocess.check_call(cmd, shell=isinstance(cmd, str), **kw)

def ensure_repo():
    if not os.path.isdir(ST):
        sh(f"git clone --depth 1 https://github.com/OpenTalker/SadTalker.git {ST!r}")
        # patch: neutralize gfpgan import (basicsr breaks on modern torchvision); we never use enhancer
        p = os.path.join(ST, "src", "facerender", "animate.py")
        s = open(p, encoding="utf-8").read()
        s = s.replace("from gfpgan import GFPGANer",
                      "try:\n    from gfpgan import GFPGANer\nexcept Exception:\n    GFPGANer = None")
        open(p, "w", encoding="utf-8").write(s)
        log("repo cloned + gfpgan import guarded")

def ensure_ckpts():
    os.makedirs(CKPT, exist_ok=True)
    os.makedirs(os.path.join(ST, "gfpgan", "weights"), exist_ok=True)
    for name, url in CHECKPOINTS.items():
        dst = os.path.join(CKPT, name)
        if not os.path.isfile(dst):
            sh(["curl", "-L", "-o", dst, url])
            log("downloaded", name)

def tts(text, out_wav):
    script = ("import asyncio,edge_tts;"
              "c=edge_tts.Communicate(" + repr(text) + ",'en-US-AvaMultilingualNeural',rate='+15%',pitch='+20Hz');"
              "asyncio.get_event_loop().run_until_complete(c.save(r'" + out_wav + "'))")
    sh([sys.executable, "-c", script, out_wav])  # last arg ignored by -c; path baked into source
    log("tts ->", out_wav)

def sadtalker_render(wav, portrait, tmpdir):
    cmd = [sys.executable, "inference.py", "--driven_audio", wav, "--source_image", portrait,
           "--result_dir", tmpdir, "--preprocess", "full", "--still"]
    if not _has_cuda(): cmd += ["--cpu"]
    sh(cmd, cwd=ST)
    latest = max(glob.glob(os.path.join(tmpdir, "**", "*.mp4"), recursive=True), key=os.path.getmtime)
    log("rendered", latest)
    return latest

def _has_cuda():
    try:
        import torch; return torch.cuda.is_available()
    except Exception:
        return False

def key_compose(src, wav, out_webm):
    sh(["ffmpeg", "-y", "-i", src, "-i", wav,
        "-filter_complex", "[0:v]chromakey=0x18A03C:0.15:0.08,format=rgba,scale=640:640:flags=lanczos[av]",
        "-map", "[av]", "-map", "1:a", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "1.5M",
        "-auto-alt-ref", "0", "-c:a", "libopus", "-b:a", "96k", "-shortest", out_webm])
    log("keyed ->", out_webm)

def maybe_r2(out_webm, key):
    need = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    if not all(os.environ.get(k) for k in need):
        log("R2 envs not set; clip stays local:", out_webm); return
    import boto3
    s3 = boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
    s3.upload_file(out_webm, os.environ["R2_BUCKET"], os.environ.get("R2_PREFIX", "clips/") + key)
    log("R2 uploaded", key)

KV_NS = os.environ.get("CF_KV_NAMESPACE_ID", "d6a6491dbc734c74b8bfde90689fe9ab")  # PROMO_STATE2

def maybe_upload(out_webm, key):
    """Primary upload: KV via wrangler CLI (remote flag = real namespace, not local emulator)."""
    kv_key = f"{os.environ.get('CLIP_PREFIX', 'clips/')}{key}"
    try:
        sh(["npx", "--yes", "wrangler", "kv", "key", "put", "--remote",
            f"--namespace-id={KV_NS}", kv_key, "--path", out_webm])
        log("KV uploaded", kv_key)
        return True
    except Exception as e:
        log("KV upload failed, falling back:", e)
        maybe_r2(out_webm, key)
        return False

def tts_timings(text):
    """Estimate rough caption windows: split at sentence boundaries, proportional to word count."""
    import re
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    if len(parts) <= 1:
        return [[text.upper(), 0.0, 9.0]]
    total_words = sum(len(p.split()) for p in parts)
    secs_per_word = 0.42
    t, caps = 0.0, []
    for p in parts:
        dur = max(1.4, len(p.split()) * secs_per_word)
        caps.append([p.upper(), round(t, 1), round(t + dur, 1)])
        t += dur + 0.2
    return caps

def build_state(name_webms, lines):
    """Roll a new state.json playlist from what we just rendered."""
    segs = []
    for item in lines:
        name = item["name"].replace(" ", "_")
        segs.append({
            "clip": f"clips/{name}.webm",
            "talent": item.get("talent", "a"),
            "title": item.get("title", ""),
            "captions": item.get("captions") or tts_timings(item["text"]),
        })
    return {"segments": segs, "ticker": "ABET SPORTS DESK - LIVE", "version": 2}

def upload_state(state):
    body = json.dumps(state)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(body); path = f.name
    sh(["npx", "--yes", "wrangler", "kv", "key", "put", "--remote",
        f"--namespace-id={KV_NS}", "state.json", "--path", path])
    log("KV uploaded state.json (", len(state.get("segments", [])), "segments )")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", required=True)
    ap.add_argument("--portrait", required=True)
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--no-state", action="store_true")
    a = ap.parse_args()
    ensure_repo(); ensure_ckpts()
    os.makedirs(a.outdir, exist_ok=True)
    lines = json.load(open(a.lines, encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        for item in lines:
            name = item["name"].replace(" ", "_")
            wav = os.path.join(tmp, name + ".wav")
            tts(item["text"], wav)
            raw = sadtalker_render(wav, os.path.abspath(a.portrait), tmp)
            out = os.path.join(a.outdir, name + ".webm")
            key_compose(raw, wav, out)
            if not a.no_upload:
                maybe_upload(out, name + ".webm")
    if not a.no_state and not a.no_upload:
        upload_state(build_state(None, lines))
    log("BATCH DONE:", len(lines), "clips in", a.outdir)

if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()

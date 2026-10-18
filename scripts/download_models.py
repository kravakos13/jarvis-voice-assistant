"""Download offline models: Piper voice(s) from rhasspy/piper-voices (Hugging Face) and the Whisper model.

    python scripts/download_models.py                       # voice from config + whisper from config
    python scripts/download_models.py --voice ru_RU-dmitri-medium
    python scripts/download_models.py --all-ru-voices
    python scripts/download_models.py --no-whisper
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import hfquiet  # noqa: E402

hfquiet.quiet()     # must precede any huggingface_hub import (hides symlink / HF_TOKEN warnings)

import requests  # noqa: E402

HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
RU_VOICES = ["ru_RU-denis-medium", "ru_RU-dmitri-medium", "ru_RU-ruslan-medium", "ru_RU-irina-medium"]


def voice_urls(voice: str) -> list[str]:
    # ru_RU-denis-medium -> ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx
    lang_region, name, quality = voice.split("-", 2)
    lang = lang_region.split("_")[0]
    base = f"{HF}/{lang}/{lang_region}/{name}/{quality}/{voice}"
    return [base + ".onnx", base + ".onnx.json"]


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  уже есть: {dest.name}")
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {dest.name}: {done * 100 // total}% ({done >> 20} / {total >> 20} МБ)", end="", flush=True)
    print()
    tmp.replace(dest)


def main() -> int:
    from jarvis.config import load_config, resolve_path
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default=cfg["tts"].get("piper_voice", "ru_RU-denis-medium"))
    ap.add_argument("--all-ru-voices", action="store_true")
    ap.add_argument("--no-whisper", action="store_true")
    ap.add_argument("--whisper", default=cfg["stt"].get("model", "small"))
    args = ap.parse_args()

    out = resolve_path(cfg["tts"].get("voices_dir", "voices"))
    out.mkdir(parents=True, exist_ok=True)
    voices = RU_VOICES if args.all_ru_voices else [args.voice]
    ok = True
    for v in voices:
        print(f"Голос Piper {v}:")
        for url in voice_urls(v):
            try:
                download(url, out / url.rsplit("/", 1)[1])
            except Exception as e:
                ok = False
                print(f"  ОШИБКА {url}: {e}")
    if not args.no_whisper:
        ok = download_whisper(args.whisper, resolve_path(cfg["stt"].get("download_root", "models/whisper"))) and ok
    return 0 if ok else 1


def download_whisper(size: str, cache_dir: Path, attempts: int = 3) -> bool:
    """faster-whisper hides its progress bar -> print folder size every 2 s so it never looks frozen."""
    import logging
    import time
    repo = hfquiet.repo_for(size)
    total = hfquiet.expected_size(repo)
    print(f"Модель распознавания речи Whisper «{size}»"
          + (f" ({hfquiet.fmt_mb(total)} МБ)" if total else "") + " — скачивается один раз, подожди...")
    for n in range(1, attempts + 1):
        try:
            from faster_whisper import download_model
            hfquiet.quiet()          # faster_whisper may have reset logger levels on import
            with hfquiet.SizeMonitor(hfquiet.repo_folder(cache_dir, repo), total=total):
                path = download_model(size, cache_dir=str(cache_dir))
            print(f"\n  готово: {path}")
            return True
        except Exception as e:
            logging.getLogger("jarvis.install").debug("whisper download failed", exc_info=True)
            print(f"\n  Не удалось скачать Whisper (попытка {n} из {attempts}): {type(e).__name__}")
            if n < attempts:
                time.sleep(5 * n)
    print("  Whisper не скачан — Джарвис докачает его при первом запуске (нужен интернет).")
    return False


if __name__ == "__main__":
    raise SystemExit(main())

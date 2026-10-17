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

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
        print(f"Модель Whisper '{args.whisper}' (faster-whisper, скачивается один раз)...")
        try:
            from faster_whisper import download_model
            path = download_model(args.whisper, cache_dir=str(resolve_path(cfg["stt"].get("download_root", "models/whisper"))))
            print(f"  готово: {path}")
        except Exception as e:
            ok = False
            print(f"  ОШИБКА: {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

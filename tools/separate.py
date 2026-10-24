"""Split a song into <name>_vocals.wav and <name>_instrumental.wav with Demucs.

    python tools/separate.py songs/mysong/original.mp3
"""
import argparse
import sys
from pathlib import Path

import soundfile as sf
import torch
from demucs.api import Separator
from demucs.audio import convert_audio


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("song", type=Path, help="WAV, MP3, FLAC or OGG")
    p.add_argument("-o", "--out-dir", type=Path, help="default: same folder as the song")
    p.add_argument("--model", default="htdemucs_ft", help="htdemucs_ft (best, default) or htdemucs (4x faster)")
    p.add_argument("--shifts", type=int, default=1, help="random-shift passes; 2-5 is slightly cleaner but slower")
    a = p.parse_args()

    try:
        audio, sr = sf.read(str(a.song), dtype="float32", always_2d=True)
    except Exception as e:
        sys.exit(f"Can't read {a.song}: {e}\nConvert it to WAV or MP3 first (e.g. open and export it in Audacity).")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Separating {a.song.name} with {a.model} on {device}...")
    sep = Separator(model=a.model, device=device, shifts=a.shifts, progress=True)
    wav = convert_audio(torch.from_numpy(audio.T.copy()), sr, sep.samplerate, sep.audio_channels)
    _, stems = sep.separate_tensor(wav)

    out_dir = a.out_dir or a.song.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    vocals = stems["vocals"]
    instrumental = sum(w for name, w in stems.items() if name != "vocals")
    for suffix, w in (("vocals", vocals), ("instrumental", instrumental)):
        path = out_dir / f"{a.song.stem}_{suffix}.wav"
        y = w.cpu().numpy().T
        if sep.samplerate != sr:
            # Demucs works at 44.1 kHz; hand the tracks back at the song's own rate so every file in the
            # folder shares one rate (mixing 44.1 kHz samples as 48 kHz plays them 9% fast and 1.5 st high)
            import soxr
            y = soxr.resample(y, sep.samplerate, sr)
        sf.write(str(path), y[: len(audio)], sr, subtype="FLOAT")
        print(f"Wrote {path} ({sr} Hz)")


if __name__ == "__main__":
    main()

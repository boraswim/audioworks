"""Amp-style vocal saturation, for originals whose vocal was driven through an amp or distortion.

Band-limits like a small amp, soft-clips with a slight asymmetry (warmer than symmetric
clipping), rounds off the top like a speaker cabinet, then restores the input level.
Measured signs of a distorted vocal: a flattened waveform (low kurtosis) and grit
filling the gaps between harmonics (high spectral flatness at 1-5 kHz).

  python tools/distort.py take_deessed.wav --drive 12 -o take_driven.wav
  python tools/distort.py take_deessed.wav --analyze --ref song_vocals.wav     measure only
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt, stft
from scipy.stats import kurtosis

sys.path.insert(0, str(Path(__file__).parent))
import match  # noqa: E402


def drive(x, sr, drive_db, bias=0.15):
    """Soft-clip `x` with `drive_db` of gain into the clipper; output at the input's RMS."""
    if drive_db <= 0:
        return x.copy()
    pre = sosfiltfilt(butter(2, [120, 10000], "bandpass", fs=sr, output="sos"), x)
    g = 10 ** (drive_db / 20) / (np.percentile(np.abs(pre), 99.5) + 1e-9)  # drive relative to the take's peaks
    y = np.tanh(g * pre + bias) - np.tanh(bias)  # asymmetric: even harmonics, warmer
    # gentle cabinet roll-off; a 7.5 kHz one left the vocal 8-19 dB too dark above 5 kHz after matching
    y = sosfiltfilt(butter(1, 11000, "low", fs=sr, output="sos"), y)
    return y * np.sqrt(np.mean(x ** 2) / (np.mean(y ** 2) + 1e-20))


def grit(m, sr, sung):
    """(kurtosis, flatness 1-5 kHz) while singing: the two measured signs of distortion."""
    n = int(0.05 * sr)
    k = len(m) // n
    blocks = m[: k * n].reshape(k, n)
    full = sung[: k * n].reshape(k, n).all(axis=1)
    kurt = float(np.median(kurtosis(blocks[full], axis=1)))
    f, _, z = stft(m, sr, nperseg=2048)
    p = np.abs(z) ** 2
    frames = np.clip(np.arange(p.shape[1]) * 1024, 0, len(sung) - 1)
    pb = p[(f >= 1000) & (f < 5000)][:, sung[frames]] + 1e-20
    flat = float(np.median(np.exp(np.mean(np.log(pb), axis=0)) / np.mean(pb, axis=0)))
    return kurt, flat


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("vocal", type=Path)
    p.add_argument("-o", "--output", type=Path, help="default: <vocal>_driven.wav")
    p.add_argument("--drive", type=float, default=12.0, help="dB of gain into the clipper (0 = clean)")
    p.add_argument("--analyze", action="store_true", help="only report kurtosis and flatness")
    p.add_argument("--ref", type=Path, help="with --analyze: also measure this vocal")
    a = p.parse_args()

    x, sr = sf.read(str(a.vocal), always_2d=True)
    m = x.mean(axis=1)
    if a.analyze:
        for label, path in (("this vocal", a.vocal),) + ((("reference", a.ref),) if a.ref else ()):
            y, ysr = sf.read(str(path), always_2d=True)
            ym = y.mean(axis=1)
            k, fl = grit(ym, ysr, match.sung_mask(ym, ysr))
            print(f"{label}: kurtosis {k:+.2f} (lower = more flattened), flatness 1-5 kHz {fl:.3f} (higher = grittier)")
        return
    y = drive(m, sr, a.drive)
    out = a.output or a.vocal.with_name(f"{a.vocal.stem}_driven.wav")
    sf.write(str(out), y, sr, subtype="FLOAT")
    print(f"Wrote {out} (drive {a.drive:g} dB)")


if __name__ == "__main__":
    main()

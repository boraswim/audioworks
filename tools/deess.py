"""De-esser: turns down harsh "s", "sh" and "t" sounds without dulling the rest of the vocal.

Only the high band (above --split Hz) is reduced, and only in moments where it
dominates the sound, so vowels keep their brightness.

  python tools/deess.py take.wav                    gentle default
  python tools/deess.py take.wav --amount strong
  python tools/deess.py take.wav --analyze          report only
"""
import argparse
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import istft, stft

# amount -> (threshold dB of high band relative to full band, ratio, max reduction dB)
AMOUNTS = {"light": (-10.0, 3.0, 6.0), "medium": (-14.0, 4.0, 10.0), "strong": (-18.0, 6.0, 15.0)}
NPERSEG, HOP = 1024, 256


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("vocal", type=Path)
    p.add_argument("-o", "--output", type=Path, help="default: <vocal>_deessed.wav")
    p.add_argument("--amount", choices=AMOUNTS, default="medium")
    p.add_argument("--split", type=float, default=4500.0, help="sibilance band starts here (Hz); 3500-6000 typical")
    p.add_argument("--analyze", action="store_true")
    a = p.parse_args()
    threshold, ratio, max_red = AMOUNTS[a.amount]

    audio, sr = sf.read(str(a.vocal), dtype="float64", always_2d=True)
    freqs, _, spec = stft(audio.T, sr, nperseg=NPERSEG, noverlap=NPERSEG - HOP)  # (ch, bins, frames)
    power = (np.abs(spec) ** 2).sum(axis=0)
    band = freqs >= a.split
    eps = 1e-12
    full_db = 10 * np.log10(power.sum(axis=0) + eps)
    band_db = 10 * np.log10(power[band].sum(axis=0) + eps)
    rel_db = band_db - full_db

    loud_enough = full_db > full_db.max() - 50  # leave silence/noise alone
    reduction = np.where(loud_enough, np.clip((rel_db - threshold) * (1 - 1 / ratio), 0, max_red), 0.0)
    # fast attack (hold the peak ~3 frames), smooth release
    reduction = uniform_filter1d(maximum_filter1d(reduction, 3), 3)

    active = reduction > 1.0
    sung = loud_enough.sum()
    print(f"Sibilance reduced in {active.sum() / max(sung, 1) * 100:.1f}% of non-silent frames"
          f" ({a.amount}: max {reduction.max():.1f} dB, avg {reduction[active].mean() if active.any() else 0:.1f} dB when active)")
    if a.analyze:
        return

    gain = np.ones_like(power)
    gain[band] = 10 ** (-reduction / 20)
    _, out = istft(spec * gain[None], sr, nperseg=NPERSEG, noverlap=NPERSEG - HOP)
    out = out[:, : len(audio)].T
    path = a.output or a.vocal.with_name(f"{a.vocal.stem}_deessed.wav")
    sf.write(str(path), out if out.shape[1] > 1 else out[:, 0], sr, subtype="FLOAT")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()

"""Vocal doubler: fakes extra takes of the same part for a thicker, wider vocal.

Each double comes in a little late (a different amount per phrase), with slightly
drifting pitch (a few cents) and level, like a singer re-recording the line.
Doubles are panned left/right into one stereo file to tuck under the lead.

  python tools/double.py take_tuned.wav                    2 doubles, L/R
  python tools/double.py take_tuned.wav --voices 4 --tightness loose
  python tools/double.py take_tuned.wav --with-lead        lead in the middle too
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import binary_closing, gaussian_filter1d

sys.path.insert(0, str(Path(__file__).parent))
import music  # noqa: E402

# tightness -> (average delay ms, per-phrase delay spread ms, pitch drift cents, level drift dB)
TIGHTNESS = {"tight": (12.0, 5.0, 5.0, 0.5), "natural": (20.0, 10.0, 9.0, 1.0), "loose": (30.0, 18.0, 15.0, 1.5)}


def drift(rng, n, smooth_s, rate=100):
    """Smooth random curve in -1..1 with n points at `rate` Hz (built at 100 Hz, then interpolated)."""
    k = max(int(n * 100 / rate), 2)
    x = gaussian_filter1d(rng.standard_normal(k), 100 * smooth_s, mode="wrap")
    x /= np.abs(x).max() + 1e-12
    return np.interp(np.linspace(0, k - 1, n), np.arange(k), x)


def phrase_delays(rng, times, voiced, n, sr, delay_ms, delay_drift):
    """Per-sample delay (in samples): constant within each phrase, changing only in the gaps.

    A delay that changes while a note sounds bends its pitch (Doppler), so it's held steady.
    """
    phrases = binary_closing(voiced, np.ones(25))  # gaps under 0.25 s don't split a phrase
    ctrl = np.full(len(voiced), np.nan)
    for seg in music.voiced_segments(phrases):
        ctrl[seg] = max(delay_ms + delay_drift * rng.uniform(-1, 1), 0)
    known = np.flatnonzero(~np.isnan(ctrl))
    ctrl = np.interp(np.arange(len(ctrl)), known, ctrl[known]) if known.size else np.full(len(ctrl), delay_ms)
    return np.interp(np.arange(n) / sr, times, ctrl) * sr / 1000


def make_double(sound, times, f0, voiced, rng, delay_ms, delay_drift, cents, level_db, floor, ceiling):
    sr = sound.sampling_frequency
    y = music.psola(sound, times, np.where(voiced, f0 * 2 ** (cents * drift(rng, len(f0), 0.4) / 1200), np.nan),
                    floor, ceiling)
    n = len(y)
    delay = phrase_delays(rng, times, voiced, n, sr, delay_ms, delay_drift)
    y = np.interp(np.arange(n) - delay, np.arange(n), y, left=0.0)
    return y * 10 ** (level_db * drift(rng, n, 0.8, sr) / 20)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("take", type=Path)
    p.add_argument("-o", "--output", type=Path, help="default: <take>_doubles.wav")
    p.add_argument("--voices", type=int, default=2, help="number of doubles (alternately panned L/R)")
    p.add_argument("--tightness", choices=TIGHTNESS, default="natural")
    p.add_argument("--width", type=float, default=0.8, help="stereo spread 0 (mono) - 1 (hard L/R)")
    p.add_argument("--with-lead", action="store_true", help="also put the original lead in the center")
    p.add_argument("--match-width", type=Path,
                   help="with --with-lead: pick the width so the stereo spread matches this reference vocal")
    p.add_argument("--seed", type=int, default=7, help="change for a different 'performance'")
    p.add_argument("--floor", type=float, default=70.0)
    p.add_argument("--ceiling", type=float, default=900.0)
    a = p.parse_args()

    delay_ms, delay_drift, cents, level_db = TIGHTNESS[a.tightness]
    sound = music.load_sound(a.take)
    times, f0 = music.track_pitch(sound, a.floor, a.ceiling)
    voiced = ~np.isnan(f0)
    rng = np.random.default_rng(a.seed)
    lead = sound.values[0]
    doubles = [make_double(sound, times, f0, voiced, rng, delay_ms, delay_drift, cents, level_db,
                           a.floor, a.ceiling)[: len(lead)] for _ in range(a.voices)]

    def build(width):
        mix = np.zeros((len(lead), 2))
        for v, y in enumerate(doubles):
            pan = width * (-1 if v % 2 == 0 else 1) * (1 - 0.3 * (v // 2) / max(1, a.voices // 2))
            angle = (pan + 1) * np.pi / 4  # equal-power pan
            mix[:, 0] += y * np.cos(angle)
            mix[:, 1] += y * np.sin(angle)
        mix /= np.sqrt(a.voices)  # keep the stack about as loud as one voice
        if a.with_lead:
            mix += lead[:, None] * np.sqrt(0.5)
        return mix

    width = a.width
    if a.match_width:
        import match

        sr = int(sound.sampling_frequency)
        ref, _ = sf.read(str(a.match_width), always_2d=True)
        sung = match.sung_mask(lead, sr)

        def side_mid(x, s):
            return 10 * np.log10(np.sum(((x[:, 0] - x[:, 1]) / 2)[s] ** 2) / np.sum(((x[:, 0] + x[:, 1]) / 2)[s] ** 2))

        target = side_mid(ref, match.sung_mask(ref.mean(axis=1), sr))
        width = min((abs(side_mid(build(w), sung) - target), w) for w in np.arange(0.1, 1.01, 0.05))[1]
        print(f"Width: {width:.2f} -> side/mid {side_mid(build(width), sung):.1f} dB (reference {target:.1f})")
    mix = build(width)

    out = a.output or a.take.with_name(f"{a.take.stem}_doubles.wav")
    sf.write(str(out), mix, int(sound.sampling_frequency), subtype="FLOAT")
    print(f"Wrote {out}  ({a.voices} doubles, {a.tightness}, width {width:.2f}{', with lead' if a.with_lead else ''}; peak {np.abs(mix).max():.2f})")


if __name__ == "__main__":
    main()

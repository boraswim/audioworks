"""Vocoder: re-synthesize a vocal on a buzzy synth tone that plays the vocal's own pitch.

Run it on a tuned take, so the synth plays clean notes. A channel vocoder: the voice's
spectral envelope (per band) shapes a band-limited sawtooth that follows the voice's
pitch; unvoiced moments (consonants) use a noise carrier so the words stay clear.

Measurement can't tell a vocoded original from a hard-tuned one (separated stems all read
~12-16 dB harmonics-to-noise), so this is an optional, by-ear effect: offer a vocoded
version next to the plain one and let the user choose. Its output is more periodic
(harmonics-to-noise ~20 dB) than a dry voice (~18 dB).

  python tools/vocoder.py take_tuned.wav -o take_vocoded.wav
  python tools/vocoder.py take_tuned.wav --mix 0.6 --bands 32 -o take_vocoded.wav
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

sys.path.insert(0, str(Path(__file__).parent))
import music  # noqa: E402

STEP = 0.01


def carrier(n, sr, times, f0, seed=11):
    """Sawtooth following f0 (short gaps bridged), noise where unvoiced, crossfaded."""
    voiced = ~np.isnan(f0)
    lf = np.log(np.where(voiced, f0, 1.0))
    idx = np.flatnonzero(voiced)
    if idx.size == 0:
        sys.exit("No pitched singing found.")
    lf = np.interp(np.arange(len(f0)), idx, lf[idx])  # hold pitch through gaps for phase continuity
    # bridge gaps under 80 ms so a word doesn't flicker between saw and noise
    gaps = ~voiced
    run_start = None
    for i, g in enumerate(np.r_[gaps, False]):
        if g and run_start is None:
            run_start = i
        elif not g and run_start is not None:
            if i - run_start < 8 and run_start > 0 and i < len(voiced):
                voiced[run_start:i] = True
            run_start = None
    t = np.arange(n) / sr
    freq = np.exp(np.interp(t, times, lf))
    dt = freq / sr
    phase = np.cumsum(dt) % 1.0
    saw = 2 * phase - 1
    # PolyBLEP: soften the jump to keep aliasing down
    lo, hi = phase < dt, phase > 1 - dt
    x = np.where(lo, phase / dt, 0)
    saw -= np.where(lo, 2 * x - x * x - 1, 0)
    y = np.where(hi, (phase - 1) / dt, 0)
    saw -= np.where(hi, y * y + 2 * y + 1, 0)
    v = np.interp(t, times, voiced.astype(float))
    v = np.convolve(v, np.ones(int(0.01 * sr)) / int(0.01 * sr), mode="same")  # 10 ms crossfade
    noise = np.random.default_rng(seed).standard_normal(n) * 0.5
    return v * saw + (1 - v) * noise


def vocode(m, sr, bands=24, fmin=80.0, fmax=12000.0, env_hz=40.0):
    import parselmouth

    times, f0 = music.track_pitch(parselmouth.Sound(m, sampling_frequency=sr), step=STEP)
    c = carrier(len(m), sr, times, f0)
    edges = np.geomspace(fmin, min(fmax, 0.45 * sr), bands + 1)
    env_sos = butter(2, env_hz, "low", fs=sr, output="sos")
    out = np.zeros_like(m)
    for lo, hi in zip(edges[:-1], edges[1:]):
        sos = butter(4, [lo, hi], "bandpass", fs=sr, output="sos")
        mb, cb = sosfiltfilt(sos, m), sosfiltfilt(sos, c)
        em = np.maximum(sosfiltfilt(env_sos, np.abs(mb)), 0)
        # fixed per-band gain (not a moment-to-moment division): a band the synth tone has no
        # harmonic in right now stays quiet instead of blowing its leakage up into spikes
        out += cb * em / (np.sqrt(np.mean(cb ** 2)) + 1e-12)
    return out * np.sqrt(np.mean(m ** 2) / (np.mean(out ** 2) + 1e-20))


def hnr(m, sr, pitch=None, win=0.04):
    """Harmonics-to-noise (dB) from how well each sung 40 ms window repeats one pitch period later.

    Uses a known pitch track (`pitch` = (times, f0)), which stays reliable on synthetic or
    vocoded sound where Praat's own harmonicity estimate breaks down.
    """
    import parselmouth

    times, f0 = pitch if pitch is not None else music.track_pitch(parselmouth.Sound(m, sampling_frequency=sr), step=STEP)
    w = int(win * sr)
    vals = []
    for t, f in zip(times[::3], f0[::3]):  # every 30 ms is plenty
        if np.isnan(f):
            continue
        lag = int(round(sr / f))
        a = int(t * sr) - w // 2
        if a < 0 or a + w + lag > len(m):
            continue
        x, y = m[a:a + w], m[a + lag:a + lag + w]
        r = np.dot(x, y) / (np.sqrt(np.dot(x, x) * np.dot(y, y)) + 1e-20)
        if r > 0:
            vals.append(10 * np.log10(min(r, 0.9999) / (1 - min(r, 0.9999))))
    return float(np.median(vals)) if vals else float("nan")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("vocal", type=Path, help="tuned vocal (mono)")
    p.add_argument("-o", "--output", type=Path, help="default: <vocal>_vocoded.wav")
    p.add_argument("--bands", type=int, default=24, help="more bands = clearer words, fewer = more robotic")
    p.add_argument("--mix", type=float, default=1.0, help="1 = fully vocoded, 0 = dry")
    a = p.parse_args()

    m, sr = sf.read(str(a.vocal))
    m = m.mean(axis=1) if m.ndim == 2 else m
    y = a.mix * vocode(m, sr, a.bands) + (1 - a.mix) * m
    out = a.output or a.vocal.with_name(f"{a.vocal.stem}_vocoded.wav")
    sf.write(str(out), y, sr, subtype="FLOAT")
    print(f"Wrote {out} ({a.bands} bands, mix {a.mix:.2f}, harmonics-to-noise {hnr(y, sr):.1f} dB)")


if __name__ == "__main__":
    main()

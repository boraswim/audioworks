"""Line a vocal take up with the original song and write an aligned copy.

Measures how early (or late) the take started with two independent methods (onset
rhythm and pitch-contour matching), checks the offset section by section to tell a
steady offset from clock drift or loose timing, then writes a mono copy that starts
exactly with the song, at the song's sample rate and length.

  python tools/align.py songs/x/take1.wav --ref songs/x/song_vocals.wav --song songs/x/song.mp3
  python tools/align.py songs/x/take1.wav --ref songs/x/song_vocals.wav --song songs/x/song.mp3 --offset 0.97
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent))
import music  # noqa: E402

STEP = 0.01


def onset_offset(take, ref, max_s=5.0):
    import librosa

    def env(p):
        y, sr = librosa.load(str(p), sr=16000, mono=True)
        e = librosa.onset.onset_strength(y=y, sr=sr, hop_length=160)
        return (e - e.mean()) / (e.std() + 1e-9)

    a, b = env(take), env(ref)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    lags = np.arange(-int(max_s / STEP), int(max_s / STEP))
    cc = np.array([np.mean(a[max(l, 0):n + min(l, 0)] * b[max(-l, 0):n - max(l, 0)]) for l in lags])
    k = int(np.argmax(cc))
    return lags[k] * STEP, cc[k], np.max(cc[np.abs(lags - lags[k]) > 20])


def note_match(ma, mb, lag, start=0, end=None, min_frames=400):
    """Share of jointly sung frames on the same note (octave-free) when the take is `lag` frames late."""
    x = ma[start + max(lag, 0):(end + lag if end is not None else None)]
    y = mb[start + max(-lag, 0):end]
    m = min(len(x), len(y))
    ok = ~np.isnan(x[:m]) & ~np.isnan(y[:m])
    if ok.sum() < min_frames:
        return None
    return np.mean(np.abs(((x[:m][ok] - y[:m][ok]) + 6) % 12 - 6) < 0.75)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("take", type=Path)
    p.add_argument("--ref", type=Path, required=True, help="original vocal stem (song_vocals.wav)")
    p.add_argument("--song", type=Path, help="original song; sets output length and sample rate")
    p.add_argument("--offset", type=float, help="use this offset (s, positive = take started early) instead of measuring")
    p.add_argument("-o", "--output", type=Path, help="default: <take>_aligned.wav")
    a = p.parse_args()

    if a.offset is None:
        t_off, t_cc, t_next = onset_offset(a.take, a.ref)
        _, fa = music.track_pitch(music.load_sound(a.take), step=STEP)
        _, fb = music.track_pitch(music.load_sound(a.ref), step=STEP)
        ma, mb = music.hz_to_midi(fa), music.hz_to_midi(fb)
        scores = [(s, l) for l in range(-500, 500) if (s := note_match(ma, mb, l, min_frames=2000)) is not None]
        best_s, best_l = max(scores)
        elsewhere = max(s for s, l in scores if abs(l - best_l) > 20)
        print(f"Onset rhythm:  take content {t_off:+.2f} s late (match {t_cc:.2f}, best elsewhere {t_next:.2f})")
        print(f"Pitch contour: take content {best_l * STEP:+.2f} s late ({100 * best_s:.0f}% same note, best elsewhere {100 * elsewhere:.0f}%)")
        if abs(t_off - best_l * STEP) > 0.1:
            print("  WARNING: the two methods disagree by more than 0.1 s; check by ear or pass --offset")

        # section by section, around the global answer
        pts = []
        for s in range(0, int(len(mb) * STEP) - 30, 20):
            res = [(sc, l) for l in range(best_l - 30, best_l + 30)
                   if (sc := note_match(ma, mb, l, int(s / STEP), int((s + 30) / STEP))) is not None]
            if res and max(res)[0] > 0.55:
                sc, l = max(res)
                pts.append((s + 15, l * STEP))
        if len(pts) >= 3:
            pt = np.array(pts)
            slope, icpt = np.polyfit(pt[:, 0], pt[:, 1], 1)
            resid = pt[:, 1] - (slope * pt[:, 0] + icpt)
            print("Per section:   " + "  ".join(f"{int(t) // 60}:{int(t) % 60:02d} {o:+.2f}" for t, o in pts))
            spread = pt[:, 1].max() - pt[:, 1].min()
            drift = slope * 60000
            if spread < 0.04:
                kind = "steady"
            elif abs(drift) > 10 and resid.std() < 0.015:
                kind = f"steady clock drift of {drift:+.0f} ms/min (consider stretching the take)"
            else:
                kind = "loose singing timing (a single offset is the right fix)"
            print(f"Timing:        {kind}; section offsets {pt[:, 1].min():+.2f} to {pt[:, 1].max():+.2f} s")
            offset = float(np.median(pt[:, 1]))
            print(f"  worst section is {1000 * np.abs(pt[:, 1] - offset).max():.0f} ms from the chosen offset")
        else:
            offset = best_l * STEP
    else:
        offset = a.offset

    x, sr = sf.read(str(a.take), always_2d=True)
    m = x.mean(axis=1)
    out_sr, n_out = (sf.info(str(a.song)).samplerate, sf.info(str(a.song)).frames) if a.song else (sr, None)
    shift = int(round(abs(offset) * sr))
    m = m[shift:] if offset >= 0 else np.r_[np.zeros(shift), m]
    if out_sr != sr:
        import soxr
        m = soxr.resample(m, sr, out_sr)
    if n_out:
        m = np.pad(m, (0, max(0, n_out - len(m))))[:n_out]
    out = a.output or a.take.with_name(f"{a.take.stem}_aligned.wav")
    sf.write(str(out), m, out_sr, subtype="FLOAT")
    print(f"Offset used:   {offset:+.2f} s ({'take started early' if offset >= 0 else 'take started late'})")
    print(f"Wrote {out} ({len(m) / out_sr:.2f} s, mono, {out_sr} Hz)")


if __name__ == "__main__":
    main()

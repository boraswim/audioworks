"""Mix your processed vocal with the instrumental and master it like the original.

Sets the vocal's loudness relative to the instrumental from the original's own balance
(plus --above dB), matches the original song's loudness (LUFS), and limits only the
stray peaks above -1 dBFS. Writes a float WAV to import into Audacity for export.
Folds to mono automatically when the original is a mono recording. A stereo (widened)
vocal is kept stereo.

  python tools/mixdown.py songs/x --vocal songs/x/take1_processed.wav -o mix.wav
  python tools/mixdown.py songs/x --vocal ... --above 1.5 --stereo
"""
import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent))
import match  # noqa: E402

warnings.filterwarnings("ignore")


def stereo(x):
    return x if x.ndim == 2 and x.shape[1] == 2 else np.repeat(x.reshape(-1, 1), 2, axis=1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", type=Path, help="song folder with song.mp3 and the stems")
    p.add_argument("--vocal", type=Path, required=True, help="processed vocal (from match.py)")
    p.add_argument("--above", type=float, default=2.0,
                   help="dB louder than the original vocal balance (the user found +1 still a bit low)")
    p.add_argument("--loudness", type=float, help="target LUFS (default: the original song's)")
    p.add_argument("--max-limit", default="auto",
                   help="most peak reduction allowed (dB) before giving up loudness; 'auto' = 6, or up to 7.5 "
                        "when the limiter would only touch stray peaks (under 1%% of samples)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--mono", action="store_true")
    g.add_argument("--stereo", action="store_true")
    p.add_argument("-o", "--output", type=Path, required=True)
    a = p.parse_args()

    d = a.folder
    song, sr = sf.read(str(d / "song.mp3"), always_2d=True)

    def read_at_song_rate(path):
        x, xsr = sf.read(str(path), always_2d=True)
        if xsr != sr:  # never mix samples of different rates: it changes speed and pitch
            import soxr
            print(f"Converting {Path(path).name} from {xsr} to {sr} Hz")
            x = soxr.resample(x, xsr, sr)
        return x

    inst = read_at_song_rate(d / "song_instrumental.wav")
    orig_voc = read_at_song_rate(d / "song_vocals.wav")
    voc = read_at_song_rate(a.vocal)  # mono, or stereo when the vocal was widened
    voc = voc[:, 0] if voc.shape[1] == 1 else voc
    meter = pyloudnorm.Meter(sr)
    L = lambda x: meter.integrated_loudness(stereo(x))  # noqa: E731  (mono counted as both speakers)

    mono = a.mono or (not a.stereo and (song.shape[1] == 1 or np.corrcoef(song[:, 0], song[:, 1])[0, 1] > 0.99))
    n = min(len(inst), len(voc))
    inst, voc = inst[:n], voc[:n]
    orig_balance = L(orig_voc) - L(inst)
    gain_db = orig_balance + a.above - (L(voc) - L(inst))
    mix = stereo(inst) + stereo(voc) * 10 ** (gain_db / 20)
    if mono:
        mix = mix.mean(axis=1)

    target = a.loudness if a.loudness is not None else L(song)
    y = mix * 10 ** ((target - L(mix)) / 20)
    need = match.db(np.abs(y).max()) + 1.0
    if a.max_limit == "auto":
        stray = np.mean(np.abs(y) > 10 ** (-1 / 20)) < 0.01  # only stray peaks would be limited
        max_limit = 7.5 if stray else 6.0
    else:
        max_limit = float(a.max_limit)
    if need > max_limit:
        print(f"Note: reaching {target:.1f} LUFS needs {need:.1f} dB of peak limiting; capping at {max_limit:g} dB"
              f" so the result will be {need - max_limit:.1f} dB quieter than the original")
        target -= need - max_limit
        y = mix * 10 ** ((target - L(mix)) / 20)
    before = y.copy()
    lim = (lambda z: match.limit(z, sr, -1.0)) if mono else \
        (lambda z: np.stack([match.limit(z[:, c], sr, -1.0) for c in range(2)], axis=1))
    for _ in range(4):
        y = lim(y)
        y *= 10 ** ((target - L(y)) / 20)
    y = lim(y)

    def crest(z):
        z = z.mean(axis=1) if z.ndim == 2 else z
        return match.db(np.abs(z).max()) - match.db(np.sqrt(np.mean(z ** 2)))

    def lra(z):
        w = 3 * sr
        v = np.array([meter.integrated_loudness(stereo(z[k:k + w])) for k in range(0, len(z) - w, sr)])
        v = v[v > -70]
        return np.percentile(v, 95) - np.percentile(v, 10)

    print(f"Vocal gain {gain_db:+.1f} dB -> vocal vs instrumental {L(voc * 10 ** (gain_db / 20)) - L(inst):+.1f} LU"
          f" (original {orig_balance:+.1f}, target {orig_balance + a.above:+.1f})")
    print(f"{'Mono' if mono else 'Stereo'} master: {L(y):.1f} LUFS (original {L(song):.1f}), peak {match.db(np.abs(y).max()):+.2f} dBFS")
    print(f"Peak-to-average {crest(y):.1f} dB (original {crest(song):.1f}); loudness range {lra(y):.1f} LU (original {lra(song):.1f})")
    print(f"Limiter touched {100 * np.mean(np.abs(before) > 10 ** (-1 / 20)):.2f}% of samples")
    sf.write(str(a.output), y, sr, subtype="FLOAT")
    print(f"Wrote {a.output}")


if __name__ == "__main__":
    main()

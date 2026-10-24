"""In-app recording: plays the song in the headphones while recording the microphone.

Playback and capture run in one duplex stream, so they share a clock. Recording starts
`preroll` seconds before the music (the count-in), the same habit as recording a take by
hand; align.py then measures the exact offset, device latency included.
"""
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf


def host_api():
    """WASAPI on Windows (proper Unicode device names, low latency), else the default host API."""
    for i, h in enumerate(sd.query_hostapis()):
        if "WASAPI" in h["name"]:
            return i
    return sd.default.hostapi


def devices():
    """(inputs, outputs) as lists of (index, name), default devices first."""
    host = host_api()
    info = sd.query_hostapis(host)
    ins, outs = [], []
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != host:
            continue
        if d["max_input_channels"] > 0:
            ins.append((i, d["name"]))
        if d["max_output_channels"] > 0:
            outs.append((i, d["name"]))
    ins.sort(key=lambda x: x[0] != info["default_input_device"])
    outs.sort(key=lambda x: x[0] != info["default_output_device"])
    return ins, outs


class Recorder:
    def __init__(self, song_path, out_path, input_device=None, output_device=None, preroll=1.0, song_volume=0.8):
        self.song, self.sr = sf.read(str(song_path), dtype="float32", always_2d=True)
        if self.song.shape[1] == 1:
            self.song = np.repeat(self.song, 2, axis=1)
        self.song = self.song[:, :2] * song_volume
        self.preroll = preroll
        self.play = np.concatenate([np.zeros((int(preroll * self.sr), 2), "float32"), self.song])
        self.out_path = out_path
        self.device = (input_device, output_device)
        self.pos = 0
        self.chunks = []
        self.level = 0.0  # mic level of the last block, 0-1, for a meter
        self.done = threading.Event()
        self.stream = None

    @property
    def seconds(self):
        return self.pos / self.sr

    @property
    def song_time(self):
        """Position in the song the singer is hearing right now (count-in and output delay removed)."""
        out_latency = self.stream.latency[1] if self.stream is not None else 0.0
        return self.pos / self.sr - self.preroll - out_latency

    @property
    def length(self):
        return len(self.play) / self.sr

    def _callback(self, indata, outdata, frames, time, status):
        chunk = self.play[self.pos:self.pos + frames]
        outdata[:len(chunk)] = chunk
        outdata[len(chunk):] = 0
        self.chunks.append(indata[:, 0].copy())
        self.level = float(np.sqrt(np.mean(indata[:, 0] ** 2)))
        self.pos += frames
        if self.pos >= len(self.play):
            raise sd.CallbackStop

    def open(self):
        extra = None
        if "WASAPI" in sd.query_hostapis(host_api())["name"]:
            # shared-mode WASAPI only runs at the device's own rate unless Windows converts for us
            extra = sd.WasapiSettings(auto_convert=True)
        return sd.Stream(samplerate=self.sr, device=self.device, channels=(1, 2), dtype="float32",
                         callback=self._callback, finished_callback=self.done.set, extra_settings=extra)

    def start(self):
        self.stream = self.open()
        self.stream.start()

    def stop(self):
        """Stop (early or at the end) and save the take. Returns its length in seconds."""
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        take = np.concatenate(self.chunks) if self.chunks else np.zeros(1, "float32")
        sf.write(str(self.out_path), take, self.sr, subtype="PCM_16")
        return len(take) / self.sr

    def peak_db(self):
        take = np.concatenate(self.chunks) if self.chunks else np.zeros(1, "float32")
        return 20 * np.log10(np.abs(take).max() + 1e-12)

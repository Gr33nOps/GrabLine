"""Measure only synthetic-tone energy from Windows output loopback; save no audio."""

import array
import math
import site
import struct
import threading
import time
import wave

from common import ROOT, Blocked, case, record

site.addsitedir(str(ROOT / "audio-tools"))
import pyaudiowpatch as audio

from app.core import alerts

with case(
    "WINDOWS-SOUND-LOOPBACK", "production sound call + actual WASAPI output-loopback measurement"
) as e:
    backend = audio.PyAudio()
    try:
        try:
            device = backend.get_default_wasapi_loopback()
        except (OSError, LookupError) as exc:
            raise Blocked("No available Windows default-output loopback device") from exc
        rate = int(device["defaultSampleRate"])
        channels = int(device["maxInputChannels"])
        e.update(sample_rate=rate, channels=channels)
        tone = ROOT / "synthetic-completion.wav"
        with wave.open(str(tone), "wb") as writer:
            writer.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            values = [
                int(
                    9000
                    * (
                        math.sin(2 * math.pi * 1337 * i / rate)
                        + math.sin(2 * math.pi * 1919 * i / rate)
                    )
                    / 2
                )
                for i in range(int(rate * 0.6))
            ]
            writer.writeframes(struct.pack("<" + "h" * len(values), *values))
        metrics = []

        def received(data, frame_count, timing, status):
            samples = array.array("h", data)[::channels]
            n = len(samples)
            if n:
                total = sum(x * x for x in samples) / n
                # Retain aggregate energy only; discard all raw samples.
                powers = []
                for frequency in (1337, 1919, 2741):
                    real = sum(
                        x * math.cos(2 * math.pi * frequency * i / rate)
                        for i, x in enumerate(samples)
                    )
                    imaginary = sum(
                        x * math.sin(2 * math.pi * frequency * i / rate)
                        for i, x in enumerate(samples)
                    )
                    powers.append(2 * (real * real + imaginary * imaginary) / (n * n))
                metrics.append((time.monotonic(), total, powers))
            return (None, audio.paContinue)

        stream = backend.open(
            format=audio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=2048,
            stream_callback=received,
        )
        try:
            started = time.monotonic()
            threading.Event().wait(0.3)
            alerts.play_complete_sound(str(tone))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not any(
                p[0] + p[1] > 100_000 and p[0] + p[1] > 12 * (p[2] + 1) for _, _, p in metrics
            ):
                threading.Event().wait(0.05)
            matching = [
                p for _, _, p in metrics if p[0] + p[1] > 100_000 and p[0] + p[1] > 12 * (p[2] + 1)
            ]
            assert matching, "The synthetic two-tone was not detected at the output loopback"
            e.update(
                matching_chunks=len(matching),
                captured_chunks=len(metrics),
                raw_audio_saved=False,
                frequencies=[1337, 1919],
                seconds=round(time.monotonic() - started, 2),
            )
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        backend.terminate()
record(
    "WINDOWS-HUMAN-AUDIBILITY",
    "BLOCKED",
    "physical speaker/hearing confirmation",
    "Digital output-loopback measurement cannot establish speaker acoustics or a person's hearing.",
)

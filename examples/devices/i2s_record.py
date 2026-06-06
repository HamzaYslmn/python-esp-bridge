"""Record 5 s from an I2S MEMS mic (INMP441: BCLK->14, WS->15, SD->32)
and save a playable WAV on the host."""
import wave

from espbridge import Bridge

RATE = 16_000

with Bridge() as esp:
    esp.i2s.begin_input(bclk=14, ws=15, din=32, rate=RATE)
    print("recording 5 s...")
    pcm = esp.i2s.read(seconds=5)
    esp.i2s.end()

with wave.open("recording.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes(pcm)
print(f"saved recording.wav ({len(pcm)} bytes)")

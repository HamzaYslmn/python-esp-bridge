"""ADC + DAC: ramp the DAC (GPIO 25) and read it back on ADC (GPIO 34).

Wire GPIO 25 -> GPIO 34 with a jumper for a neat closed loop.
Note: classic-ESP32 only feature (S3 has no DAC).
"""
import time

from espbridge import Bridge

DAC_PIN = 25
ADC_PIN = 34

with Bridge() as esp:
    esp.adc.config(ADC_PIN, atten=11)  # full 0..3.3 V range
    for value in range(0, 256, 32):
        esp.dac.write(DAC_PIN, value)
        time.sleep(0.05)
        print(f"DAC={value:3d}  ->  ADC={esp.adc.read(ADC_PIN):4d} raw, "
              f"{esp.adc.read_mv(ADC_PIN):4d} mV")

    print("\n440 Hz cosine on the DAC for 3 s ...")
    esp.dac.cosine(DAC_PIN, 440)
    time.sleep(3)
    esp.dac.disable(DAC_PIN)      # stops the generator and releases the pin

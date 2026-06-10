"""HC-SR04 ultrasonic ranging: TRIG->GPIO5, ECHO->GPIO18.

The sensor runs at 5 V — divide ECHO down to 3.3 V (1k/2k divider).
"""
import time

from espbridge import Bridge

with Bridge() as esp:
    sonar = esp.hcsr04(trig=5, echo=18)   # bundled driver factory (docs/DRIVERS.md)
    try:
        while True:
            cm = sonar.distance_cm()
            print("out of range" if cm is None else f"{cm:6.1f} cm")
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass

"""RPi.GPIO-style blink — for porting old Raspberry Pi scripts unchanged.

Only the import changes; pin numbers are ESP32 GPIO numbers.
"""
import time

from espbridge.compat import rpi_gpio as GPIO  # was: import RPi.GPIO as GPIO

LED = 2

GPIO.setmode(GPIO.BCM)
GPIO.setup(LED, GPIO.OUT)

for _ in range(10):
    GPIO.output(LED, GPIO.HIGH)
    time.sleep(0.25)
    GPIO.output(LED, GPIO.LOW)
    time.sleep(0.25)

GPIO.cleanup()

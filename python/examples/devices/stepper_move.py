"""Stepper via an A4988/DRV8825 driver: STEP->GPIO12, DIR->GPIO14."""
import time

from espbridge import Bridge
from espbridge.drivers.stepper import Stepper

with Bridge() as esp:
    motor = Stepper(esp, step_pin=12, dir_pin=14)

    print("one revolution forward with a ramp...")
    motor.move(200, speed=800, accel=1600)   # 200 steps/rev (1.8 deg)
    time.sleep(0.5)

    print("back, constant speed...")
    motor.move(-200, speed=400)
    print(f"position: {motor.position}")

    print("free-running 3 s...")
    motor.run(600)
    time.sleep(3)
    motor.stop()

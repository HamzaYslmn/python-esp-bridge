"""PWM: LED fade on GPIO 16, hobby servo sweep on GPIO 13, buzzer on GPIO 27."""
import time

from espbridge import Bridge

LED = 16
SERVO = 13
BUZZER = 27

with Bridge() as esp:
    esp.pwm.attach(LED, freq=5000, resolution_bits=10)
    for pct in list(range(0, 101, 5)) + list(range(100, -1, -5)):
        esp.pwm.duty_pct(LED, pct)
        time.sleep(0.03)
    esp.pwm.detach(LED)

    for angle in (0, 90, 180, 90, 0):
        print(f"servo -> {angle} deg")
        esp.pwm.servo(SERVO, angle)
        time.sleep(0.7)
    esp.pwm.detach(SERVO)

    # tone() is duty-free PWM for piezo buzzers: just a frequency, 0 = off.
    for freq in (262, 330, 392, 523):       # C E G C
        esp.pwm.tone(BUZZER, freq)
        time.sleep(0.2)
    esp.pwm.tone(BUZZER, 0)
    esp.pwm.detach(BUZZER)

"""gpiozero pin factory: LED, Button (edge events), PWMLED via the bridge."""
import struct
import threading

import pytest

pytest.importorskip("gpiozero")
from gpiozero import LED, Button, PWMLED  # noqa: E402

from espbridge import constants as C  # noqa: E402
from espbridge.compat.gpiozero import EspBridgeFactory  # noqa: E402


@pytest.fixture()
def factory(bridge):
    f = EspBridgeFactory(bridge)
    yield f
    f.close()


def test_led_on_off_toggle(factory, fw):
    led = LED(2, pin_factory=factory)
    led.on()
    assert fw.gpio_modes[2] == 1 and fw.gpio_levels[2] == 1
    led.off()
    assert fw.gpio_levels[2] == 0
    led.toggle()
    assert fw.gpio_levels[2] == 1
    led.close()


def test_button_when_pressed_via_edge_event(factory, fw):
    fw.gpio_levels[4] = 1  # pulled up, not pressed
    btn = Button(4, pin_factory=factory)
    assert fw.gpio_modes[4] == 2          # input_pullup
    assert 4 in fw.watching               # edge watch armed
    assert not btn.is_pressed

    pressed = threading.Event()
    btn.when_pressed = lambda: pressed.set()

    fw.gpio_levels[4] = 0                 # firmware reports the new level too
    fw.emit(C.GPIO_EDGE_EVT, bytes([4, 0]) + struct.pack(">I", 1234))
    assert pressed.wait(2.0)
    assert btn.is_pressed
    btn.close()
    assert 4 not in fw.watching


def test_pwmled_duty(factory, fw):
    led = PWMLED(5, pin_factory=factory)
    led.value = 0.5
    assert 5 in fw.pwm_attached
    assert fw.pwm_duty[5] == round(0.5 * 4095)
    led.value = 1.0
    assert fw.pwm_duty[5] == 4095
    led.off()
    assert fw.pwm_duty[5] == 0
    led.close()


def test_pin_specs_and_identity(factory):
    p1 = factory.pin(2)
    p2 = factory.pin("GPIO2")
    p3 = factory.pin("2")
    assert p1 is p2 is p3

"""Back-compat shim — transports now live in espbridge.transports.*"""
from .transports.serial import PortInfo, SerialTransport, autodetect_port, find_ports
from .transports.mock import MockTransport

__all__ = ["PortInfo", "SerialTransport", "MockTransport", "autodetect_port", "find_ports"]

#!/usr/bin/env python3
"""OPC-UA bridge exposing the legacy GIZMo slow-control surface."""

from __future__ import annotations

import os
import socket
import threading
import time

import numpy as np
import zmq
from opcua import Server, ua

from gizmo_common import atomic_write, read_exported_int, state_path


ENDPOINT = os.environ.get("GIZMO_OPCUA_ENDPOINT", "opc.tcp://0.0.0.0:4840")
ZMQ_ENDPOINT = os.environ.get("GIZMO_ZMQ_CLIENT_ENDPOINT", "tcp://127.0.0.1:5555")
TEMPERATURE_HOST = os.environ.get("GIZMO_TEMPERATURE_CLIENT_HOST", "127.0.0.1")
TEMPERATURE_PORT = int(os.environ.get("GIZMO_TEMPERATURE_PORT", "5005"))
SDR_HOST = os.environ.get("GIZMO_SDR_CLIENT_HOST", "127.0.0.1")
SDR_PORT = int(os.environ.get("GIZMO_SDR_PORT", "5556"))
SDR_SAMPLE_COUNT = int(os.environ.get("GIZMO_SDR_SAMPLE_COUNT", "2048"))
REQUEST_TIMEOUT_MS = int(os.environ.get("GIZMO_BRIDGE_TIMEOUT_MS", "3000"))


class GizmoOpcUaServer:
    def __init__(self) -> None:
        self._context = zmq.Context.instance()
        self._request_lock = threading.Lock()
        self.server = Server()
        self.server.set_endpoint(ENDPOINT)
        self.namespace = self.server.register_namespace("SimpleOPCUAServer")
        command_object = self.server.get_objects_node().add_object(
            self.namespace, "CommandObject"
        )

        command_object.add_method(
            self.namespace,
            "send_command",
            self.send_command,
            [ua.VariantType.String],
            [ua.VariantType.String],
        )

        threshold = read_exported_int("setThreshold.env", "threshold", 100)
        run_interval = read_exported_int("setRunInterval.env", "runInterval", 100)

        self.set_threshold = command_object.add_variable(self.namespace, "set_th", threshold)
        self.set_threshold.set_writable()
        self.data = command_object.add_variable(self.namespace, "data", "")
        self.set_time = command_object.add_variable(self.namespace, "set_time", "")
        self.set_time.set_writable()
        self.clear_latch = command_object.add_variable(self.namespace, "clear_latch", "")
        self.clear_latch.set_writable()
        self.measurements = command_object.add_variable(
            self.namespace, "measurements_per_calc", run_interval
        )
        self.measurements.set_writable()
        self.calibrate = command_object.add_variable(self.namespace, "calibrate", 0)
        self.calibrate.set_writable()
        self.read_adc = command_object.add_variable(self.namespace, "ReadADC", 0)
        self.read_adc.set_writable()
        self.csv_data = command_object.add_variable(self.namespace, "csvData", "")
        self.resistance_calibration = command_object.add_variable(
            self.namespace, "RCalData", ""
        )
        self.capacitance_calibration = command_object.add_variable(
            self.namespace, "CCalData", ""
        )
        self.thermals = command_object.add_variable(self.namespace, "thermals", "")
        self.sdr = command_object.add_variable(
            self.namespace, "SDR", ua.Variant([], ua.VariantType.Int32)
        )
        self.normalize = command_object.add_variable(self.namespace, "normalize", 0)
        self.normalize.set_writable()

        self._last_threshold = threshold
        self._last_run_interval = run_interval
        self._last_time = self.set_time.get_value()

    def request(self, command: str) -> str:
        """Use a request-local ZMQ socket so OPC callback threads never share one."""
        with self._request_lock:
            client = self._context.socket(zmq.REQ)
            client.setsockopt(zmq.LINGER, 0)
            client.setsockopt(zmq.SNDTIMEO, REQUEST_TIMEOUT_MS)
            client.setsockopt(zmq.RCVTIMEO, REQUEST_TIMEOUT_MS)
            client.connect(ZMQ_ENDPOINT)
            try:
                client.send_string(command)
                return client.recv_string()
            finally:
                client.close()

    def send_command(self, parent: object, command: object) -> list[ua.Variant]:
        del parent
        value = getattr(command, "Value", command)
        reply = self.request(str(value))
        return [ua.Variant(reply, ua.VariantType.String)]

    @staticmethod
    def csv_as_text(name: str) -> str:
        try:
            lines = state_path(name).read_text(encoding="utf-8").splitlines()
            return ", ".join(line.strip() for line in lines)
        except OSError as error:
            return f"Unable to read {name}: {error}"

    @staticmethod
    def read_temperature() -> str:
        with socket.create_connection((TEMPERATURE_HOST, TEMPERATURE_PORT), timeout=3) as client:
            return client.recv(1024).decode("utf-8", errors="replace").strip()

    @staticmethod
    def read_sdr() -> list[int]:
        frame_size = SDR_SAMPLE_COUNT * 4
        raw = bytearray(frame_size)
        view = memoryview(raw)
        received = 0
        with socket.create_connection((SDR_HOST, SDR_PORT), timeout=3) as client:
            while received < frame_size:
                count = client.recv_into(view[received:], frame_size - received)
                if count == 0:
                    raise ConnectionError("SDR service closed before a complete frame")
                received += count
        return np.frombuffer(raw, dtype=np.int32).tolist()

    def poll_readbacks(self) -> None:
        self.data.set_value(self.request("get_data"))
        self.thermals.set_value(self.read_temperature())
        self.resistance_calibration.set_value(self.csv_as_text("Rcalibration_ph.csv"))
        self.capacitance_calibration.set_value(self.csv_as_text("Ccalibration_ph.csv"))
        self.sdr.set_value(ua.Variant(self.read_sdr(), ua.VariantType.Int32))

    def forward_writes(self) -> None:
        threshold = self.set_threshold.get_value()
        if threshold != self._last_threshold:
            print(self.request(f"set_th {threshold}"), flush=True)
            self._last_threshold = threshold

        requested_time = self.set_time.get_value()
        if requested_time != self._last_time:
            print(self.request(f"set_time {requested_time}"), flush=True)
            self._last_time = requested_time

        if self.clear_latch.get_value() == "clear_latch":
            print(self.request("clear_latch"), flush=True)
            self.clear_latch.set_value("")

        run_interval = self.measurements.get_value()
        if run_interval != self._last_run_interval:
            print(self.request(f"run {run_interval}"), flush=True)
            self._last_run_interval = run_interval

        if self.calibrate.get_value() == 1:
            self.calibrate.set_value(0)
            print(self.request(f"CAL {run_interval}"), flush=True)

        if self.read_adc.get_value() == 1:
            self.read_adc.set_value(0)
            self.csv_data.set_value("")
            print(self.request("read_adc"), flush=True)
            time.sleep(5)
            self.csv_data.set_value(self.csv_as_text("adc.csv"))

        if self.normalize.get_value() == 1:
            atomic_write(state_path("normalizeMagFlag.env"), "normalizeMagFlag=1\n")
            self.normalize.set_value(0)

    def run(self) -> None:
        self.server.start()
        print(f"OPC-UA server listening at {ENDPOINT}", flush=True)
        last_poll = 0.0
        try:
            while True:
                now = time.monotonic()
                if now - last_poll >= 1.0:
                    try:
                        self.poll_readbacks()
                    except (OSError, ConnectionError, zmq.ZMQError) as error:
                        print(f"Readback poll failed: {error}", flush=True)
                    last_poll = now

                try:
                    self.forward_writes()
                except (OSError, RuntimeError, ValueError, zmq.ZMQError) as error:
                    print(f"OPC write forwarding failed: {error}", flush=True)
                time.sleep(0.05)
        finally:
            self.server.stop()


def main() -> None:
    GizmoOpcUaServer().run()


if __name__ == "__main__":
    main()

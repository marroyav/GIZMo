#!/usr/bin/env python3
import time
import zmq
import socket
import numpy as np
from opcua import ua, Server

# Setup ZMQ REQ Socket
context = zmq.Context()
zmqSocket = context.socket(zmq.REQ)
zmqSocket.connect("tcp://localhost:5555")  # Update IP if needed

def read_threshold_from_file(filename):
    """
    Reads a file, strips the prefix, and returns the value as an int.
    """
    with open(filename, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export threshold="):
                # remove the prefix and return only the value
                return int(line.replace("export threshold=", "", 1))
    return None  # return None if not found

def read_run_interval_from_file(filename):
    """
    Reads a file, strips the prefix, and returns the value as an int.
    """
    with open(filename, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export runInterval="):
                # remove the prefix and return only the value
                return int(line.replace("export runInterval=", "", 1))
    return None  # return None if not found

class SimpleOPCUAServer:
    def __init__(self, endpoint="opc.tcp://0.0.0.0:4840"):
        self.server = Server()
        self.server.set_endpoint(endpoint)
        self.idx = self.server.register_namespace("SimpleOPCUAServer")

        self.HOST = '0.0.0.0'
        self.PORT = 5005
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.connect((self.HOST, self.PORT))
        print(f"Connected to {self.HOST}:{self.PORT}")

        self.objects = self.server.get_objects_node()
        self.command_obj = self.objects.add_object(self.idx, "CommandObject")

        # Add method exposed to clients
        self.command_obj.add_method(self.idx, "send_command", self.send_command,
                                    [ua.VariantType.String], [])  # input: string, output: none
        
        # Set Threshold variable
        currentThreshold = read_threshold_from_file("/home/ubuntu/Software/setThreshold.env")
        self.set_th_var = self.command_obj.add_variable(self.idx, "set_th", currentThreshold)    #set_th
        self.set_th_var.set_writable()
        self._last_set_th = self.set_th_var.get_value()

        # Get Data variable
        self.get_data = self.command_obj.add_variable(self.idx, "data", "")        #data from ZMQ server
        self.get_data.set_writable(False) #Read Only

        # Set time variable
        self.set_time = self.command_obj.add_variable(self.idx, "set_time", "")     #set this time to the installation date/time
        self.set_time.set_writable(True)

        # clear latch variable
        self.clear_latch = self.command_obj.add_variable(self.idx, "clear_latch", "")
        self.clear_latch.set_writable(True)

        # Set measurements per calculation
        currentRunInterval = read_run_interval_from_file("/home/ubuntu/Software/setRunInterval.env")
        print(f"currentRunInterval = {currentRunInterval}")
        self.set_measurements_per_calc = self.command_obj.add_variable(self.idx, "measurements_per_calc", currentRunInterval)
        self.set_measurements_per_calc.set_writable(True)
        self._last_set_runInterval = self.set_measurements_per_calc.get_value()

        # Calibrate system Boolean logic. 1 for Cal, 0 for nothing
        self.calibrate = self.command_obj.add_variable(self.idx, "calibrate", 0) #Setting default to 0
        self.calibrate.set_writable(True)

        # Read ADC Boolean logic. 1 for Read, 0 for nothing
        self.readADC = self.command_obj.add_variable(self.idx, "ReadADC", 0) # Setting default to 0
        self.readADC.set_writable(True)
        # ADC Data
        self.csv_data = self.command_obj.add_variable(self.idx, "csvData", "")
        self.csv_data.set_writable(False)

        # Read RCal and CCal csv files
        self.rCalData = self.command_obj.add_variable(self.idx, "RCalData", "")
        self.rCalData.set_writable(False)
        self.cCalData = self.command_obj.add_variable(self.idx, "CCalData", "")
        self.cCalData.set_writable(False)

        # Read Thermals
        self.thermalsData = self.command_obj.add_variable(self.idx, "thermals", "")
        self.thermalsData.set_writable(False)

        # Read SDR data
        self.SDR_list = self.command_obj.add_variable(self.idx, "SDR", ua.Variant([], ua.VariantType.Int32))
        self.SDR_list.set_writable(False)

        # Set Normalize Flag
        self.normalizeFlag = self.command_obj.add_variable(self.idx, "normalize", 0)
        self.normalizeFlag.set_writable(True)

        print(f"OPC-UA server listening at {endpoint}")

    def send_command(self, parent, command):
    # Try to extract the actual Python value if Variant is passed
        try:
            cmd_val = command.Value
        except AttributeError:
            cmd_val = command
        print(f"Received command: {cmd_val}")
        zmqSocket.send_string(cmd_val)
        reply = zmqSocket.recv_string()
        #return []
        return [ua.Variant(reply, ua.VariantType.String)]

    def read_ADC_CSV_Data_from_file(self, filename):
        """
        Reads CSV data from adc.csv and loads csv_data variable with a single string.
        """
        with open(filename, "r") as f:
            lines = f.readlines()
            csv_content = ", ".join(line.strip() for line in lines)
            self.csv_data.set_value(csv_content)

    def read_RCal_Data_from_file(self, filename):
        """
        Reads CSV data from Rcalibration_ph.csv and loads getRCal variable with a single string.
        """
        with open(filename, "r") as f:
            lines = f.readlines()
            RCal_content = ", ".join(line.strip() for line in lines)
            self.rCalData.set_value(RCal_content)
    
    def read_CCal_Data_from_file(self, filename):
        """
        Reads CSV data from Ccalibration_ph.csv and loads getCCal variable with a single string.
        """
        with open(filename, "r") as f:
            lines = f.readlines()
            CCal_content = ", ".join(line.strip() for line in lines)
            self.cCalData.set_value(CCal_content)

    def SDRconnect(self):
        SDR_HOST = "0.0.0.0"
        SDR_PORT = 5556
        SDR_NUM_SAMPLES = 2048
        SDR_BYTES_PER_FRAME = SDR_NUM_SAMPLES * 4  # 4 bytes per int32
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((SDR_HOST, SDR_PORT))

        raw = bytearray(SDR_BYTES_PER_FRAME)
        view = memoryview(raw)
        read_bytes = 0

        # Read exactly one frame
        while read_bytes < SDR_BYTES_PER_FRAME:
            n = s.recv_into(view[read_bytes:], SDR_BYTES_PER_FRAME - read_bytes)
            if n == 0:
                raise ConnectionError("Server closed the connection unexpectedly")
            read_bytes += n

        # Convert to NumPy array
        SDR_data = np.frombuffer(raw, dtype=np.int32)
        #print("Received 2048 samples:")
        #print(SDR_data)
        data_list = SDR_data.tolist()  # convert NumPy array to Python list
        self.SDR_list.set_value(ua.Variant(data_list, ua.VariantType.Int32))

        s.close()

    def set_normalize_mag_flag(self):
        file_path = "/home/ubuntu/Software/normalizeMagFlag.env"
        with open(file_path, "w") as f:
            f.write("normalizeMagFlag=1\n")  # write the string and add newline

    def start(self):
        self.server.start()
        print("Server started")

        last_set_th = self._last_set_th
        last_set_runInterval = self._last_set_runInterval
        last_set_time = None
        last_poll = time.monotonic()
        poll_interval = 1.0

        temperature_data = "" # Stores the latest temperature from the server

        try:
            while True:
                now = time.monotonic()

                # Poll ZMQ server for CSV data once per second
                if now - last_poll >= poll_interval:
                    zmqSocket.send_string("get_data")
                    data_string = zmqSocket.recv_string()
                    self.get_data.set_value(data_string)
                    data = self.s.recv(1024)
                    temperature_data = data.decode('utf-8').strip()
                    self.thermalsData.set_value(temperature_data)
                    last_poll = now
                    #print("Polled get_data from ZMQ")
                    # Also update the Rcal and Ccal datasets
                    self.read_RCal_Data_from_file("/home/ubuntu/Software/Rcalibration_ph.csv")
                    self.read_CCal_Data_from_file("/home/ubuntu/Software/Ccalibration_ph.csv")
                    # Update the SDR data
                    self.SDRconnect()

                # Forward set_th if client updated it
                current_th = self.set_th_var.get_value()
                if current_th != last_set_th:
                    cmd_str = f"set_th {current_th}"
                    print(f"Forwarding command to ZMQ: {cmd_str}")
                    zmqSocket.send_string(cmd_str)
                    reply = zmqSocket.recv_string()
                    print(f"ZMQ replied: {reply}")
                    last_set_th = current_th

                # Forward set_time if client updated it
                current_time_val = self.set_time.get_value()
                if current_time_val != last_set_time:
                    cmd_str = f"set_time {current_time_val}"
                    print(f"Forwarding command to ZMQ: {cmd_str}")
                    zmqSocket.send_string(cmd_str)
                    reply = zmqSocket.recv_string()
                    print(f"ZMQ replied: {reply}")
                    last_set_time = current_time_val
                
                current_latch = self.clear_latch.get_value()
                if current_latch == "clear_latch":
                    cmd_str = f"clear_latch"
                    print(f"Forwarding command to ZMQ: {cmd_str}")
                    zmqSocket.send_string(cmd_str)
                    reply = zmqSocket.recv_string()
                    print(f"ZMQ replied: {reply}")
                    current_latch == ""
                    self.clear_latch.set_value("")

                # Forward runInterval if client updated it
                current_runInterval = self.set_measurements_per_calc.get_value()
                if current_runInterval != last_set_runInterval:
                    cmd_str = f"run {current_runInterval}"
                    print(f"Forwarding command to ZMQ: {cmd_str}")
                    zmqSocket.send_string(cmd_str)
                    reply = zmqSocket.recv_string()
                    print(f"ZMQ replied: {reply}")
                    last_set_runInterval = current_runInterval

                # Forward calibrate command if client updated it
                current_calibrate = self.calibrate.get_value()
                if current_calibrate == 1:
                    self.calibrate.set_value(0)
                    cmd_str = f"CAL {current_runInterval}"
                    print(f"Forwarding command to ZMQ: {cmd_str}")
                    zmqSocket.send_string(cmd_str)
                    reply = zmqSocket.recv_string()
                    print(f"ZMQ replied: {reply}")

                # Forward read_adc command if client updated it
                current_readADC = self.readADC.get_value()                    
                if current_readADC == 1:
                    current_readADC = self.readADC.set_value(0) #Reset Bool to 0
                    self.csv_data.set_value("") #clear the existing data in the csv_data
                    cmd_str = f"read_adc"
                    print(f"Forwarding command to ZMQ: {cmd_str}")
                    zmqSocket.send_string(cmd_str)
                    reply = zmqSocket.recv_string()
                    print(f"ZMQ replied: {reply}")
                    time.sleep(5)
                    self.read_ADC_CSV_Data_from_file("/home/ubuntu/Software/adc.csv")
                    time.sleep(.1)
                    #self.csv_data.set_value("")

                currentMagFlag = self.normalizeFlag.get_value()
                if currentMagFlag == 1:
                    self.set_normalize_mag_flag()
                    currentMagFlag = 0
                    self.normalizeFlag.set_value(0)
                
                    

                # Small sleep to avoid busy loop
                time.sleep(0.05)
        finally:
            print("Stopping server")
            self.server.stop()

if __name__ == "__main__":
    server = SimpleOPCUAServer()
    server.start()

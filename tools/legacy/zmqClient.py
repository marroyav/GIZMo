import zmq
import re
import datetime
import time
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy.interpolate import PchipInterpolator
from scipy.optimize import curve_fit
import csv

# Allowed commands (prefixes)
valid_commands = {"set_time", "get_data", "get_data_continuous", "testHello.py", "run", "CAL", "set_th", "read_adc", "get_adc", "plot_adc", "get_cal", "plot_Rcal", "interpolate", "fit_curve", "clear_latch"}

# Setup ZMQ REQ socket
context = zmq.Context()
socket = context.socket(zmq.REQ)
socket.connect("tcp://192.168.0.150:5555")  # Update IP if needed

while True:
    plt.close('all')

    # Get user input
    print("Enter a command:\n")
    print("  set_time             --> Set the time manually or from the local clock")
    print("  clear_latch          --> Clear the latched state of the Impedance Monitor")
    print("  get_data             --> Read Impedance Monitor Socket Data")
    print("  get_data_continuous  --> Conintuously Read Impedance Monitor Socket Data")
    print("  run N                --> Run with N seconds between each measurement")
    print("  CAL N                --> Calibrate the system with N/10 seconds between each measurement")
    print("  get_Rcal             --> Get the Rcalibration.csv file from the Impedance Monitor")
    print("  get_Ccal             --> Get the Ccalibration.csv file from the Impedance Monitor")
    print("  plot_Rcal            --> Plot the Rcalibration.csv file after retrieving from the the Impedance Monitor")
    print("  fit_curve            --> Fit an exponential curve to the calibration data")
    print("  interpolate          --> Conduct a reverse interpolation using the measured magnitude")
    print("  set_th N             --> Set the impedance (N) threshold for the system alarm")
    print("  read_adc             --> Read the ADC in the Impedance Monitor")
    print("  get_adc              --> Get the adc.csv file after reading the ADC")
    print("  plot_adc             --> Plot the adc.csv file after getting it from the Impedance Monitor")
    print("  testHello.py         --> Run the testHello.py script on the Impedance Monitor")
    print("  exit\n")
    msg = input(" > ").strip()

    # Exit condition
    if msg == "exit":
        print("Exiting client.")
        break
    
    # Special hangling for set_time command
    if msg == ("set_time"):
        answer = input("set time Manually or from Local clock?\n M for Manually setting the time \n L for use Local Clock\n > ").strip()
        if answer == "L" or answer == "l":
            now = datetime.datetime.now()
            print(f"Sending set_time {now}")
            # Convert 'now' to string before concatenation
            msgNow = msg + " " + now.strftime("%Y-%m-%d %H:%M:%S")  # Formatting 'now' to string
            socket.send_string(msgNow)
            reply = socket.recv_string()
            print(f"Received:\n{reply}\n")
            continue
        if answer == "M" or answer == "m":
            print("Enter a Date - Time - String in the following format\n")
            answerM = input("YYYY-MM-DD HH:MM:SS\n").strip()
            print(f"Sending set_time {answerM}")
            msgNow = msg + " " + answerM
            socket.send_string(msgNow)
            reply = socket.recv_string()
            print(f"Received:\n{reply}\n")
            continue
    
    # Special handling for run N (e.g., run 5) runs with a specified number of seconds between measurements
    if msg.startswith("run "):
        parts = msg.split()
        if len(parts) == 2 and parts[1].isdigit():
            # Valid run command with a number
            print(f"Sending: {msg}")
            socket.send_string(msg)
            reply = socket.recv_string()
            print(f"Received:\n{reply}\n")
            continue
        else:
            print("Invalid 'run N' format. Usage: run <number_of_seconds>")
            continue

    # Special handling for CAL N (e.g., CAL 5) calibrated with a specified number of seconds between measurements
    if msg.startswith("CAL "):
        parts = msg.split()
        if len(parts) == 2 and parts[1].isdigit():
            # Valid run command with a number
            print(f"Sending: {msg}")
            socket.send_string(msg)
            reply = socket.recv_string()
            print(f"Received:\n{reply}\n")
            continue
        else:
            print("Invalid 'CAL N' format. Usage: CAL <number_of_seconds>")
            continue

    # Special handling for set_th (e.g., set_th 50) set's the alarm threshold to a specified impedance   
    if msg.startswith("set_th "):
        parts = msg.split()
        if len(parts) == 2 and parts[1].isdigit():
            # Valid run command with a number
            print(f"Sending: {msg}")
            socket.send_string(msg)
            reply = socket.recv_string()
            print(f"Received:\n{reply}\n")
            continue
        else:
            print("Invalid 'set_th N' format. Usage: set_th <N Ohms>")
            continue

    # Handle continuous get_data - gets data from server once per second
    if msg == "get_data_continuous":
        try:
            while True:
                print(f"Sending: {msg}")
                socket.send_string(msg)
                reply = socket.recv_string()
                print(f"Received:\n{reply}\n")
        except KeyboardInterrupt:
            print("\nStopped continuous data stream.")
            continue

    # Handle get_adc for an incoming csv file
    if msg == "get_adc":
        print(f"Sending: {msg}")
        socket.send_string(msg)
        csv_str = socket.recv_string()
        rows = csv_str.splitlines()
        data = [row.split(",") for row in rows]

        # Write to adc.csv
        with open("adc.csv", "w") as f:
            for row in data:
                f.write(",".join(row) + "\n")

        print("CSV data written to adc.csv")
        continue
    
    # Handle get_Rcal for an incoming csv file
    if msg == "get_Rcal":
        print(f"Sending: {msg}")
        socket.send_string(msg)
        csv_str = socket.recv_string()
        rows = csv_str.splitlines()
        data = [row.split(",") for row in rows]

        # Write to adc.csv
        with open("Rcalibration.csv", "w") as f:
            for row in data:
                f.write(",".join(row) + "\n")

        print("CSV data written to cal.csv")
        continue
    
        # Handle get_Ccal for an incoming csv file
    if msg == "get_Ccal":
        print(f"Sending: {msg}")
        socket.send_string(msg)
        csv_str = socket.recv_string()
        rows = csv_str.splitlines()
        data = [row.split(",") for row in rows]

        # Write to adc.csv
        with open("Ccalibration.csv", "w") as f:
            for row in data:
                f.write(",".join(row) + "\n")

        print("CSV data written to cal.csv")
        continue

    # Handle Plotting the adc.csv with an FFT
    if msg == "plot_adc":
        print("Plotting ADC signal and FFT data")
        
        filename = 'adc.csv'
        df = pd.read_csv(filename, header=None)
        adc_signal = df.iloc[:, 0].values  # ADC with DC offset
        in_phase_ref = df.iloc[:, 1].values  # In-phase sine
        quad_ref = df.iloc[:, 2].values      # Quadrature cosine
        
        # --- Plot ADC Signal ---
        plt.figure(figsize=(10, 4))
        plt.plot(adc_signal, label='ADC Signal with DC Offset\n')
        plt.title(filename)
        plt.xlabel('Sample BRAM Register Number')
        plt.ylabel('ADC Value')
        plt.grid(True)
        plt.legend()
        plt.tight_layout()
        plt.savefig('plot.reference_wave_plot.png')
        #plt.close()

        # --- Compute FFT of ADC Signal ---
        N = len(adc_signal)
        sampling_rate = 734566.4  # Hz — measured signal frequency is 1.4327 kHz
        fft_result = np.fft.fft(adc_signal)
        magnitude = np.abs(fft_result)
        freqs = np.fft.fftfreq(N, d=1/sampling_rate)

        # Keep only positive frequencies
        half_N = N // 2
        freqs = freqs[:half_N]
        magnitude = magnitude[:half_N]

        # Optionally remove low frequencies from FFT plot (e.g., below 5 Hz)
        min_freq = 1500  # Hz (Signal Frequency is 1.4327kHz so there's no useful data below this point
        mask = freqs > min_freq

        plt.figure(figsize=(10, 4))
        plt.plot(freqs[mask], magnitude[mask], label='FFT Magnitude')
        plt.title('FFT of ADC Signal (0–1499 Hz Omitted, DC Included)')
        plt.xlabel('Frequency (Hz)')
        plt.ylabel('Magnitude')
        plt.grid(True)
        plt.legend()
        plt.tight_layout()
        plt.savefig('plot.fft_plot.png')
        

        # --- Step 4: Plot Reference Sine and Cosine Waves ---
        plt.figure(figsize=(10, 4))
        plt.plot(in_phase_ref, label='In-Phase Reference (Sine)')
        plt.plot(quad_ref, label='Quadrature Reference (Cosine)')
        plt.title('Reference Sine and Cosine Waves')
        plt.xlabel('Sample')
        plt.ylabel('Amplitude')
        plt.grid(True)
        plt.legend()
        plt.tight_layout()
        plt.savefig('plot.reference_wave_plot.png')

        # Plot I vs Q
        plt.figure(figsize=(6,6))
        plt.plot(in_phase_ref, quad_ref)
        plt.xlabel('Quadrature')
        plt.ylabel('In-Phase')
        plt.title('I vs Q Plot (Circle)')
        plt.axis('equal')
        plt.grid(True)
        plt.savefig('plot.reference_wave_plot.pngi_vs_q_circle.png')

        # Plot Signal vs References
        plt.figure(figsize=(10, 4))
        plt.plot(adc_signal, in_phase_ref)
        plt.plot(adc_signal, quad_ref)
        plt.xlabel('ADC_Signal')
        plt.ylabel('Reference_Signals')
        plt.grid(True)
        plt.savefig('plot.ADC_vs_InPhase_circle.png')

        # --- Step 5: Correlation to Compute I/Q Amplitudes ---
        # Normalize references if needed
        in_phase_ref = in_phase_ref / np.linalg.norm(in_phase_ref)
        quad_ref = quad_ref / np.linalg.norm(quad_ref)

        I = np.dot(adc_signal, in_phase_ref)
        Q = np.dot(adc_signal, quad_ref)
        amplitude = np.sqrt(I**2 + Q**2)
        phase_rad = np.arctan2(Q, I)
        phase_deg = np.degrees(phase_rad)

        print(f"I: {I:.2f}\n, Q: {Q:.2f}, Amplitude: {amplitude:.2f}, Phase: {phase_deg:.2f}°\n")
        
        # --- Step 6: Phasor Plot (Vector Diagram) ---
        plt.figure(figsize=(5, 5))
        plt.quiver(0, 0, I, Q, angles='xy', scale_units='xy', scale=1, color='blue')
        plt.xlim(-1.1 * amplitude, 1.1 * amplitude)
        plt.ylim(-1.1 * amplitude, 1.1 * amplitude)
        plt.axhline(0, color='gray', linestyle='--')
        plt.axvline(0, color='gray', linestyle='--')
        plt.grid(True)
        plt.gca().set_aspect('equal', adjustable='box')
        plt.title(f'Phasor Diagram\nAmplitude = {amplitude:.2f}, Phase = {phase_deg:.2f}°')
        plt.xlabel('In-Phase (I)')
        plt.ylabel('Quadrature (Q)')      
        plt.savefig('plot.phasor_diagram.png')
        
        print("Plots saved to current directory")
        print("Close opened plots to continue:\n")

        plt.show()

        continue

    if msg == "plot_Rcal":
        print("Plotting Calibration Interpolation\n")

        x, y = [], []
        with open('Rcalibration.csv', 'r') as file:
            reader = csv.reader(file)
            for row in reader:
                if len(row) >= 2:
                    try:
                        x.append(float(row[0]))
                        y.append(float(row[1]))
                    except ValueError:
                        continue  # Skip rows with invalid data

        x_data = np.array(x)
        y_data = np.array(y)

        # Create dense interpolator y = f(x)
        interp_y_from_x = PchipInterpolator(x_data, y_data, extrapolate=True)
        # Generate new x values for interpolation for smooth plotting
        x_new = np.linspace(min(x_data), max(x_data), 1000)
        y_cubic = interp_y_from_x(x_new)

        # Invert the interpolation to get x = f⁻¹(y)
        x_dense = np.linspace(min(x_data), max(x_data), 11000)
        y_dense = interp_y_from_x(x_dense)

        # Sort the data to make y strictly increasing for inversion
        sorted_indices = np.argsort(y_dense)
        y_sorted = y_dense[sorted_indices]
        x_sorted = x_dense[sorted_indices]


        ### Plotting
        plt.figure(figsize=(10, 6))
        plt.plot(x_data, y_data, 'o', label='Original Data')
        plt.plot(x_sorted, y_sorted, '--', label='Cubic Spline Interpolation')
        plt.xlabel('X')
        plt.ylabel('Y')
        plt.title('Numerical Interpolation of Dataset')
        plt.legend()
        plt.grid(True)
        plt.tight_layout()
        plt.savefig('plot.interpolated-calibration.png')
        print("Plots saved to current directory")
        print("Close opened plots to continue:\n")
        
        plt.show()

        continue

    if msg == "plot_Ccal":
        print("Plotting Calibration Interpolation\n")

        filename = 'Ccalibration.csv'
        df = pd.read_csv(filename, header=None)
        capacitance = df.iloc[:, 0].values
        magnitude = df.iloc[:, 1].values
        
        # --- Plot Capacitor Curve ---
        plt.figure(figsize=(10, 4))
        plt.plot(capacitance, magnitude, label='Mag')
        plt.title(filename)
        plt.xlabel('Capacitance (uF)')
        plt.ylabel('Magnitude')
        plt.grid(True)
        plt.legend()
        plt.tight_layout()
        plt.savefig('plot.Ccalibration.png')
        #plt.close()
        
        plt.show()

        continue 

    if msg == "interpolate":
        # Read x and y data from a CSV file without a header
        print("Plotting Calibration Interpolation\n")

        x, y = [], []
        with open('Rcalibration.csv', 'r') as file:
            reader = csv.reader(file)
            for row in reader:
                if len(row) >= 2:
                    try:
                        x.append(float(row[0]))
                        y.append(float(row[1]))
                    except ValueError:
                        continue  # Skip rows with invalid data

        x_data = np.array(x)
        y_data = np.array(y)

        # Create monotonic cubic spline interpolator y = f(x)
        interp_y_from_x = PchipInterpolator(x_data, y_data, extrapolate=True)

        # Generate new x values for interpolation for smooth plotting
        x_new = np.linspace(min(x_data), max(x_data), 1000)
        y_cubic = interp_y_from_x(x_new)

        # Invert the interpolation to get x = f⁻¹(y)
        x_dense = np.linspace(min(x_data), max(x_data), 11000)
        y_dense = interp_y_from_x(x_dense)

        # Sort the data to make y strictly increasing for inversion
        sorted_indices = np.argsort(y_dense)
        y_sorted = y_dense[sorted_indices]
        x_sorted = x_dense[sorted_indices]

        # Create inverse interpolator x = f⁻¹(y)
        interp_x_from_y = PchipInterpolator(y_sorted, x_sorted, extrapolate=True)

        # Prompt user for y input
        try:
            y_input = float(input(f"Enter a y-value between {min(y_data):.2f} and {max(y_data):.2f}: "))
            x_output = interp_x_from_y(y_input)
            if np.isnan(x_output):
                print(f"No valid x found for y = {y_input}\n")
            else:
                print(f"\nInterpolated x for y = {y_input:.2f} is x = {x_output:.4f}\n")
                time.sleep(1.5)
        except ValueError:
            print("Invalid input. Please enter a numeric y-value.\n")

        continue

    if msg == "fit_curve":
        # Define the exponential decay model
        def exp_decay(x, A, B, C):
            return A * np.exp(-B * x) + C
    
        x, y = [], []
        with open('Rcalibration.csv', 'r') as file:
            reader = csv.reader(file)
            for row in reader:
                if len(row) >= 2:
                    try:
                        x.append(float(row[0]))
                        y.append(float(row[1]))
                    except ValueError:
                        continue  # Skip rows with invalid data

        x_data = np.array(x)
        y_data = np.array(y)
        # Initial guess for A, B, and C
        initial_guesses = [-10000, 0.01, 147000]

        # Curve fitting
        popt, pcov = curve_fit(exp_decay, x_data, y_data, p0=initial_guesses)
        A, B, C = popt

        print(f"Fitted Parameters:\nA = {A:.2f}\nB = {B:.5f}\nC = {C:.2f}")

        # Generate smooth x values for plotting the fitted curve
        x_fit = np.linspace(min(x_data), max(x_data), 500)
        y_fit = exp_decay(x_fit, *popt)

        # Plot original data and fitted curve
        plt.figure(figsize=(8, 5))
        plt.scatter(x_data, y_data, color='red', label='Data')
        plt.plot(x_fit, y_fit, color='blue', label=f'Fit: {A:.1f} * exp(-{B:.5f} * x) + {C:.1f}')
        plt.xlabel('x')
        plt.ylabel('y')
        plt.title('Exponential Decay Fit from CSV Data')
        plt.legend()
        plt.grid(True)
        plt.tight_layout()
        plt.savefig('plot.cure-fit-calibration.png')
        print("Plots saved to current directory")
        print("Close opened plots to continue:\n")
        plt.show()
        continue

    # Validate against fixed valid commands
    if msg not in valid_commands:
        print(f"Invalid command: '{msg}'")
        continue

    # Handle all other valid commands
    print(f"Sending: {msg}")
    socket.send_string(msg)
    reply = socket.recv_string()
    print(f"Received:\n{reply}\n")

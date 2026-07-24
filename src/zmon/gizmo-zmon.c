#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <fcntl.h>
#include <math.h>
#include <sys/types.h>
#include <linux/types.h> //__u8, __u16      ...
#include <string.h>      //strlen()         ...
#include <sys/mman.h>    //mmap(), munmap() ...
#include <unistd.h>      //gets()           ...
#include <time.h>
#include <ctype.h>
#include <arpa/inet.h> //used for integrating sockets into the code
#include <errno.h> //error handlingm
#include <stdbool.h>


#define PORT 5055 // port used for setting up sockets with inter-process-communication

#define BASE_GPIO_0 0xA0000000 // DAC-GPIO
#define DAC_Word    0x03028FFF // Enables the DAC Firmware State Machine
#define BASE_GPIO_1 0xA0044000 // 0x00A0000000 #GPIO Base Address (This is the relays)
#define BASE_GPIO_2 0xA0060000 // GPIO Relay Controller
#define BASE_BRAM_0 0xA0040000 // BRAM_BASE_ADDRESS 0x00A0040000 (This is the DAC)
#define BASE_BRAM_1 0xA0042000 // #BRAM address from Vivado. This is the value of the ADC
#define BRAM_SIZE_BYTES 0x2000 // 1kB
#define OFFSET    0x4           // Offset
#define MemSize 0x1000

#define PI 3.14159265359

#define AMPLITUDE 11000         // Amplitude of the sine wave in DAC value terms
#define OFFSET_V 49151          // Offset for the sine wave in DAC value terms
#define NUM_SAMPLES_DAC 2048    // Number of samples in one sine wave cycle
#define frequency 4             //change this number to adjust the frequency

#define MEM_DEV "/dev/mem"      // Memory device file

#ifndef GIZMO_STATE_DIR
#define GIZMO_STATE_DIR "/var/lib/gizmo"
#endif

#define GIZMO_STATE_FILE(name) GIZMO_STATE_DIR "/" name

#define nTerp 65               // For interpolating data



// Function Prototypes
void generate_sine_wave_to_bram(volatile uint32_t *bram_base);
void parse(const char *cmd);
void SetRes(int res);
int ClearADCMem(unsigned long len);
int ReadADCMem();
void ChangeRelay(int relay_addr, int state);
void LoadTH(const char *filename);
int WriteADCtoFile(unsigned long ADC[], int digitsToWrite);
void compute_spline_coefficients();
void compute_phase_spline_coefficients();
double interpolate_y(double x_val);
double interpolate_phase(double mag_val, double phaseMeasured);
double interpolate_x(double y_val, int max_iter, double tol);
int WriteCalibrationValuesR();
int WriteCalibrationValuesC();
int WriteCalibrationValuesR_ph();
int WriteCalibrationValuesC_ph();
void write_gpio_0(unsigned long val1, unsigned long val2);
void write_gpio_1(unsigned long val1, unsigned long val2);
void write_gpio_2(unsigned long val);
void Load_auto();
void readSystemTime(char* buffer, size_t bufferSize);
void writeToLatch(int latchedValue, const char *timestamp);
void readFromLatch();
void sort_by_total_cap(double total_cap[], double magArrayC[], double phaseArrayC[], double phase2ArrayC[], int size);
void sort_array(double arr[], int size);
double findCenterOfCap(double array_x[], double array_y[]);
int finxMaxMagnitude(void);
int writeNormalizeMagFlag(int flag);
int readNormalizeMagFlag();
void LoadResistance(const char *filename);
void LoadCapacitance(const char *filename);


//---------------------
int global_th = 2000;
volatile uint32_t *_gpio_1;
volatile uint32_t *_gpio_0;
volatile uint32_t *_gpio_2;
volatile uint32_t *_bram_0;
volatile uint32_t *_bram_1;
int num_sampl_ADC = 2048;
unsigned long _ADC[2048];
unsigned long _DAC[2048];
double _Calib8_R[100];
double _Calib8_mag[100];
int _freq_mult;
float _amp = 1.0;
double _fI[16384];
double _fQ[16384];
unsigned long _length = 1023;
unsigned long _file_length = 2048;
double _phase;
// Make this a static variable so it remembers relay states
static unsigned long relay_state = 0xA5556AAA;
double a[nTerp], b[nTerp], c[nTerp], d[nTerp]; // spline coefficients
double a_phi[nTerp], b_phi[nTerp], c_phi[nTerp], d_phi[nTerp]; //phase spline coefficients
double total_res[128];
double total_cap[128];
double magArrayR[nTerp];
double magArrayC[128];
double phaseArrayR[nTerp];
double phase2ArrayR[nTerp];
double phaseArrayC[128];
double phase2ArrayC[128];
double aveIArray[128];
double aveQArray[128];
double resistor_values[7];
double capacitor_values[7];

int latched = 0;
char latchedStamp[64];

double baseResonateCap;


int main(int argc, char *argv[])
{
    LoadTH(GIZMO_STATE_FILE("setThreshold.env"));
    int memfd;
    //char str[255];
    char cmd[200];
    //unsigned int DataOld;
    //unsigned int DataNew;
    void *bram_base;

    //---------------------------------------------------------------------
    // Open /dev/mem file
    //---------------------------------------------------------------------
    memfd = open("/dev/mem", O_RDWR | O_SYNC);
    if (memfd == -1)
    {
        printf("Can't open /dev/mem\r\n");
        exit(EXIT_FAILURE);
    }

    printf("/dev/mem opened\r\n");
    printf("===============\r\n");

    //---------------------------------------------------------------------
    // Map the device into memory.
    // Map one page of memory into user space such that the device is
    // in that page, but it may not be at the start of the page.
    //---------------------------------------------------------------------

    unsigned long page_size = sysconf(_SC_PAGESIZE);
    printf("page_size=0x%.8lX\r\n", page_size);

    // map gpio_1: writing the relay word
    _gpio_1 = (volatile uint32_t *)mmap(NULL, MemSize, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, BASE_GPIO_1 & ~(MemSize - 1));

    if (_gpio_1 == MAP_FAILED)
    {
        printf("Can't map the memory=0x%.8lX to user space for GPIO_1\r\n", (long unsigned int)(BASE_GPIO_1));
        exit(EXIT_FAILURE);
    }
    else {
        printf("HW GPIO_1 =0x%.8lX mapped to user space at mapped_base=%p\r\n", (long unsigned int)(BASE_GPIO_1), (void *)_gpio_1);
    }

        // map gpio_0: DAC's GPIO
    _gpio_0 = (volatile uint32_t *)mmap(NULL, MemSize, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, BASE_GPIO_0 & ~(MemSize - 1));

    if (_gpio_0 == MAP_FAILED)
    {
        printf("Can't map the memory=0x%.8lX to user space for GPIO_0\r\n", (long unsigned int)(BASE_GPIO_0));
        exit(EXIT_FAILURE);
    }
    else {
        printf("HW GPIO_0 =0x%.8lX mapped to user space at mapped_base=%p\r\n", (long unsigned int)(BASE_GPIO_0), (void *)_gpio_0);
    }

            // map gpio_0: Relay Controllers
    _gpio_2 = (volatile uint32_t *)mmap(NULL, MemSize, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, BASE_GPIO_2 & ~(MemSize - 1));

    if (_gpio_2 == MAP_FAILED)
    {
        printf("Can't map the memory=0x%.8lX to user space for GPIO_2\r\n", (long unsigned int)(BASE_GPIO_2));
        exit(EXIT_FAILURE);
    }
    else {
        printf("HW GPIO_2 =0x%.8lX mapped to user space at mapped_base=%p\r\n", (long unsigned int)(BASE_GPIO_2), (void *)_gpio_2);
    }

    // map bram_0
    _bram_0 = (volatile uint32_t *)mmap(NULL, BRAM_SIZE_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, BASE_BRAM_0);

    if (_bram_0 == MAP_FAILED)
    {
        printf("Can't map the memory=0x%.8lX to user space\r\n",
               (long unsigned int)(BASE_BRAM_0));
        exit(EXIT_FAILURE);
    }
    printf(
        "HW bram_0 Memory=0x%.8lX mapped to user space at mapped_base=%p\r\n",
        (long unsigned int)(BASE_BRAM_0), (void *)_bram_0);


    // map bram_1
    bram_base = mmap(NULL, BRAM_SIZE_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, BASE_BRAM_1);

    if (bram_base == MAP_FAILED) {
        printf("mmap() failed\n");
        close(memfd);
        return 1;
    }

    _bram_1 = (volatile uint32_t *)bram_base;

    printf(
        "HW bram_1 Memory=0x%.8lX mapped to user space at mapped_base=%p\r\n",
        (long unsigned int)(BASE_BRAM_1), (void *)_bram_1);

    // Setting up Firmware GPIO_1 Tri-state at offset 4 and 12
    //printf("Making GPIO_1 tri all outputs\r\n");
    //*((volatile unsigned long *)(_gpio_1 + 4)) = 0x0; // 0xffffffff;
    //printf("Making GPIO_1_2 tri all inputs\r\n");
    //*((volatile unsigned long *)(_gpio_1 + 12)) = 0xffffffff;

    printf("ver Apr 11, 2025 T.DeLine \r\n");
    printf("ver Jun 2017 16.3 PMR \r\n");
    printf("implemented: \r\n");
    printf("		WLED 0x000000FF (32bit Hex U_Long-first 24 bits are zeros)\r\n");
    printf("		WLED (Last 8 bits are for LEDs\r\n");
    printf("		RSW (Something with LEDs?)\r\n");
    printf("		WREG_GPI01 addr u_byte l_byte\r\n");
    printf("		BW1 addr value(32 bit dec)\r\n");
    printf("		BR1 addr \r\n");
    printf("		BW2 addr value(32 bit dec)\r\n");
    printf("		BR2 addr \r\n");
    printf("		CLR_M1 (Clear BRAM0)\r\n");
    printf("		CLR_M2 (Clear BRAM1)\r\n");
    printf("		LOAD_F n*1.472kHz (Frequency Multiplier: n=integer)\r\n");
    printf("		SET_TH res (Alarm Threshold and writes a value to spi device: probably EVE uC)\r\n");
    printf("		AMP \r\n");
    printf("		LEN \r\n");
    printf("		READ_ADC \r\n");
    printf("		RUN number-of-seconds-to-delay-between-runs\r\n");
    printf("		RSPI \r\n");
    printf("		WSPI \r\n");
    printf("		SET_RLY num(starts from 0) 1=on, 0=off \r\n");
    printf("		CAL compute-ever-x-number-of-seconds \r\n");
    //-------------------------------------------------------

    write_gpio_0(0x4, 0x0); //sets the GPIO to Output 0xA0000000
    write_gpio_0(0x0, DAC_Word); //Enables the DAC Firmware State Machine 0xA0000000
    write_gpio_2(0x1); //take the relay controllers out of reset 0xA0060000
    write_gpio_1(0x0, 0x1); // Enable the firmware to start sending data out 0xA0044000

    //int i = 0;
    //int looping = 1;
    generate_sine_wave_to_bram(_bram_0);
    //write_gpio_1(relay_state);

    if (argc > 1)
    {
        for (int i = 1; i < argc; ++i) {
            parse(argv[i]);  // Call your parser on each command
        }
    }
    while (fgets(cmd, sizeof(cmd), stdin) != NULL)
    {
        printf(">");
        parse(cmd);
        //printf(cmd);
    }

    //---------------------------------------------------------------------
    // Closing /dev/mem file
    //---------------------------------------------------------------------
    if (close(memfd) == -1)
    {
        printf("Can't close /dev/mem\r\n");
        exit(EXIT_FAILURE);
    }
    printf("/dev/mem closed\r\n");
    return 0;
}

void write_gpio_1(unsigned long val1, unsigned long val2)
{
    *(_gpio_1 + (val1 / sizeof(uint32_t))) = val2;

}

void write_gpio_0(unsigned long val1, unsigned long val2)
{
    *(_gpio_0 + (val1 / sizeof(uint32_t))) = val2;

}

void write_gpio_2(unsigned long val)
{
    *(_gpio_2 + (0x0 / sizeof(uint32_t))) = val;

}

void read_gpio_1()
{
    unsigned long i;
    //    i = *((volatile unsigned long *) (_gpio_1 ));
    //    printf("read gpio_1=%lu (%x)\r\n", i);
    //i = *((volatile unsigned long *)(_gpio_1 + 8));
    i = *(_gpio_1 + (OFFSET / sizeof(uint32_t))) ;
    //printf("gpio read =%lu (%x)\r\n", (i & 0xffff));
    printf("gpio read = %lx\r\n", i);

    // Print the array contents (Commented out 3.7.2025)
/*     printf("Array: ");
    for (int j = 0; j < 8; j++) {
        printf("%x ", array[j]);  // Print each element in hex
    }
    printf("\r\n"); */
}

// Returns an array of the gpio register digits for comparing later
int* parse_gpio_1()
{
    static int array[8];  // Array to store each digit of 'i', static to persist after function returns
    unsigned long i;

    // Read the 32-bit value from the GPIO address (_gpio_1 + OFFSET)
    i = *(_gpio_1 + (OFFSET / sizeof(uint32_t)));

    // Store each digit of 'i' into the array
    for (int j = 0; j < 8; j++) {
        array[j] = (i >> (28 - 4 * j)) & 0xF;  // Extract each hex digit
    }

    // Return a pointer to the array
    return array;
}

void write_bram_0(unsigned long addr, unsigned long data)
{
    *((volatile unsigned long *)(_bram_0 + 4 * addr)) = data;
}

void read_bram_0(unsigned long addr)
{
    unsigned long data;
    data = *((volatile unsigned long *)(_bram_0 + 4 * addr));
    printf("read from bram0 at addr %ld = %ld (0x%lx)\r\n", addr, data, data);
}

void write_bram_1(unsigned long addr, unsigned long data)
{
    *((volatile unsigned long *)(_bram_1 + 4 * addr)) = data;
}

void read_bram_1(unsigned long addr)
{
    unsigned long data;
    data = *((volatile unsigned long *)(_bram_1 + 4 * addr));
    printf("read from bram1 at addr %ld = %ld (0x%lx)\r\n", addr, data, data);
}

/*
Flexible function which accepts any command with a single argument when called by another function.
For example, if you want to create a new command called writeHello, then you would call:
parse("writeHello").
If you need to bring arguments along with, simply add a space after the command and pass as many arguments
as you will like, all separated with spaces.
The parse function then uses sscanf to check if the first x amount of letters in the cmd starts with
specific letters. For example:
parse("writeHello 1") // --> This is called from another function.
Then from within the parse function, to check what the command is:
if (!strncmp(cmd, "writeHello", 10)) // --> This checks if the first ten letters of the command
starts with writeHello. Then, to handle the arguments, use sscanf to stuff the value into a local variable:
if (sscanf(cmd + 11, "%d", &<variable-to-fill>) > 0)
Then, the if statement surrounding the sccanf will check if sscanf completed successfully.
If it returns 0, then we do not enter the if condition and we leave the parse function.
*/
//========================================================
void parse(const char *cmd)
{
    if (cmd == NULL) {
        return;
    }
    //======================================
    if (!strncmp(cmd, "run", 3))    // checks if cmd starts with "RUN". If it does, the condition is true, and the code inside the if block will execute
    {
        unsigned long num_Reads = 0;
        if (sscanf(cmd + 4, "%lu", &num_Reads) != 1 ||
            num_Reads == 0 || num_Reads > 1000000) {
            fprintf(stderr, "run read count must be between 1 and 1000000\n");
            return;
        }

	// Setting up sockets ---------
	int server_fd, client_fd;
	struct sockaddr_in server_addr, client_addr;
	socklen_t addr_len = sizeof(client_addr);
	//char buffer[256];
    char buffer[2048];
	int opt = 1;  // Option for setsockopt

	// Seed random number generator (used for getting the sockets to work. Comment out once sockets are working)
	srand(time(NULL));

	// Create socket
	if ((server_fd = socket(AF_INET, SOCK_STREAM, 0)) < 0) {
		perror("Socket failed");
		exit(EXIT_FAILURE);
	}

	// Set socket options to reuse address
	if (setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
		perror("setsockopt failed");
		close(server_fd);
		exit(EXIT_FAILURE);
	}

	// Bind socket
	server_addr.sin_family = AF_INET;
	server_addr.sin_addr.s_addr = INADDR_ANY;
	server_addr.sin_port = htons(PORT);

	if (bind(server_fd, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
		perror("Bind failed");
		close(server_fd);
		exit(EXIT_FAILURE);
	}

    if (fcntl(server_fd, F_SETFL, O_NONBLOCK) < 0) {
        perror("Unable to make server socket nonblocking");
        close(server_fd);
        exit(EXIT_FAILURE);
    }
    // Only need to call listen() once (outside your main loop)
    if (listen(server_fd, 3) < 0) {
        perror("Listen failed");
        close(server_fd);
        exit(EXIT_FAILURE);
    }

    printf("Server listening on port %d...\n", PORT);

    //---------------------------- END SOCKET SETUP ---------------------

        //int i = 0;
        double ave = 0;
        double aveI = 0;
        double aveQ = 0;
        double maxMeasuredMag = 19000;
        int normalizeFlag = 0;
        //unsigned long value [16384];
        relay_state = 0xA5556AAA;
        write_gpio_1(OFFSET, relay_state);
        LoadResistance(GIZMO_STATE_FILE("resistance.env"));
        LoadCapacitance(GIZMO_STATE_FILE("capacitance.env"));
        Load_auto();

        for (int i = 0; i < 64; i++){
            //printf("Res = %f Mag = %f\n", total_res[i], magArrayR[i]);
        }
        compute_spline_coefficients();
        compute_phase_spline_coefficients();

        while (1 > 0)
            {
                readFromLatch();
                int i = 0;
                ave = 0;
                aveI = 0;
                aveQ = 0;
                ClearADCMem(num_sampl_ADC);

                //Read ADC memory 3 times, stack each of the values on existing element
                 for (unsigned long read_index = 0; read_index < num_Reads; read_index++){
                     ReadADCMem();
                     //usleep(50000);
                 }

		        //determine the DC offset of the ADC signal
                for (i = 0; i < num_sampl_ADC; i++)
                {
                    ave = ave + (double)(_ADC[i]);
                }

                //divide total by 3 from stacking values when reading the ADC 3 times
                ave = ave / num_sampl_ADC / num_Reads;
                //printf("ave= %f\r\n", ave);

                for (i = 0; i < num_sampl_ADC; i++)
                {
                    aveI += ((_ADC[i] / num_Reads)-ave) * _fI[i] * 0.001;
                    aveQ += ((_ADC[i] / num_Reads)-ave) * _fQ[i] * 0.001;
                }
                normalizeFlag = readNormalizeMagFlag();
                if (normalizeFlag == 1)
                {
                    maxMeasuredMag = sqrt((aveI * aveI) + (aveQ * aveQ));
                    writeNormalizeMagFlag(0);
                }

                double mag = sqrt((aveI * aveI) + (aveQ * aveQ));
                double x_reverse = interpolate_x(mag, 100, 1e-6);
                double phase_rad = atan(aveQ/aveI);
                double phase_rad2 = atan2(aveQ, aveI);
                double phase_deg = phase_rad * (180 / PI);
                double phase_deg2 = phase_rad2 * (180 / PI);
                double phx_reverse = interpolate_phase(mag, phase_deg2);
                double max_mag = finxMaxMagnitude();
                printf("mag = %f\n", mag);
                printf("Max_Mag = %1f\n", max_mag);
                double magScalingFactor = max_mag / maxMeasuredMag;
                printf("magScalingFactor = %f\n", magScalingFactor);
                double normalizedMag = mag * magScalingFactor;
                printf("normalizedMag = %f\n", normalizedMag);
                //double calcCapacitance = ((1 - ((double)max_mag / mag)) / (pow((2 * PI * 1436), 2)) * 0.00136);
                double calcCapacitance = 1000000*(1-(max_mag/normalizedMag))/(((2*PI*1.436)*(2*PI*1.436)*1.46)); //nanoFarads
                //printf("phx_reverse = %f\n", phx_reverse);
                //double parasiticCap = baseResonateCap - findCenterOfCap()

                //printf("x at y=%.6f: %.6f\n", mag, x_reverse);

                //usleep(100000);

                /*if (fabs(phase_deg - phase_deg2) > 0.1)
                {
                    ChangeRelay(14, 0);
                    ChangeRelay(15, 0);
                    printf("Un-equal Phase Alarm");
                }*/
                if ((x_reverse > 8) && (fabs(phx_reverse - phase_deg2) > 1.5)){
                    ChangeRelay(14, 0);
                    ChangeRelay(15, 0);
                    printf("Out of Phase Interpolation Alarm");

                }
                else if (x_reverse < global_th)
                {
                    if (latched == 0) {
                        char bufferTime[64];
                        readSystemTime(bufferTime, sizeof(bufferTime));
                        latched = 1;
                        snprintf(latchedStamp, sizeof(latchedStamp), "%s", bufferTime);
                        writeToLatch(latched, latchedStamp);
                        //printf("Latched");
                    }
                    ChangeRelay(14, 0);
                    ChangeRelay(15, 0);
                    printf("Threshold Alarm");
                }

                else {
                    ChangeRelay(14, 1);
                    ChangeRelay(15, 1);

                }

//------------------------------------------This is I, Q and Mag-----------------------
                // printf("read =%lu (%x)\r\n", (ret & 0x7fff));
                printf("RES=%.1f, CAP=%.15f,  TH=%d, mag= %.0f, Phase = %0f, Phase2 = %0f, PhaseRx %0f, I= %.0f, Q= %.0f\r\n",
                    x_reverse, calcCapacitance, global_th, normalizedMag, phase_deg, phase_deg2, phx_reverse, aveI, aveQ);
                //for (i = 0; i < num_sec; i++)
                //{
                    //printf(".\n");
                    //usleep(1000000);
                //}

                client_fd = accept(server_fd, (struct sockaddr *)&client_addr, &addr_len);
                if (client_fd < 0) {
                    if (errno != EAGAIN && errno != EWOULDBLOCK) {
                        perror("Accept failed");
                    }
                    // No connection pending — just continue loop
                } else {
                    snprintf(buffer, sizeof(buffer), "Res=%.1f,Cap=%.0f,Th=%d,Mag=%.0f,Phase=%.3f,Phase2=%.3f,PhaseRX=%.3f,I=%.0f,Q=%.0f,latched=%d,LatchStamp=%s", x_reverse, calcCapacitance, global_th, mag, phase_deg, phase_deg2, phx_reverse, aveI, aveQ, latched, latchedStamp);
                    send(client_fd, buffer, strlen(buffer), 0);
                    //printf("Sent: %s\n", buffer);
                    close(client_fd);
                }
                normalizedMag = 0;
                mag = 0;
            }
			// close the socket server when not in use
            close(server_fd);


    }
    //======================================
    if (!strncmp(cmd, "CAL", 3))
    {
        unsigned long num_Reads = 3;
        //unsigned long num_sec = 1;
        //if (sscanf(cmd + 4, "%ld", &num_sec) > 0)
        LoadResistance(GIZMO_STATE_FILE("resistance.env"));
        LoadCapacitance(GIZMO_STATE_FILE("capacitance.env"));
        if (sscanf(cmd + 4, "%lu", &num_Reads) > 0)
        {
            if (num_Reads == 0 || num_Reads > 1000000) {
                fprintf(stderr, "CAL read count must be between 1 and 1000000\n");
                return;
            }
            //int num_sec = 0;
            //const double resistor_values[7] = {8.06, 16, 31.6, 61.9, 127, 255, 511};
            //const double resistor_values[7] = {1.125, 2.82, 19.82, 41.75, 87, 178, 10000000};
            //const double resistor_values[7] = {1.862, 3.482, 20.396, 42.83, 88.82, 183.82, 10000000};
            //const double capacitor_values[7] = {0.0060, 0.0088, 0.0938, 0.9496, 1.938, 2.495, 3.6166};
            //const double capacitor_values[7] = {0.0078, 0.01066, 0.09635, 0.96678, 1.963, 2.57161, 3.6166};
            double total_resistance[128] = {0};
            double total_capacitance[128] = {0};
            uint32_t relay_state_array[128];
            int i = 0;
            printf("Calibrating......\n\n");
            usleep(1000000);

                //------------RESISTORS----------------------------------------------------------
            for (uint32_t combo = 0; combo < 127; combo++) {
                relay_state = 0;

                // Set resistor relays
                for (int relay = 0; relay < 7; relay++) {
                    int bit = (combo >> relay) & 1;
                    uint32_t val = bit ? 2UL : 1UL; // 10 = ON, 01 = OFF
                    relay_state |= val << (2 * relay);
                }

                // Set capacitor relays OFF (relays 7–13)
                for (int relay = 7; relay <= 13; relay++) {
                    relay_state |= (1UL << (2 * relay));
                }

                // Force MSB nibble to A
                relay_state &= 0x0FFFFFFF;
                relay_state |= 0xA0000000;

                relay_state_array[combo] = relay_state;

                //printf("res_combo: %03u -> relay_state = 0x%08lX\n", combo, relay_state);
            }

            //calculating the resistance values
            int n = 7;
            //int m = 0;
            int combo_index = 0;  // Index to track all total_resistance combinations
            for (int k = 0; k < n; k++) {
                // Single resistor
                total_resistance[combo_index] = resistor_values[k];
                printf("Combo %d: %f\n", combo_index, total_resistance[combo_index]);
                combo_index++;

                int limit = k;  // include up to current resistor
                int max_combos = 1 << limit;

                // All combinations of previous resistors (plus current)
                for (int mask = 1; mask < max_combos; mask++) {
                    float combo_resistance = resistor_values[k];
                    printf("Combo %d: %d", combo_index, k);
                    for (int bit = 0; bit < limit; bit++) {
                        if (mask & (1 << bit)) {
                            combo_resistance += resistor_values[bit];
                            printf("+%d", bit);
                        }
                    }
                    total_resistance[combo_index] = combo_resistance;
                    printf(" = %f\n", combo_resistance);
                    combo_index++;
                }
            }

            //DISPLAY THE TOTAL RESISTANCE, WRITE TO GPIO1, calculate the magnitude
            total_res[0] = 0;
            for (int p = 0; p < 65; p++) {
                double ave = 0;
                double aveI = 0;
                double aveQ = 0;
                double phase_rad, phase_rad2, phase_deg, phase_deg2;
                total_res[p] = (p == 0) ? 0 : total_resistance[p - 1];
                printf("Total Resistance = %f, Relay State: 0x%08X, ", total_res[p], relay_state_array[p]);
                write_gpio_1(OFFSET, relay_state_array[p]);

                //adds the specified delay between reads
                //for (i = 0; i < num_sec; i++) {
                        //printf(".\n");
                        //usleep(100000);
                //}

                //Read the ADC and calculate the magnitude
                ClearADCMem(num_sampl_ADC);

                //Read ADC memory 3 times, stack each of the values on existing element
                for (unsigned long read_index = 0; read_index < num_Reads; read_index++){
                    ReadADCMem();
                }
                //unsigned long value[16384];
                //WriteADCtoFile(value, num_sampl_ADC);

		        //determine the DC offset of the ADC signal
                for (i = 0; i < num_sampl_ADC; i++)
                {
                    ave = ave + (double)(_ADC[i]);
                }

                //divide total by 3 from stacking values when reading the ADC 3 times
                ave = ave / num_sampl_ADC / num_Reads;
                //printf("ave= %f\r\n", ave);

                for (i = 0; i < num_sampl_ADC; i++)
                {
                    aveI += ((_ADC[i] / num_Reads)-ave) * _fI[i] * 0.001;
                    aveQ += ((_ADC[i] / num_Reads)-ave) * _fQ[i] * 0.001;
                }

                magArrayR[p] = sqrt((aveI * aveI) + (aveQ * aveQ));
                phase_rad = atan(aveQ/aveI);
                phase_rad2 = atan2(aveQ, aveI);
                phase_deg = phase_rad * (180 / PI);
                phase_deg2 = phase_rad2 * (180 / PI);
                phaseArrayR[p] = phase_deg;
                phase2ArrayR[p] = phase_deg2;
                printf("TH=%d ( mag= %.3f, I= %.3f, Q= %.3f )\r\n", global_th, magArrayR[p], aveI, aveQ);
            }
                //manually coding first value
                phase2ArrayR[0] = phaseArrayR[0];
                WriteCalibrationValuesR();
                WriteCalibrationValuesR_ph();
                compute_spline_coefficients();
                compute_phase_spline_coefficients();
                relay_state = 0xA5556AAA;
                //write_gpio_1(OFFSET, relay_state);
                //double x_reverse = interpolate_x(magArrayR[127], 100, 1e-6);
                //printf("x at y=%.6f: %.6f\n", magArrayR[127], x_reverse);


                //--------------------Capacitors------------------------------------------------------------------------------
                      // === Capacitor loop (relays 7–13) ===
            uint32_t base_state = 0xA5556AAA;  // start state when combo == 0

            for (uint32_t combo = 0; combo < 128; combo++) {
                uint32_t relay_state = base_state;

                // Clear capacitor relays bits (2 bits per relay, relays 7 to 13)
                for (int relay = 7; relay <= 13; relay++) {
                    relay_state &= ~(3UL << (2 * relay)); // clear 2 bits
                }

                // Set capacitor relays bits according to combo
                for (int relay = 7; relay <= 13; relay++) {
                    int bit = (combo >> (relay - 7)) & 1;
                    uint32_t val = bit ? 2UL : 1UL; // 10 = ON, 01 = OFF
                    relay_state |= val << (2 * relay);
                }

                relay_state_array[combo] = relay_state;

                printf("cap_combo: %03u -> relay_state = 0x%08X\n", combo, relay_state);

            }

            //calculating the capacitance values
            //int n = 8;
            //int m = 0;
            combo_index = 0;  // Index to track all total_capacitance combinations
            for (int k = 0; k < n; k++) {
                // Single capacitor
                total_capacitance[combo_index] = capacitor_values[k];
                printf("Combo %d: %f\n", combo_index, total_capacitance[combo_index]);
                combo_index++;

                int limit = k;  // include up to current resistor
                int max_combos = 1 << limit;

                // All combinations of previous capacitors (plus current)
                for (int mask = 1; mask < max_combos; mask++) {
                    float combo_capacitance = capacitor_values[k];
                    printf("Combo %d: %d", combo_index, k);
                    for (int bit = 0; bit < limit; bit++) {
                        if (mask & (1 << bit)) {
                            combo_capacitance += capacitor_values[bit];
                            printf("+%d", bit);
                        }
                    }
                    total_capacitance[combo_index] = combo_capacitance;
                    printf(" = %f\n", combo_capacitance);
                    combo_index++;
                }
            }
            //double preSortCap[127];
            //for (int i = 0; i < 127; i++){
                //preSortCap[i] = total_capacitance[i];
                //printf("preSortCap[%d] = %f\n", i, preSortCap[i]);
            //}
            //sort_array(preSortCap, 127);

            //DISPLAY THE TOTAL Capacitance, WRITE TO GPIO1, calculate the magnitude
            total_cap[0] = 0;
            for (int p = 0; p < 128; p++){
                double ave = 0;
                double aveI = 0;
                double aveQ = 0;
                double phase_rad, phase_rad2, phase_deg, phase_deg2;
                total_cap[p] = (p == 0) ? 0 : total_capacitance[p - 1];
                //total_cap[p+1] = preSortCap[p];
                printf("Total Capacitance = %f, Relay State: 0x%08X, ", total_cap[p], relay_state_array[p]);
                write_gpio_1(OFFSET, relay_state_array[p]);

                //adds the specified delay between reads
                //for (i = 0; i < num_sec; i++)
                //{
                    //printf(".\n");
                    //usleep(100000);
                //}

                //Read the ADC and calculate the magnitude
                ClearADCMem(num_sampl_ADC);

                //Read ADC memory 3 times, stack each of the values on existing element
                for (unsigned long read_index = 0; read_index < num_Reads; read_index++){
                    ReadADCMem();
                }
                //unsigned long value[16384];
                //WriteADCtoFile(value, num_sampl_ADC);

		        //determine the DC offset of the ADC signal
                for (i = 0; i < num_sampl_ADC; i++)
                {
                    ave = ave + (double)(_ADC[i]);
                }

                //divide total by 3 from stacking values when reading the ADC 3 times
                ave = ave / num_sampl_ADC / num_Reads;
                //printf("ave= %f\r\n", ave);

                for (i = 0; i < num_sampl_ADC; i++)
                {
                    aveI += ((_ADC[i] / num_Reads)-ave) * _fI[i] * 0.001;
                    aveQ += ((_ADC[i] / num_Reads)-ave) * _fQ[i] * 0.001;
                }

                aveIArray[p] = aveI;
                aveQArray[p] = aveQ;

                magArrayC[p] = sqrt((aveI * aveI) + (aveQ * aveQ));
                phase_rad = atan(aveQ/aveI);
                phase_rad2 = atan2(aveQ, aveI);
                phase_deg = phase_rad * (180 / PI);
                phase_deg2 = phase_rad2 * (180 / PI);
                phaseArrayC[p] = phase_deg;
                phase2ArrayC[p] = phase_deg2;
                printf("TH=%d ( mag= %.3f, I= %.3f, Q= %.3f )\r\n", global_th, magArrayC[p], aveI, aveQ);
            }

            relay_state = 0xA5556AAA;
            write_gpio_1(OFFSET, relay_state);
            usleep(10000);

            // Sort the two capacitor arrays in order of capacitance because they are not binary weighted
            sort_by_total_cap(total_cap, magArrayC, phaseArrayC, phase2ArrayC, 128);
            // Print the sorted array for verification
            for (int i = 0; i < 128; i++) {
                printf("Cap: %f, Mag: %f\n", total_cap[i], magArrayC[i]);
            }
            WriteCalibrationValuesC();
            WriteCalibrationValuesC_ph();
            baseResonateCap = findCenterOfCap(total_cap, magArrayC);
        }
    }

    //======================================
    if (!strncmp(cmd, "set_th", 6))
    {
        int th;
        //int val;
        if (sscanf(cmd + 7, "%d", &th) > 0)
        {
            printf("setting Alarm threshold to =%d\r\n", th);
            global_th = th;
        }
    }
    //======================================
    if (!strncmp(cmd, "read_adc", 8))
    {
        relay_state = 0xA5556AAA;
        write_gpio_1(OFFSET, relay_state);
        usleep(1000000);
        unsigned long value[2048];

        ClearADCMem(num_sampl_ADC);
        for (int i=0; i < 3; i++){
            ReadADCMem();
        }
        for (int i = 0; i < num_sampl_ADC; i++)
        {
            value[i] = (_ADC[i] / 3);
        }
        WriteADCtoFile(value, num_sampl_ADC);
    }
    //======================================
    if (!strncmp(cmd, "set_time", 8))
    {
        //int addr = 0;
        unsigned int val1 = 0;
        unsigned int val2 = 0;
        if (sscanf(cmd + 8, "%x %x", &val1, &val2) > 0)
        {

        }
        printf("done\r\n");
    }
    //======================================
    if (!strncmp(cmd, "rsw", 3))
    {

    }
    //======================================
    if (!strncmp(cmd, "clr_m1", 6))
    {
        int i = 0;
        printf("clr start 0 to 4095\r\n");
        for (i = 0; i < 4096; i++)
        {
            write_bram_0(i, 0);
        }
        printf("clr done\r\n");
    }
    //======================================
    if (!strncmp(cmd, "clr_m2", 6))
    {
        int i = 0;
        printf("clr start 0 to 4095\r\n");
        for (i = 0; i < 4096; i++)
        {
            write_bram_1(i, 0);
        }
        printf("clr done\r\n");
    }
    //======================================
    if (!strncmp(cmd, "wled", 4))
    {

    }
    //======================================
    if (!strncmp(cmd, "set_res", 7))
    {
        int val = 0;
        if (sscanf(cmd + 7, "%d", &val) > 0)
        {
            SetRes(val);
        }
    }
    //======================================
    if (!strncmp(cmd, "set_rly", 7))
    {
        int addr = 0;
        int val1 = 0;

        if (sscanf(cmd + 7, "%d %d", &addr, &val1) > 0)
        {
            if (val1 > 1) val1 = 0;
            if (addr > 15) addr = 0;

            printf("Relay value before clearing relay_state=0x%08lx\r\n", relay_state);
            // Clear both bits for this relay
            relay_state &= ~(3UL << (2 * addr));
            printf("Relay value before clearing relay_state=0x%08lx\r\n", relay_state);

            // Set ON (10) or OFF (01)
            // ON (val1 == 1) = 10 = binary 2
            // OFF (val1 == 0) = 01 = binary 1
            relay_state |= ((val1 ? 2UL : 1UL) << (2 * addr));

            printf(" addr=%d, val=%d\r\n", addr, val1);
            printf(" relay_state=0x%08lx\r\n", relay_state);

            write_gpio_1(OFFSET, relay_state);
        }
        return;
    }

    //======================================
    if (!strncmp(cmd, "wreg", 4))
    {


    }
    //======================================
    if (!strncmp(cmd, "gw1", 3))
    {


    }
    //======================================
    if (!strncmp(cmd, "gr1", 3))
    {

    }
    //======================================
    if (!strncmp(cmd, "bw1", 3))
    {

    }
    //======================================
    if (!strncmp(cmd, "br1", 3))
    {

    }
    //======================================
    if (!strncmp(cmd, "bw2", 3))
    {

    }
    //======================================
    if (!strncmp(cmd, "br2", 3))
    {


    }
    //======================================
    if (!strncmp(cmd, "len", 3))
    {


    }

    //======================================
    if (!strncmp(cmd, "amp", 3))
    {

    }

}

//===========================================================================
// ============= HELPERS ====================================================
//===========================================================================

void SetRes(int res)
{
    //printf("Hello from SetRes");
    int i = 0;
    int t_res = res;
    if (res < 0) // all open
    {
        for (i = 0; i < 6; i++)
        {
            ChangeRelay(i, 0);
            printf("all r open \r\n");
        }
    }
    else if (res < 32)
    {
        ChangeRelay(5, 1);
        printf("r5 closed \r\n");
        t_res = res + 1;
        for (i = 4; i >= 0; i--)
        {
            if (t_res > (1 << i))
            {
                ChangeRelay(i, 0);
                printf("%d set to %d\r\n", i, 0);
                t_res = t_res - (1 << i);
            }
            else
            {
                ChangeRelay(i, 1);
                printf("%d set to %d\r\n", i, 1);
            }
        }
    }
    printf("R set to %d\r\n", res);
}
void ChangeRelay(int relay_addr, int state)
{
    if (state > 1) state = 0;
    if (relay_addr > 15) relay_addr = 0;

    //printf("Relay value before clearing relay_state=0x%08lx\r\n", relay_state);
    // Clear both bits for this relay
    relay_state &= ~(3UL << (2 * relay_addr));
    //printf("Relay value before clearing relay_state=0x%08lx\r\n", relay_state);

    // Set ON (10) or OFF (01)
    // ON (val1 == 1) = 10 = binary 2
    // OFF (val1 == 0) = 01 = binary 1
    relay_state |= ((state ? 2UL : 1UL) << (2 * relay_addr));

    //printf(" relay_addr=%d, val=%d\r\n", relay_addr, state);
    //printf(" relay_state=0x%08lx\r\n", relay_state);

    write_gpio_1(OFFSET, relay_state);
}



void Load_auto()
{
    FILE *file = fopen(GIZMO_STATE_FILE("Rcalibration_ph.csv"), "r");
    if (file == NULL)
    {
        perror("Failed to open Rcalibration_ph.csv");
        return;
    }

    int index = 0;
    double res, mag, phase, phase2;
    while (index < 65)
    {
        int fields = fscanf(file, "%lf,%lf,%lf,%lf", &res, &mag, &phase, &phase2);
        if (fields == EOF) {
            break;
        }
        if (fields == 4)
        {
            total_res[index] = res;
            magArrayR[index] = mag;
            phaseArrayR[index] = phase;
            phase2ArrayR[index] = phase2;
            index++;
        }
        else
        {
            int character;
            do {
                character = fgetc(file);
            } while (character != '\n' && character != EOF);
            if (character == EOF) {
                break;
            }
        }
    }
    //print everything
    for (int i = 0; i < 65; i++){
        printf("total_res[%d] = %lf, magArrayR[%d] = %lf, phaseArrayR[%d] = %lf, phase2ArrayR[%d] = %lf\n", i, total_res[i], i, magArrayR[i], i, phaseArrayR[i], i, phase2ArrayR[i]);
    }

    fclose(file);
}

void LoadResistance(const char *filename)
{
    FILE *file = fopen(filename, "r");
    if (!file) {
        perror("Error opening resistance file");
        return;
    }

    char line[128];

    while (fgets(line, sizeof(line), file)) {
        int index;
        double res_value;

        /* Match lines like: R1=1.125 */
        if (sscanf(line, "R%d=%lf", &index, &res_value) == 2) {
            if (index >= 1 && index <= 7) {
                resistor_values[index - 1] = res_value;
            }
        }
    }

    fclose(file);
}

void LoadCapacitance(const char *filename)
{
    FILE *file = fopen(filename, "r");
    if (!file) {
        perror("Error opening resistance file");
        return;
    }

    char line[128];

    while (fgets(line, sizeof(line), file)) {
        int index;
        double cap_value;

        /* Match lines like: R1=1.125 */
        if (sscanf(line, "C%d=%lf", &index, &cap_value) == 2) {
            if (index >= 1 && index <= 7) {
                capacitor_values[index - 1] = cap_value;
            }
        }
    }

    fclose(file);
}

/*
This is where BRAM0 is filled for the DAC to output.
**Should update this to just calculate the value to output here instead of read from file
This is where the average in-phase aveI and average quadrature aveQ components arrays are populated.
The values are populated by reading a file and using a loop to fill the _fI[n] and _fQ[n] arrays.
*/


void LoadTH(const char *filename)
{
    FILE *file = fopen(filename, "r");
    if (!file) {
        perror("Error opening threshold file");
        return;
    }

    char line[128];
    while (fgets(line, sizeof(line), file)) {
        if (strncmp(line, "export threshold=", 17) == 0) {
            global_th = atoi(line + 17);
            break;
        }
    }

    fclose(file);
}

int WriteADCtoFile(unsigned long ADC[], int digitsToWrite){
    // Write _ADC, _fI and _fQ to file
    //printf("In WriteADCtoFile");
    FILE *fp = fopen(GIZMO_STATE_FILE("adc.csv"), "w");
    if (fp == NULL)
    {
        perror("Failed to open adc.csv");
        return 1;
    }

    for (int i = 0; i < digitsToWrite; i++)
    {
        fprintf(fp, "%lu, %f, %f\n", ADC[i], _fI[i], _fQ[i]);
    }

    fclose(fp);
    return 0;
}

int WriteCalibrationValuesR(){
    // Write the values from calibration to a file
    FILE *fp = fopen(GIZMO_STATE_FILE("Rcalibration.csv"), "w");
    if (fp == NULL)
    {
        perror("Failed to open Rcalibration.csv");
        return 1;
    }

    for (int i = 0; i < 65; i++)
    {
        fprintf(fp, "%f, %f\n", total_res[i], magArrayR[i]);
    }

    fclose(fp);
    return 0;
}

int WriteCalibrationValuesR_ph(){
    // Write the values from calibration to a file
    FILE *fp = fopen(GIZMO_STATE_FILE("Rcalibration_ph.csv"), "w");
    if (fp == NULL)
    {
        perror("Failed to open Rcalibration_ph.csv");
        return 1;
    }

    for (int i = 0; i < 65; i++)
    {
        fprintf(fp, "%f, %f, %f, %f\n", total_res[i], magArrayR[i], phaseArrayR[i], phase2ArrayR[i]);
    }

    fclose(fp);
    return 0;
}

int WriteCalibrationValuesC(){
    // Write the values from calibration to a file
    FILE *fp = fopen(GIZMO_STATE_FILE("Ccalibration.csv"), "w");
    if (fp == NULL)
    {
        perror("Failed to open Ccalibration.csv");
        return 1;
    }

    for (int i = 0; i < 128; i++)
    {
        fprintf(fp, "%f, %f\n", total_cap[i], magArrayC[i]);
    }

    fclose(fp);
    return 0;
}

int WriteCalibrationValuesC_ph(){
    // Write the values from calibration to a file
    FILE *fp = fopen(GIZMO_STATE_FILE("Ccalibration_ph.csv"), "w");
    if (fp == NULL)
    {
        perror("Failed to open Ccalibration_ph.csv");
        return 1;
    }

    for (int i = 0; i < 128; i++)
    {
        fprintf(fp, "%f, %f, %f, %f\n", total_cap[i], magArrayC[i], phaseArrayC[i], phase2ArrayC[i]);
    }

    fclose(fp);
    return 0;
}

int ReadADCMem()
{
    for (int i = 0; i < num_sampl_ADC; i++)
    {
        _ADC[i] += _bram_1[i];
    }
    return 0;
}
int ClearADCMem(unsigned long len)
{
    for (unsigned long i = 0; i < len; i++)
    {
        _ADC[i] = 0;
    }
    return 0;
}

void generate_sine_wave_to_bram(volatile uint32_t *bram_base)
{
    double sine_wave[NUM_SAMPLES_DAC];
    uint32_t bram_values[NUM_SAMPLES_DAC];

    // Generate sine wave values
    for (int i = 0; i < NUM_SAMPLES_DAC; i++) {
        sine_wave[i] = AMPLITUDE * sin(frequency * PI * i / NUM_SAMPLES_DAC);
        bram_values[i] = (uint32_t)(sine_wave[i] + OFFSET_V);
    }

    // Generate reference fI and fQ wave values
    for (int i = 0; i < NUM_SAMPLES_DAC; i++) {
        _fI[i] = sin(2* frequency * PI * i / NUM_SAMPLES_DAC);
        _fQ[i] = cos(2 * frequency * PI * i / NUM_SAMPLES_DAC);
    }


    // // Write to BRAM (pairing two 16-bit values per 32-bit address)
    for (int i = 0; i < NUM_SAMPLES_DAC; i++) {
        bram_base[i] = bram_values[i];
    }

}

void compute_spline_coefficients() {
    double h[nTerp], alpha[nTerp], l[nTerp], mu[nTerp], z[nTerp];
    for (int i = 0; i < nTerp - 1; i++)
        h[i] = total_res[i + 1] - total_res[i];

    for (int i = 1; i < nTerp - 1; i++)
        alpha[i] = (3.0 / h[i]) * (magArrayR[i + 1] - magArrayR[i]) - (3.0 / h[i - 1]) * (magArrayR[i] - magArrayR[i - 1]);

    l[0] = 1.0; mu[0] = z[0] = 0.0;
    for (int i = 1; i < nTerp - 1; i++) {
        l[i] = 2.0 * (total_res[i + 1] - total_res[i - 1]) - h[i - 1] * mu[i - 1];
        mu[i] = h[i] / l[i];
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i];
    }
    l[nTerp - 1] = 1.0; z[nTerp - 1] = c[nTerp - 1] = 0.0;

    for (int j = nTerp - 2; j >= 0; j--) {
        c[j] = z[j] - mu[j] * c[j + 1];
        b[j] = (magArrayR[j + 1] - magArrayR[j]) / h[j] - h[j] * (c[j + 1] + 2.0 * c[j]) / 3.0;
        d[j] = (c[j + 1] - c[j]) / (3.0 * h[j]);
        a[j] = magArrayR[j];
    }
}

void compute_phase_spline_coefficients() {
    double h[nTerp], alpha[nTerp], l[nTerp], mu[nTerp], z[nTerp];

    for (int i = 0; i < nTerp - 1; i++)
        h[i] = magArrayR[i + 1] - magArrayR[i];

    for (int i = 1; i < nTerp - 1; i++)
        alpha[i] = (3.0 / h[i]) * (phase2ArrayR[i + 1] - phase2ArrayR[i]) -
                   (3.0 / h[i - 1]) * (phase2ArrayR[i] - phase2ArrayR[i - 1]);

    l[0] = 1.0; mu[0] = z[0] = 0.0;
    for (int i = 1; i < nTerp - 1; i++) {
        l[i] = 2.0 * (magArrayR[i + 1] - magArrayR[i - 1]) - h[i - 1] * mu[i - 1];
        mu[i] = h[i] / l[i];
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i];
    }

    l[nTerp - 1] = 1.0; z[nTerp - 1] = c_phi[nTerp - 1] = 0.0;

    for (int j = nTerp - 2; j >= 0; j--) {
        c_phi[j] = z[j] - mu[j] * c_phi[j + 1];
        b_phi[j] = (phase2ArrayR[j + 1] - phase2ArrayR[j]) / h[j] -
                   h[j] * (c_phi[j + 1] + 2.0 * c_phi[j]) / 3.0;
        d_phi[j] = (c_phi[j + 1] - c_phi[j]) / (3.0 * h[j]);
        a_phi[j] = phase2ArrayR[j];
    }
}

double interpolate_y(double x_val) {
    int i;
    for (i = 0; i < nTerp - 1; i++)
        if (x_val < total_res[i + 1])
            break;

    double dx = x_val - total_res[i];
    return a[i] + b[i]*dx + c[i]*dx*dx + d[i]*dx*dx*dx;
}



double interpolate_x(double y_val, int max_iter, double tol) {
    double max_y = interpolate_y(total_res[0]);
    double min_y = max_y;  // initialize min_y to the first y

    // Find candidate interval
    for (int i = 0; i < nTerp - 1; i++) {
        double y0 = interpolate_y(total_res[i]);
        double y1 = interpolate_y(total_res[i + 1]);
        //printf("y0 = %f, y1 = %f\n", y0, y1);

        // Track maximum and minimum y
        if (y0 > max_y) max_y = y0;
        if (y1 > max_y) max_y = y1;

        if (y0 < min_y) min_y = y0;
        if ((y1 < min_y) && (y1 != 0)) min_y = y1;
        //printf("min_y = %f\n", min_y);

        // Handle out-of-range y_val
        if (y_val < min_y) return 0.1;        // below known minimum (adjust this as needed)

        if ((y_val >= y0 && y_val <= y1) || (y_val <= y0 && y_val >= y1)) {
            // Bisection within [x[i], x[i+1]]
            double low = total_res[i], high = total_res[i + 1];
            double mid = 0.5 * (low + high);
            for (int j = 0; j < max_iter; j++) {
                mid = 0.5 * (low + high);
                double y_mid = interpolate_y(mid);
                if (fabs(y_mid - y_val) < tol)
                    return mid;
                if ((y_mid > y_val) == (y0 > y_val))
                    low = mid;
                else
                    high = mid;
            }
            return mid; // best effort
        }
    }
    if (y_val > max_y) return 1050;       // above known maximum

    return NAN; // y_val not within known interpolation range
}

double interpolate_phase(double mag_val, double phaseMeasured) {
    (void)phaseMeasured;
    int i;
    for (i = 0; i < nTerp - 1; i++)
        if (mag_val < magArrayR[i + 1])
            break;

    if (i >= nTerp - 1) i = nTerp - 2;

    double dx = mag_val - magArrayR[i];
    double phase = a_phi[i] + b_phi[i]*dx + c_phi[i]*dx*dx + d_phi[i]*dx*dx*dx;

    // Clamp phase to [-180, 0]
    // if (phase > 0.0)
    //     phase = 0;
    // else if (phase < -180.0)
    //     phase = -180.0;

    return phase;
}


void readSystemTime(char* buffer, size_t bufferSize) {
    time_t rawtime;
    struct tm *timeinfo;

    // Get the current calendar time
    time(&rawtime);

    // Convert to local time (broken-down time)
    timeinfo = localtime(&rawtime);

    // Format: e.g. "2025-05-07 14:23:10"
    strftime(buffer, bufferSize, "%Y-%m-%d %H:%M:%S", timeinfo);
}

void readFromLatch() {
    FILE *file = fopen(GIZMO_STATE_FILE("latchState.env"), "r");
    if (file == NULL) {
        perror("Failed to open latchState.env");
        return;
    }

    char line[128];

    if (fgets(line, sizeof(line), file)) {
        if (sscanf(line, "latched=%d", &latched) != 1) {
            fprintf(stderr, "Malformed latched line\n");
        }
    }

    if (fgets(line, sizeof(line), file)) {
        size_t stamp_length = strcspn(line, "\r\n");
        if (stamp_length >= sizeof(latchedStamp)) {
            stamp_length = sizeof(latchedStamp) - 1;
        }
        memcpy(latchedStamp, line, stamp_length);
        latchedStamp[stamp_length] = '\0';
    }

    fclose(file);

    //printf("Latched = %d", latched);
}

void writeToLatch(int latchedValue, const char *timestamp) {
    FILE *file = fopen(GIZMO_STATE_FILE("latchState.env"), "w");
    if (file == NULL) {
        perror("Failed to write to latchState.env");
        return;
    }

    fprintf(file, "latched=%d\n", latchedValue);
    fprintf(file, "%s\n", timestamp);

    fclose(file);
}

void sort_by_total_cap(double total_cap[], double magArrayC[], double phaseArrayC[], double phase2ArrayC[], int size) {
    for (int i = 0; i < size - 1; i++) {
        int min_idx = i;
        for (int j = i + 1; j < size; j++) {
            if (total_cap[j] < total_cap[min_idx]) {
                min_idx = j;
            }
        }

        if (min_idx != i) {
            // Swap total_cap[i] and total_cap[min_idx]
            double temp_cap = total_cap[i];
            total_cap[i] = total_cap[min_idx];
            total_cap[min_idx] = temp_cap;

            // Swap magArrayC[i] and magArrayC[min_idx]
            double temp_mag = magArrayC[i];
            magArrayC[i] = magArrayC[min_idx];
            magArrayC[min_idx] = temp_mag;

            // Swap phaseArrayC[i] and phaseArrayC[min_idx]
            double temp_phase = phaseArrayC[i];
            phaseArrayC[i] = phaseArrayC[min_idx];
            phaseArrayC[min_idx] = temp_phase;

            // Swap phase2ArrayC[i] and phase2ArrayC[min_idx]
            double temp_phase2 = phase2ArrayC[i];
            phase2ArrayC[i] = phase2ArrayC[min_idx];
            phase2ArrayC[min_idx] = temp_phase2;
        }
    }
}

// Sorts an array in ascending order
void sort_array(double arr[], int size) {
    for (int i = 0; i < size - 1; ++i) {
        for (int j = 0; j < size - i - 1; ++j) {
            if (arr[j] > arr[j + 1]) {
                // Swap
                double temp = arr[j];
                arr[j] = arr[j + 1];
                arr[j + 1] = temp;
            }
        }
    }
}

double findCenterOfCap(double array_x[], double array_y[]) {

    //find the maximum magnitude in the array
    //outputs the index number in the array and the value at the index
    double max = 0;
    int maxIndex = 0;
    for (int i = 0; i < 128; i++){
        if ( array_y[i] > max){
            max = array_y[i];
            maxIndex = i;
        }
    }

    // Get the mean near the peak without indexing outside the calibration arrays.
    int windowStart = (maxIndex > 8) ? maxIndex - 8 : 0;
    int windowEnd = (maxIndex + 8 < 128) ? maxIndex + 8 : 128;
    double sum = 0;
    for (int i = windowStart; i < windowEnd; i++){
        sum += array_y[i];
    }
    double mean = sum / (windowEnd - windowStart);
    printf("Maximum Magnitude in Capacitance Array = %f at i = %d\n", max, maxIndex);
    printf("Mean = %f\n", mean);

    double minLeft = fabs(mean - array_y[maxIndex]);
    int minLeftIndex = maxIndex;
    double localLeft;
    for (int i = windowStart; i < maxIndex; i++){
        localLeft = mean - array_y[i];
        if (fabs(localLeft) < minLeft){
            minLeft = fabs(localLeft);
            minLeftIndex = i;
        }
    }
    printf("Minimum Magnitude in Left side of Max = %f at i = %d\n", minLeft, minLeftIndex);

    double minRight = fabs(mean - array_y[maxIndex]);
    int minRightIndex = maxIndex;
    double localRight;
    for (int i = maxIndex + 1; i < windowEnd; i++){
        localRight = mean - array_y[i];
        if (fabs(localRight) < minRight){
            minRight = fabs(localRight);
            minRightIndex = i;
        }
    }
    printf("Minimum Magnitude in Right side of Max = %f at i = %d\n", minRight, minRightIndex);

    double center_x = (array_x[minRightIndex] + array_x[minLeftIndex]) / 2;
    printf("Center of Distribution is approximately: %f", center_x);

    return center_x;
}

int finxMaxMagnitude(void) {
    double max = magArrayR[0]; //initial array value
    int data_size = sizeof(magArrayR) / sizeof(magArrayR[0]);

    for (int i = 1; i < data_size; i++) {
        if (magArrayR[i] > max){
            max = magArrayR[i];
        }
    }

    return (int)max;

}

int readNormalizeMagFlag()
{
    FILE *file = fopen(GIZMO_STATE_FILE("normalizeMagFlag.env"), "r");
    if (file == NULL)
    {
        perror("Failed to open normalizeMagFlag.env");
        return 0; // Default to 0 if the file can't be opened
    }

    char line[64];
    int flag = 0; // Default value

    if (fgets(line, sizeof(line), file) != NULL)
    {
        // Expected format: normalizeMagFlag=0 or normalizeMagFlag=1
        char *equals = strchr(line, '=');
        if (equals != NULL)
        {
            int value = atoi(equals + 1); // Convert the number after '='
            if (value == 1)
                flag = 1;
        }
    }

    fclose(file);
    return flag;
}

int writeNormalizeMagFlag(int flag)
{
    if (flag != 0 && flag != 1)
    {
        // Only allow 0 or 1
        return -1;
    }

    FILE *file = fopen(GIZMO_STATE_FILE("normalizeMagFlag.env"), "w");  // Open for writing (overwrite)
    if (file == NULL)
    {
        perror("Failed to open normalizeMagFlag.env for writing");
        return -1;
    }

    fprintf(file, "normalizeMagFlag=%d\n", flag);
    fclose(file);

    return 0; // Success
}

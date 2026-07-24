/**
 @file eve_example.c
 */
/*
 * ============================================================================
 * History
 * =======
 * Nov 2019		Initial beta for FT81x and FT80x
 * Mar 2020		Updated beta - added BT815/6 commands
 * Mar 2021		Beta with BT817/8 support added
 *
 *
 *
 *
 *
 * (C) Copyright,  Bridgetek Pte. Ltd.
 * ============================================================================
 *
 * This source code ("the Software") is provided by Bridgetek Pte Ltd
 * ("Bridgetek") subject to the licence terms set out
 * http://www.ftdichip.com/FTSourceCodeLicenceTerms.htm ("the Licence Terms").
 * You must read the Licence Terms before downloading or using the Software.
 * By installing or using the Software you agree to the Licence Terms. If you
 * do not agree to the Licence Terms then do not download or use the Software.
 *
 * Without prejudice to the Licence Terms, here is a summary of some of the key
 * terms of the Licence Terms (and in the event of any conflict between this
 * summary and the Licence Terms then the text of the Licence Terms will
 * prevail).
 *
 * The Software is provided "as is".
 * There are no warranties (or similar) in relation to the quality of the
 * Software. You use it at your own risk.
 * The Software should not be used in, or for, any medical device, system or
 * appliance. There are exclusions of Bridgetek liability for certain types of loss
 * such as: special loss or damage; incidental loss or damage; indirect or
 * consequential loss or damage; loss of income; loss of business; loss of
 * profits; loss of revenue; loss of contracts; business interruption; loss of
 * the use of money or anticipated savings; loss of information; loss of
 * opportunity; loss of goodwill or reputation; and/or loss of, damage to or
 * corruption of data.
 * There is a monetary cap on Bridgetek's liability.
 * The Software may have subsequently been amended by another user and then
 * distributed by that other user ("Adapted Software").  If so that user may
 * have additional licence terms that apply to those amendments. However, Bridgetek
 * has no liability in relation to those amendments.
 * ============================================================================
 */

 #include <stdint.h>
 #include "EVE.h"
 #include "../include/HAL.h"
 #include "MCU.h"
 #include "eve_example.h"


 //includes for reading IP address
 #include <stdio.h>
 #include <stdlib.h>
 #include <stdint.h>
 #include <fcntl.h>
 #include <linux/i2c-dev.h>
 #include <string.h>
 #include <unistd.h>
 #include <sys/socket.h>
 #include <sys/ioctl.h>
 #include <net/if.h>
 #include <netinet/in.h>
 #include <arpa/inet.h>
 #include <time.h>

 //Sockets server port and IP
 #define PORT 5055
 #define SERVER_IP "127.0.0.1"

 //maximum interface name length
 #define MAX_iface_NAME 16
 #define IP_ADDRESS_MAX_LEN INET_ADDRSTRLEN

 //Temperature Sensor definitions
 #define I2C_DEVICE "/dev/i2c-7"  // Change this if using a different I2C bus
 #define MCP9808_ADDR 0x18
 #define TEMP_REGISTER 0x05

 float readTemperature(){
	int file;
    char buf[3];

    // Open I2C device
    if ((file = open(I2C_DEVICE, O_RDWR)) < 0) {
        perror("Failed to open the I2C bus");
        return -1000.0;
    }

    // Set the I2C address
    if (ioctl(file, I2C_SLAVE, MCP9808_ADDR) < 0) {
        perror("Failed to acquire bus access and/or talk to slave");
        close(file);
        return -1000.0;
    }

    // Write the pointer to the temperature register
    buf[0] = TEMP_REGISTER;
    if (write(file, buf, 1) != 1) {
        perror("Failed to write to the I2C bus");
        close(file);
        return -1000.0;
    }

    // Read 2 bytes from the temperature register
    if (read(file, buf, 2) != 2) {
        perror("Failed to read from the I2C bus");
        close(file);
        return -1000.0;
    }

    close(file);

    // Parse temperature
    uint16_t temp_raw = (buf[0] << 8) | buf[1];
    temp_raw &= 0x1FFF;  // Mask off flags
    float temperature = temp_raw & 0x1000 ? (temp_raw - 8192) * 0.0625 : temp_raw * 0.0625;

    printf("Temperature: %.2f°C\n", temperature);

	return temperature;

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


 //function to read IP address of host
 char* get_ip_address(const char* iface) {
	 int fd;
	 struct ifreq ifr;
	 char ip_address[IP_ADDRESS_MAX_LEN];

	 // Create a socket
	 fd = socket(AF_INET, SOCK_DGRAM, 0);
	 if (fd == -1) {
		 perror("socket");
		 //exit(EXIT_FAILURE);
		 return NULL;
	 }

	 // Get the IP address associated with the interface
	 strncpy(ifr.ifr_name, iface, IFNAMSIZ - 1);
	 ifr.ifr_name[IFNAMSIZ - 1] = '\0';

	 if (ioctl(fd, SIOCGIFADDR, &ifr) == -1) {
		 perror("ioctl");
		 close(fd);
		 //exit(EXIT_FAILURE);
		 return NULL;
	 }

	 // Convert the IP address to a human-readable string
	 inet_ntop(AF_INET, &((struct sockaddr_in *)&ifr.ifr_addr)->sin_addr, ip_address, IP_ADDRESS_MAX_LEN);

	 // Close the socket
	 close(fd);

	 // Allocate memory for the IP address string
	 char* ip_address_str = (char*)malloc(IP_ADDRESS_MAX_LEN * sizeof(char));
	 if (ip_address_str == NULL) {
		 perror("malloc");
		 //exit(EXIT_FAILURE);
		 return NULL;
	 }

	 // Copy the IP address to the allocated string
	 strcpy(ip_address_str, ip_address);

	 return ip_address_str;
 }


 extern const uint8_t font0[];
 const EVE_GPU_FONT_HEADER *font0_hdr = (const EVE_GPU_FONT_HEADER *)font0;

 void readFromZMonSocket(void)
 {
	 printf("Beginning of Socket Function\n");

	 int sock;
	 struct sockaddr_in server_addr;
	 char buffer[256] = {0};
	 int attempt = 1;

	 while (1) {
		 if ((sock = socket(AF_INET, SOCK_STREAM, 0)) < 0) {
			 perror("Socket creation error");
			 exit(EXIT_FAILURE);
		 }

		 // Set server address
		 server_addr.sin_family = AF_INET;
		 server_addr.sin_port = htons(PORT);

		 if (inet_pton(AF_INET, SERVER_IP, &server_addr.sin_addr) <= 0) {
			 perror("Invalid address");
			 close(sock);
			 exit(EXIT_FAILURE);
		 }

		 printf("Connection attempt %d...\n", attempt);

		 if (connect(sock, (struct sockaddr *)&server_addr, sizeof(server_addr)) == 0) {
			 printf("Connected on attempt %d.\n", attempt);
			 break;  // Exit loop if connection is successful
		 }

		 perror("Connection failed");
		 close(sock);
		 attempt++;
		 sleep(1);  // Wait before retrying
	 }

	 // Receive data
	 ssize_t bytes_read = read(sock, buffer, sizeof(buffer) - 1);
	 if (bytes_read < 0) {
		 perror("Read failed");
		 close(sock);
		 return;
	 }
	 buffer[bytes_read] = '\0';
	 printf("Received: %s\n", buffer);

	 close(sock);
	 printf("End of Socket Function\n");
 }

 char* readFromZMonSocket2(void) {
	 int sock;
	 struct sockaddr_in server_addr;
	 char buffer[256] = {0};
	 int attempt = 1;

	 while (1) {
		 if (attempt > 1) {
			 printf("Attempt %d: Connecting to ZMon Script...\n", attempt);
			 sleep(3);
		 }

		 // Create socket
		 if ((sock = socket(AF_INET, SOCK_STREAM, 0)) < 0) {
			 perror("Socket creation error");
			 sleep(1);
			 attempt++;
			 continue;
		 }

		 // Set server address
		 server_addr.sin_family = AF_INET;
		 server_addr.sin_port = htons(PORT);

		 if (inet_pton(AF_INET, SERVER_IP, &server_addr.sin_addr) <= 0) {
			 perror("Invalid address");
			 close(sock);
			 sleep(1);
			 attempt++;
			 continue;
		 }

		 // Connect to server
		 if (connect(sock, (struct sockaddr *)&server_addr, sizeof(server_addr)) < 0) {
			 perror("Connection failed");
			 close(sock);
			 sleep(1);
			 attempt++;
			 continue;
		 }

		 // Read loop - retry whole connection if it fails
		 ssize_t bytes_read = read(sock, buffer, sizeof(buffer) - 1);
		 if (bytes_read < 0) {
			 perror("Read failed");
			 close(sock);
			 sleep(1);
			 attempt++;
			 continue; // Restart connection from the top
		 }

		 buffer[bytes_read] = '\0';  // Null-terminate
		 close(sock);
		 break;  // Success!
	 }

	 size_t len = strlen(buffer);
	 char* result = (char*)malloc(len + 1);
	 if (!result) {
		 perror("Memory allocation failed");
		 exit(EXIT_FAILURE);
	 }
	 memcpy(result, buffer, len);
	 result[len] = '\0';
	 //printf("DEBUG: allocated %zu bytes for result at %p\n", len + 1, result);


	 //printf("Received string length: %zu\n", strlen(buffer));
	 //printf("DEBUG: buffer='%s'\n", buffer);
	 //printf("DEBUG: buffer length = %zu\n", strlen(buffer));

	 //strcpy(result, buffer);
	 return result;  // Caller must free this
 }



 // Function to split a CSV string into individual values
 void parseCSV(const char* input, char** value1, char** value2, char** value3, char** value4, char** value5, char** value6, char** value7, char** value8, char** value9, char** value10, char** value11) {
	 // Copy input string to avoid modifying the original
	 //printf("I am Here");
	 char* temp = strdup(input);
	 if (!temp) {
		 perror("Memory allocation failed");
		 exit(EXIT_FAILURE);
	 }

	 // Extract values using strtok
	 char* token = strtok(temp, ",");
	 int index = 0;
	 char** values[] = {value1, value2, value3, value4, value5, value6, value7, value8, value9, value10, value11}; // Array of pointers

	 while (token != NULL && index < 11) { //If you add more values to parseCSV, this section needs to be updated from 10 to the number of values being passed
		 *values[index] = strdup(token);  // Allocate and copy each value
		 if (!(*values[index])) {
			 perror("Memory allocation failed");
			 exit(EXIT_FAILURE);
		 }
		 token = strtok(NULL, ",");
		 index++;
	 }

	 // A short/malformed response must not leave caller pointers uninitialized.
	 while (index < 11) {
		 *values[index] = strdup("");
		 if (!(*values[index])) {
			 perror("Memory allocation failed");
			 exit(EXIT_FAILURE);
		 }
		 index++;
	 }

	 free(temp); // Free temporary string copy
 }
 void eve_display(void)
 {

	 //Print the value from the socket connection:
	 //printf("Beginning of eve_display");
	 //readFromZMonSocket();
	 uint32_t counter = 0;
	 uint8_t key;

	 //path for reading temperature inputs from the hwmon
	 const char* file_path1 = {"/sys/class/hwmon/hwmon0/temp1_input"};
	 const char* file_path2 = {"/sys/class/hwmon/hwmon0/temp2_input"};
	 const char* file_path3 = {"/sys/class/hwmon/hwmon0/temp3_input"};
	 int temperature1 = 0;
	 int temperature2 = 0;
	 int temperature3 = 0;
	 int avgTemp;

	 //IP Address -----------
	 char iface0[MAX_iface_NAME];
	 char iface1[MAX_iface_NAME];
	 // Define the interface name you want to check
	 strcpy(iface0, "eth0");
	 strcpy(iface1, "eth1");
	 char* ip0 = get_ip_address(iface0);
	 char* ip1 = get_ip_address(iface1);
	 if (!ip0) {
		 ip0 = "000.000.000.000";
	 }
	 if (!ip1) {
		 ip1 = "000.000.000.000";
	 }


	 //-----------------------

	 do {
		  //Read the Chassis Temperature Sensor
		 float chassisTemperature = readTemperature();
		 char chassisTemperatureBuf[32];
		 snprintf(chassisTemperatureBuf, sizeof(chassisTemperatureBuf), "Chassis = %.1fC", chassisTemperature);

		 char bufferTime[64];
		 readSystemTime(bufferTime, sizeof(bufferTime));
		 printf("\nCurrent local time: %s\n", bufferTime);

		 //readFromZMonSocket();
		 char* socketData = readFromZMonSocket2();
		 // Pointers to hold extracted values
		 char *Res, *Cap, *Th, *Mag, *Phase, *Phase2, *PhaseRX, *I, *Q, *latched, *latchedStamp;

		 printf("before socketData\n");
		 if (!socketData) {
			 fprintf(stderr, "socketData is NULL!\n");
			 exit(EXIT_FAILURE);
		 }

		 printf("socketData = %s\n", socketData);
		 // Call the function
		 //printf("Call the function\n");
		 parseCSV(socketData, &Res, &Cap, &Th, &Mag, &Phase, &Phase2, &PhaseRX, &I, &Q, &latched, &latchedStamp);
		 char *allocatedValues[] = {
			 Res, Cap, Th, Mag, Phase, Phase2, PhaseRX, I, Q, latched, latchedStamp
		 };
		 if (strncmp(Res, "Res=", 4) == 0) {
			 Res += 4;  // Skip "Res="
		 }
		 if (strncmp(Th, "Th=", 3) == 0) {
			 Th += 3;  // Skip "Th="
		 }
		 if (strncmp(latched, "latched=", 8) == 0) {
			latched += 8;  // Skip "latched="
		}
		//printf("latchedNew=%s\n", latched);
		printf("PhaseRX = %s\n", PhaseRX);
		printf("PhaseRX = %s\n", Phase);

		 if (strncmp(latchedStamp, "LatchStamp=", 11) == 0) {
			latchedStamp += 11;  // Skip "LatchStamp="
		}

		 float fRes = atof(Res);
		 float fTh  = atof(Th);
		 float flatched = atof(latched);

		 printf("flatched = %f\n", flatched);

		 if (fRes <= fTh) {
			 EVE_LIB_BeginCoProList();
			 EVE_CMD_DLSTART();
			 EVE_CLEAR_COLOR_RGB(175, 0, 0); //set the color of the display in (R, G, B)
			 EVE_CLEAR(1,1,1); //clears the display and applies the new color
			 EVE_COLOR_RGB(0, 0, 0);
			 //printf("In Red: Res = '%s', fRes = %f, Th = '%s', fTh = %f\n", Res, fRes, Th, fTh);
		 }
		 else{
			 EVE_LIB_BeginCoProList();
			 EVE_CMD_DLSTART();
			 EVE_CLEAR_COLOR_RGB(0, 175, 0); //set the color of the display in (R, G, B)
			 EVE_CLEAR(1,1,1); //clears the display and applies the new color
			 EVE_COLOR_RGB(0, 0, 0);
			 //printf("In Green: Res = '%s', fRes = %f, Th = '%s', fTh = %f\n", Res, fRes, Th, fTh);
		 }

		 // Print extracted values
		 //printf("Extracted Values:\n");
		 //printf("1: %s\n2: %s\n3: %s\n4: %s\n5: %s\n", RES, TH, Mag, I, Q);

		 FILE* file1 = fopen(file_path1, "r");
		 if (file1 != NULL) {
					 // Read the temperature value from the file
				 if (fscanf(file1, "%d", &temperature1) != 1) {
					 temperature1 = 0;
				 }

					 // Close the file
				 fclose(file1);

					 // Convert to degrees Celsius
				 temperature1 /= 1000;
			 //printf("Temperature 1: %d degrees Celsius\n", temperature1);
			 }
			 else {
					 // Handle file opening error
				 printf("Error opening file: %s\n", file_path1);

		 }
		 FILE* file2 = fopen(file_path2, "r");
		 if (file2 != NULL) {
			 if (fscanf(file2, "%d", &temperature2) != 1) {
				 temperature2 = 0;
			 }
			 fclose(file2);
			 temperature2 /= 1000;
			 //printf("Temperature 2: %d degress Celsius\n", temperature2);
		 }
		 else {
			 printf("Error opening file: %s\n", file_path2);
		 }
		 FILE* file3 = fopen(file_path3, "r");
		 if (file3 != NULL) {
			 if (fscanf(file3, "%d", &temperature3) != 1) {
				 temperature3 = 0;
			 }
			 fclose(file3);
			 temperature3 /= 1000;
			 //printf("Temperature 3: %d degrees Celsius\n", temperature3);
		 }
		 else {
			 printf("Error opening file: %s\n", file_path3);
		 }

		 avgTemp = (temperature1 + temperature2 + temperature3) / 3;
		 //printf("\nAvgTemp= %d\n", avgTemp);
		 char temperatureBuffer[32]; //buffer to store char version of int to char for printing with the display
		 snprintf(temperatureBuffer, sizeof(temperatureBuffer), "CPU = %dC", avgTemp);

		 // Comment this line if the counter needs to increment continuously.
		 // Uncomment and it will increment by one each press.
		 //while (eve_read_tag(&key) != 0);



		 EVE_BEGIN(EVE_BEGIN_BITMAPS);
 #if (defined EVE2_ENABLE || defined EVE3_ENABLE || defined EVE4_ENABLE)
		 // Set origin on canvas using EVE_VERTEX_TRANSLATE.
		 //EVE_VERTEX_TRANSLATE_X(((EVE_DISP_WIDTH/2)-(eve_img_bridgetek_logo_width/2)) * 16);
		 //EVE_VERTEX2II(0, 0, BITMAP_BRIDGETEK_LOGO, 0);
		 //EVE_VERTEX_TRANSLATE_X(0);
 #else
		 // Place directly on canvas EVE_VERTEX_TRANSLATE not available.
		 //EVE_VERTEX2II((EVE_DISP_WIDTH/2)-(eve_img_bridgetek_logo_width/2), 0, BITMAP_BRIDGETEK_LOGO, 0);
 #endif

		 //Adding a GAUGE to the display EVE_CMD_GAUGE(x,y,radius,options, major, minor, val, range)
		 //EVE_CMD_GAUGE(90, 377, 94, 0, 4, 8, counter, 10000);
		 //EVE_CMD_GAUGE(292, 377, 94, 0, 10, 5, temperature1, 100);
		 //EVE_CMD_GAUGE(498, 377, 94, 0, 10, 5, temperature2, 100);
		 //EVE_CMD_GAUGE(693, 377, 94, 0, 10, 5, temperature3, 100);
		 //EVE_CMD_BUTTON(18, 117, 120, 36, 27, 0, "RESET");
		 //EVE_CMD_TEXT(262, 199, 31, 0, "TEMPERATURE MONITOR");
		 EVE_CMD_TEXT(355,  50, 31, 0, "ETH0:");
		 EVE_CMD_TEXT(475, 50, 31, 0, ip0);
		 EVE_CMD_TEXT(355,  100, 31, 0, "ETH1: ");
		 EVE_CMD_TEXT(475, 100, 31, 0, ip1);
		 //EVE_CMD_TEXT(EVE_DISP_WIDTH/2, 4, 31, EVE_OPT_CENTERX, "GIZMo-Kria"); // Displays the GIZMo-Kria Text at the center 'x' of the display
		 EVE_CMD_TEXT(675, 4, 31, EVE_OPT_CENTERX, "GIZMo-Kria"); // Displays the GIZMo-Kria Text at the center 'x' of the display
		 //EVE_CMD_TEXT(7, 50, 31, 0, "Res:");
		 //EVE_CMD_TEXT(100, 50, 28, 0, readFromZMonSocket2()); //set font size from 28 to 31 for maximum font
		 EVE_CMD_TEXT(7,  4, 31, 0, "Res=");
		 EVE_CMD_TEXT(100,  4, 31, 0, Res);
		 EVE_CMD_TEXT(7, 50, 31, 0, "Th=");
		 EVE_CMD_TEXT(80, 50, 31, 0, Th);
		 EVE_CMD_TEXT(7, 100, 31, 0, Mag);
		 EVE_CMD_TEXT(7, 150, 31, 0, Phase);
		 EVE_CMD_TEXT(455, 150, 31, 0, Cap);
		 EVE_CMD_TEXT(7, 200, 31, 0, Phase2);
		 EVE_CMD_TEXT(455, 200, 31, 0, PhaseRX);
		 EVE_CMD_TEXT(7, 250, 31, 0, I);
		 EVE_CMD_TEXT(7, 300, 31, 0, Q);
		 EVE_CMD_TEXT(7,  350, 31, 0, "System Time: ");
		 EVE_CMD_TEXT(400, 350, 31, 0, bufferTime);
		 EVE_CMD_TEXT(7,  400, 31, 0, "System Latched at: ");
		if (flatched == 1){
			EVE_CMD_TEXT(400, 400, 31, 0, latchedStamp);
		 }
		 EVE_CMD_TEXT(602, 250, 31, 0, temperatureBuffer);
		 EVE_CMD_TEXT(500, 300, 31, 0, chassisTemperatureBuf);


		 EVE_TAG(100);

		 //EVE_COLOR_RGB(255, 0, 0);

		 EVE_BEGIN(EVE_BEGIN_BITMAPS);

 /* #if (defined EVE2_ENABLE || defined EVE3_ENABLE || defined EVE4_ENABLE)
		 EVE_VERTEX_TRANSLATE_Y((EVE_DISP_HEIGHT / 2) * 16);
		 for (i = 0; i < 5; i++)
		 {
			 EVE_VERTEX_TRANSLATE_X((((EVE_DISP_WIDTH - (font0_hdr->FontWidthInPixels * 5)) / 2) - (font0_hdr->FontWidthInPixels) + (font0_hdr->FontWidthInPixels * (5 - i))) * 16);
			 EVE_VERTEX2II(0, 0, FONT_CUSTOM, ((counter / units) % 10)+1); //+1 as in the converted font the number '0' is in position 1 in the font table
			 units *= 10;
		 }
 #else
		 for (i = 0; i < 5; i++)
		 {
			 EVE_VERTEX2II((((EVE_DISP_WIDTH - (font0_hdr->FontWidthInPixels * 5)) / 2) - (font0_hdr->FontWidthInPixels) + (font0_hdr->FontWidthInPixels * (5 - i))),
					 (EVE_DISP_HEIGHT / 2), FONT_CUSTOM, ((counter / units) % 10)+1); //+1 as in the converted font the number '0' is in position 1 in the font table
			 units *= 10;
		 }
 #endif */

		 EVE_DISPLAY();
		 EVE_CMD_SWAP();
		 EVE_LIB_EndCoProList();
		 EVE_LIB_AwaitCoProEmpty();

		 //while (eve_read_tag(&key) == 0);
		 eve_read_tag(&key);
		 if (key == 100)
		 {
			 counter++;
			 if (counter == 100000)
			 {
				 counter = 0;
			 }
		 }
		 free(socketData);
		 for (size_t valueIndex = 0;
		      valueIndex < sizeof(allocatedValues) / sizeof(allocatedValues[0]);
		      valueIndex++) {
			 free(allocatedValues[valueIndex]);
		 }
	 } while (1);
 }

 void eve_example(void)
 {
	 uint32_t font_end;

	 // Initialise the display
	 EVE_Init();

	 // Calibrate the display
	 //eve_calibrate();

	 // Load fonts and images
	 font_end = eve_init_fonts();

	 eve_load_images(font_end);

	 // Start example code
	 eve_display();
 }

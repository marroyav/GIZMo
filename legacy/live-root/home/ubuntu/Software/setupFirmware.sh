/home/ubuntu/Software/peekpoke -b 0xa0000000 w.l 0x4 0x0 			#Sets the GPIO to output
/home/ubuntu/Software/peekpoke -b 0xa0000000 w.l 0x0 0x03028FFf		#Enables the DAC Firmware state machine
#sleep 1
/home/ubuntu/Software/peekpoke -b 0xa0060000 w.l 0x0 0x1   # take relay controllers out of reset
/home/ubuntu/Software/peekpoke -b 0xa0044000 w.l 0x0 0x1  # enables the firmware to starting sending data out
#/home/ubuntu/Software/peekpoke -b 0xa0044000 w.l 0x4 0xaaaaaaaa  #   turning off all relays
#sleep 1
#/home/ubuntu/Documents/GIZMo-Kria/peekpoke -b 0xa0044000 w.l 0x0 0x0      # disabling the firmware for sending relay data  if you need to change the state of the relays

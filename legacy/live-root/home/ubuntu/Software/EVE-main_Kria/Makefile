PLATFORM?=RASPBERRYPI

CC = gcc
CFLAGS = -c -Wall -g -Os -DPLATFORM_RASPBERRYPI -DUSE_LINUX_SPI_DEV -Ilib/eve/include -Iexample
LD = $(CC)
LDFLAGS = 

TARGET = $(shell basename $(CURDIR))

OBJECTS = $(patsubst %.c, %.o, $(shell find . -name "*.c"))

all: $(TARGET)

$(TARGET): $(OBJECTS)
	$(LD) -o $@ $^ $(LDFLAGS)

%.o: %.c
	$(CC) $(CFLAGS) $^ -o $@

clean:
	rm $(TARGET) $(OBJECTS)

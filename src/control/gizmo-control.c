#define _GNU_SOURCE

#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define SYSTEMD_SOCKET_FD 3
#define REQUEST_SIZE 256

static volatile sig_atomic_t stopping = 0;

static void handle_signal(int signal_number)
{
    (void)signal_number;
    stopping = 1;
}

static int write_response(int fd, const char *response)
{
    size_t remaining = strlen(response);
    const char *cursor = response;

    while (remaining > 0) {
        ssize_t written = write(fd, cursor, remaining);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        cursor += written;
        remaining -= (size_t)written;
    }
    return 0;
}

static int run_systemctl_restart(void)
{
    pid_t child = fork();
    if (child < 0) {
        return -1;
    }
    if (child == 0) {
        execl("/usr/bin/systemctl", "systemctl", "try-restart",
              "gizmo-zmon.service", (char *)NULL);
        execl("/bin/systemctl", "systemctl", "try-restart",
              "gizmo-zmon.service", (char *)NULL);
        _exit(127);
    }

    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) {
            return -1;
        }
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}

static int set_system_time(const char *value)
{
    char *end = NULL;
    errno = 0;
    long long seconds = strtoll(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0') {
        return -1;
    }

    /* Reject obviously accidental values while covering 2000-01-01 through 2100-01-01. */
    if (seconds < 946684800LL || seconds > 4102444800LL) {
        return -1;
    }

    struct timespec requested = {
        .tv_sec = (time_t)seconds,
        .tv_nsec = 0,
    };
    return clock_settime(CLOCK_REALTIME, &requested);
}

static void handle_client(int client_fd)
{
    char request[REQUEST_SIZE];
    ssize_t count;

    do {
        count = read(client_fd, request, sizeof(request) - 1);
    } while (count < 0 && errno == EINTR);

    if (count <= 0) {
        return;
    }

    request[count] = '\0';
    request[strcspn(request, "\r\n")] = '\0';

    if (strcmp(request, "ping") == 0) {
        (void)write_response(client_fd, "OK pong\n");
        return;
    }

    if (strcmp(request, "restart-zmon") == 0) {
        if (run_systemctl_restart() == 0) {
            (void)write_response(client_fd, "OK zmon restart requested\n");
        } else {
            (void)write_response(client_fd, "ERR unable to restart zmon\n");
        }
        return;
    }

    static const char prefix[] = "set-time ";
    if (strncmp(request, prefix, sizeof(prefix) - 1) == 0) {
        if (set_system_time(request + sizeof(prefix) - 1) == 0) {
            (void)write_response(client_fd, "OK system time updated\n");
        } else {
            (void)write_response(client_fd, "ERR invalid time or clock_settime failed\n");
        }
        return;
    }

    (void)write_response(client_fd, "ERR unsupported control request\n");
}

static int inherited_socket(void)
{
    const char *listen_pid = getenv("LISTEN_PID");
    const char *listen_fds = getenv("LISTEN_FDS");
    if (listen_pid == NULL || listen_fds == NULL) {
        return -1;
    }
    if ((pid_t)strtol(listen_pid, NULL, 10) != getpid() ||
        strtol(listen_fds, NULL, 10) != 1) {
        return -1;
    }
    return SYSTEMD_SOCKET_FD;
}

int main(void)
{
    int server_fd = inherited_socket();
    if (server_fd < 0) {
        fprintf(stderr, "gizmo-control must be started by gizmo-control.socket\n");
        return EXIT_FAILURE;
    }

    struct sigaction action = {
        .sa_handler = handle_signal,
    };
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);

    fprintf(stderr, "gizmo-control ready on its systemd socket\n");
    while (!stopping) {
        int client_fd = accept4(server_fd, NULL, NULL, SOCK_CLOEXEC);
        if (client_fd < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("accept4");
            return EXIT_FAILURE;
        }
        handle_client(client_fd);
        close(client_fd);
    }

    return EXIT_SUCCESS;
}

/* Keep a native responsible process alive so macOS can track LAN consent. */
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <limits.h>
#include <errno.h>

int main(int argc, char **argv) {
    char executable[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size)) return 1;
    char *slash = strrchr(executable, '/');
    if (!slash) return 1;
    *slash = '\0';
    char script[PATH_MAX];
    if (snprintf(script, sizeof(script), "%s/../Resources/launch.sh", executable) >= (int)sizeof(script)) return 1;
    char **arguments = calloc(argc + 2, sizeof(char *));
    if (!arguments) return 1;
    arguments[0] = "/bin/bash";
    arguments[1] = script;
    for (int i = 1; i < argc; i++) arguments[i + 1] = argv[i];
    pid_t pid = fork();
    if (pid == 0) {
        execv(arguments[0], arguments);
        perror("Sensorius launcher");
        _exit(1);
    }
    free(arguments);
    if (pid < 0) return 1;
    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}

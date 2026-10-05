/* Linux launcher for the pinned worker, not an interpreter or tool bridge.
 * Only the binding's literal "subprocess" argument is accepted. The mount
 * namespace has the native worker and runtime libraries, never Bot state.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

static void limit(int resource, rlim_t value) {
    struct rlimit bound = {value, value};
    if (setrlimit(resource, &bound) != 0) {
        perror("monty resource limit");
        exit(125);
    }
}

int main(int argc, char **argv) {
    if (argc != 2 || strcmp(argv[1], "subprocess") != 0) {
        fputs("monty launcher: unsupported command\n", stderr);
        return 125;
    }
    /* Bounds include native parser/dump allocations, outside VM accounting.
     * The Host adds a wall watchdog and a global process admission limit.
     */
    limit(RLIMIT_AS, 512UL * 1024 * 1024);
    limit(RLIMIT_CPU, 30);
    limit(RLIMIT_FSIZE, 4UL * 1024 * 1024);
    limit(RLIMIT_NOFILE, 32);
    limit(RLIMIT_CORE, 0);
    if (clearenv() != 0) return 125;
    char *const command[] = {
        "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
        "--uid", "65534", "--gid", "65534", "--cap-drop", "ALL", "--clearenv",
        "--ro-bind", "/opt/yuki-monty/monty", "/worker/monty",
        "--ro-bind", "/usr/lib", "/usr/lib",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--dir", "/etc", "--chdir", "/tmp",
        "/worker/monty", "subprocess", NULL
    };
    execv(command[0], command);
    perror("monty isolated launcher");
    return 125;
}

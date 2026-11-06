/*
 * Shell-free bootstrap for the Go and Rust runtime families.
 *
 * The zip layer on provided.al2023 used to ship a `#!/bin/sh` bootstrap that
 * exec'd /opt/<runtime>/bin/$_HANDLER. A distroless container has no /bin/sh, so
 * build-runtime compiles this launcher instead: one statically linked binary that
 * runs unchanged on Amazon Linux 2023 and on gcr.io/distroless/cc-debian12.
 *
 * It does exactly what the shell did: apply the Lambda filesystem contract and
 * exec the handler named by _HANDLER, without spawning an interpreter.
 *
 * Compiled per runtime with -DRUNTIME_DIR_NAME="go" / "rust".
 */

#define _GNU_SOURCE

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef RUNTIME_DIR_NAME
#error "RUNTIME_DIR_NAME must be defined at compile time"
#endif

#ifndef LAMBDA_RUNTIME_ROOT
#define LAMBDA_RUNTIME_ROOT "/opt"
#endif

int main(int argc, char **argv) {
    /* Lambda sets _HANDLER; a container image may instead pass the handler as
     * CMD (`CMD ["app.handler"]`), which reaches us as the first argument after
     * the entrypoint. Accepting both keeps one payload usable as a zip layer and
     * as a container image, and exporting it keeps the handler code's own
     * getenv("_HANDLER") working either way. */
    const char *handler = getenv("_HANDLER");
    if ((handler == NULL || handler[0] == '\0') && argc > 1) {
        handler = argv[1];
        setenv("_HANDLER", handler, 1);
    }
    if (handler == NULL || handler[0] == '\0') {
        fprintf(stderr, "bootstrap: _HANDLER is not set and no handler was given as an argument\n");
        return 127;
    }

    /* /tmp is the only writable path in a Lambda container image. */
    setenv("HOME", "/tmp", 0);
    setenv("TMPDIR", "/tmp", 0);

    char path[4096];
    int written = snprintf(path, sizeof(path), "%s/%s/bin/%s",
                           LAMBDA_RUNTIME_ROOT, RUNTIME_DIR_NAME, handler);
    if (written < 0 || (size_t)written >= sizeof(path)) {
        fprintf(stderr, "bootstrap: handler path is too long\n");
        return 127;
    }

    argv[0] = path;
    execv(path, argv);

    fprintf(stderr, "bootstrap: cannot execute %s: %s\n", path, strerror(errno));
    return 127;
}

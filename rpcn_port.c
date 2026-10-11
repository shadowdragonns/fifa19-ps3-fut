/* Per-process RPCN and FIFA gameplay UDP bind ports. Linux only.
 * Native gameplay getsockname sees the physical port for peer advertisement.
 * RPCN's separate logical PS3 port remains managed by RPCS3 itself. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>

static int (*real_bind)(int, const struct sockaddr *, socklen_t);
static pthread_once_t bind_once = PTHREAD_ONCE_INIT;

static void resolve_bind(void)
{
    real_bind = dlsym(RTLD_NEXT, "bind");
}

int bind(int fd, const struct sockaddr *address, socklen_t length)
{
    pthread_once(&bind_once, resolve_bind);
    if (!real_bind) { errno = ENOSYS; return -1; }
    const char *setting = getenv("FIFA19_RPCN_PORT");
    int type = 0;
    socklen_t type_length = sizeof(type);
    if (!setting || !address || length > sizeof(struct sockaddr_storage)
        || length < sizeof(sa_family_t)
        || getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &type_length) || type != SOCK_DGRAM)
        return real_bind(fd, address, length);
    char *end;
    long port = strtol(setting, &end, 10);
    if (!*setting || *end || port < 1024 || port > 65535)
        return real_bind(fd, address, length);
    struct sockaddr_storage copy;
    memset(&copy, 0, sizeof(copy));
    memcpy(&copy, address, length);
    unsigned short *field = NULL;
    if (address->sa_family == AF_INET && length >= sizeof(struct sockaddr_in))
        field = &((struct sockaddr_in *)&copy)->sin_port;
    else if (address->sa_family == AF_INET6 && length >= sizeof(struct sockaddr_in6))
        field = &((struct sockaddr_in6 *)&copy)->sin6_port;
    if (!field)
        return real_bind(fd, address, length);
    unsigned short original = ntohs(*field);
    long mapped = port;
    if (original == 3659) mapped += 1;
    else if (original == 9999) mapped += 2;
    else if (original != 3658) return real_bind(fd, address, length);
    if (mapped > 65535) return real_bind(fd, address, length);
    *field = htons((unsigned short)mapped);
    int result = real_bind(fd, (struct sockaddr *)&copy, length);
    int saved_errno = errno;
    if (!result) fprintf(stderr, "FUTCLIENT: %s host UDP port %u -> %ld\n",
                        original == 3658 ? "RPCN" : "FIFA", original, mapped);
    errno = saved_errno;
    return result;
}

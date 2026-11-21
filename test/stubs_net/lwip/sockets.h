#pragma once
#include <stddef.h>
#include <stdint.h>

/* Offline double for the slice of the BSD/lwip socket API the beacon uses.
 * Shapes match the real header's usage in net.c; no packet ever leaves the
 * process, and the tests count sendto() calls instead of reading a wire. */
#define AF_INET         2
#define SOCK_DGRAM      2
#define IPPROTO_UDP     17
#define SOL_SOCKET      1
#define SO_BROADCAST    0x0020
#define INADDR_BROADCAST 0xFFFFFFFFu

typedef uint32_t in_addr_t;
struct in_addr { in_addr_t s_addr; };
typedef struct in_addr in_addr;

struct sockaddr {
    uint16_t sa_family;
    char sa_data[14];
};

struct sockaddr_in {
    uint16_t sin_family;
    uint16_t sin_port;
    struct in_addr sin_addr;
    char sin_zero[8];
};

/* Byte swap, not the host macro: the tests only ever compare round-trips. */
#define htons(x) ((uint16_t)(((uint16_t)(x) << 8) | ((uint16_t)(x) >> 8)))
#define ntohs(x) htons(x)

int socket(int domain, int type, int protocol);
int setsockopt(int fd, int level, int option, const void *value, int length);
int sendto(int fd, const void *data, size_t length, int flags,
           const struct sockaddr *destination, unsigned destination_length);

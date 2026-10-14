#include <jni.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>
#include <poll.h>
#include <time.h>
#include <pthread.h>
#include <sys/eventfd.h>
#include <stdint.h>
#include <unistd.h>

static long long monotonic_ms(void) {
    struct timespec time;
    clock_gettime(CLOCK_MONOTONIC, &time);
    return (long long)time.tv_sec * 1000 + time.tv_nsec / 1000000;
}

/* Video IN and OUT share a completion queue. Either caller can reap a request;
 * usercontext routes it back to its owner, under the same completion lock.
 * The Android mux path uses only synchronous bulk ioctls on this fd. */
static pthread_mutex_t completions = PTHREAD_MUTEX_INITIALIZER;
struct pending_transfer { int done; int wake_fd; struct usbdevfs_urb urb; };

static int video_transfer(int fd, int endpoint, void *bytes, int length, int timeout) {
    struct pending_transfer request = {.urb = {
        .type = USBDEVFS_URB_TYPE_BULK, .endpoint = (unsigned char)endpoint,
        .buffer = bytes, .buffer_length = length
    }};
    request.urb.usercontext = &request;
    request.wake_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (request.wake_fd < 0) return -errno;
    pthread_mutex_lock(&completions);
    int submitted = ioctl(fd, USBDEVFS_SUBMITURB, &request.urb);
    int submit_error = errno;
    pthread_mutex_unlock(&completions);
    if (submitted < 0) { close(request.wake_fd); return -submit_error; }
    long long deadline = monotonic_ms() + timeout;
    int timed_out = 0;
    for (;;) {
        void *completed = NULL;
        pthread_mutex_lock(&completions);
        int reaped;
        do {
            reaped = ioctl(fd, USBDEVFS_REAPURBNDELAY, &completed);
            if (reaped == 0) {
                struct usbdevfs_urb *urb = completed;
                struct pending_transfer *owner = urb->usercontext;
                owner->done = 1;
                // Reaping removes readiness from the shared USB fd. Wake the
                // actual owner as well, or it can sleep for its entire USB
                // timeout when the other direction consumed its completion.
                uint64_t wake = 1;
                (void)write(owner->wake_fd, &wake, sizeof(wake));
            }
        } while (reaped == 0);
        int reap_error = errno;
        if (!request.done && reap_error != EAGAIN && reap_error != EINTR) {
            // ENODEV/EBADF means the kernel has removed/cancelled this fd's
            // requests. Normal shutdown waits for callers before closing fd.
            ioctl(fd, USBDEVFS_DISCARDURB, &request.urb);
            request.urb.status = -reap_error;
            request.done = 1;
        }
        if (request.done) {
            int result = request.urb.status;
            if (result == 0) result = request.urb.actual_length;
            else if (timed_out && (result == -ECONNRESET || result == -ENOENT)) {
                // Preserve an IN prefix received before cancellation. OUT
                // cancellation remains an error: input writes are not replayed.
                result = (endpoint & 128) && request.urb.actual_length > 0
                    ? request.urb.actual_length : -ETIMEDOUT;
            }
            pthread_mutex_unlock(&completions);
            close(request.wake_fd);
            return result;
        }
        int remaining = (int)(deadline - monotonic_ms());
        if (remaining <= 0 && !timed_out) {
            timed_out = 1;
            ioctl(fd, USBDEVFS_DISCARDURB, &request.urb);
        }
        pthread_mutex_unlock(&completions);
        struct pollfd wait[2] = {
            {.fd = fd, .events = POLLOUT},
            {.fd = request.wake_fd, .events = POLLIN}
        };
        // After discard, wait for completion before releasing stack/JNI data.
        poll(wait, 2, timed_out ? 100 : remaining);
    }
}

JNIEXPORT jint JNICALL
Java_com_rayren_imirrorlite_UsbIo_clearHalt(JNIEnv *env, jclass cls, jint fd, jint endpoint) {
    (void)env; (void)cls;
    if (fd < 0 || endpoint < 0 || endpoint > 255) return -EINVAL;
    unsigned int ep = (unsigned int)endpoint;
    int result = ioctl(fd, USBDEVFS_CLEAR_HALT, &ep);
    return result < 0 ? -errno : result;
}

/* The descriptor is opened and permission-checked by Android UsbManager.
 * Preserve errno: Android bulkTransfer collapses timeout and stall into -1. */
JNIEXPORT jint JNICALL
Java_com_rayren_imirrorlite_UsbIo_transfer(JNIEnv *env, jclass cls, jint fd,
                                         jint endpoint, jbyteArray data, jint timeout) {
    (void)cls;
    if (fd < 0 || !data || timeout < 1) return -EINVAL;
    jsize length = (*env)->GetArrayLength(env, data);
    jbyte *bytes = (*env)->GetByteArrayElements(env, data, NULL);
    if (!bytes) return -ENOMEM;
    int result = video_transfer(fd, endpoint, bytes, length, timeout);
    (*env)->ReleaseByteArrayElements(env, data, bytes,
                                    result >= 0 && (endpoint & 128) ? 0 : JNI_ABORT);
    return result;
}

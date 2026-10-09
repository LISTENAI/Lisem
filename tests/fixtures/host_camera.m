/* SPDX-License-Identifier: MIT */
#define LISEM_CAMERA_TEST
#include "../../native/camera/avfoundation.m"
#include <assert.h>
#include <mach/mach_time.h>

static void test_clock(void)
{
    mach_timebase_info_data_t timebase;
    assert(mach_timebase_info(&timebase) == KERN_SUCCESS);
    uint64_t before = mach_absolute_time();
    uint64_t timestamp = host_nanoseconds();
    uint64_t after = mach_absolute_time();
    before = (uint64_t)((__uint128_t)before * timebase.numer / timebase.denom);
    after = (uint64_t)((__uint128_t)after * timebase.numer / timebase.denom);
    assert(timestamp >= before && timestamp <= after);
}

static void test_frame(void)
{
    uint8_t pixels[32] = {
        3, 2, 1, 255, 6, 5, 4, 255, 99, 99, 99, 99, 99, 99, 99, 99,
        9, 8, 7, 255, 12, 11, 10, 255, 99, 99, 99, 99, 99, 99, 99, 99,
    };
    CVPixelBufferRef buffer = NULL;
    assert(CVPixelBufferCreateWithBytes(NULL, 2, 2, kCVPixelFormatType_32BGRA,
        pixels, 16, NULL, NULL, NULL, &buffer) == kCVReturnSuccess);
    NSData *frame = rgb_frame(buffer, UINT64_C(0x0102030405060708));
    assert(frame.length == 44);
    assert(fwrite(frame.bytes, 1, frame.length, stdout) == frame.length);
    CVPixelBufferRelease(buffer);
    assert(CVPixelBufferCreate(NULL, 2, 2, kCVPixelFormatType_32ARGB,
        NULL, &buffer) == kCVReturnSuccess);
    assert(rgb_frame(buffer, 0) == nil);
    CVPixelBufferRelease(buffer);
    assert(CVPixelBufferCreate(NULL, 1921, 2, kCVPixelFormatType_32BGRA,
        NULL, &buffer) == kCVReturnSuccess);
    assert(rgb_frame(buffer, 0) == nil);
    CVPixelBufferRelease(buffer);
}

static void test_latest(void)
{
    CameraSink *sink = [[CameraSink alloc] init];
    NSMutableData *old = [NSMutableData dataWithLength:35];
    NSMutableData *new = [NSMutableData dataWithLength:35];
    memcpy((uint8_t *)new.mutableBytes + 32, "new", 3);
    [sink offer:old];
    [sink offer:new];
    [sink recordDrop];
    NSData *selected = [sink take];
    assert(selected.length == 35);
    assert(memcmp((const uint8_t *)selected.bytes + 32, "new", 3) == 0);
    assert(((const uint8_t *)selected.bytes)[20] == 2);
    [sink offer:[NSMutableData dataWithLength:32]];
    selected = [sink take];
    assert(((const uint8_t *)selected.bytes)[20] == 2); /* Cumulative, not per frame. */
    [sink offer:[NSMutableData dataWithLength:32]];
    [sink stop];
    assert([sink take] == nil);
    assert([sink stopped]);
}

static void test_pipe(BOOL cancel, BOOL broken)
{
    int descriptors[2];
    assert(pipe(descriptors) == 0);
    assert(fcntl(descriptors[1], F_SETFL, O_NONBLOCK) == 0);
    CameraSink *sink = [[CameraSink alloc] init];
    NSMutableData *frame = [NSMutableData dataWithLength:262144];
    uint8_t *bytes = frame.mutableBytes;
    for (size_t i = 0; i < frame.length; i++) {
        bytes[i] = i % 251;
    }
    memset(bytes + 20, 0, 4);
    [sink offer:frame];
    if (broken) {
        close(descriptors[0]);
    }
    int output = descriptors[1];
    dispatch_group_t writer = dispatch_group_create();
    dispatch_group_async(writer, dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        [sink writeTo:output];
    });
    if (cancel) {
        /* Let the writer fill the pipe, then cancellation must wake it. */
        usleep(150000);
        [sink stop];
    } else if (!broken) {
        NSMutableData *received = [NSMutableData data];
        uint8_t chunk[113];
        while (received.length < frame.length) {
            ssize_t count = read(descriptors[0], chunk, sizeof(chunk));
            assert(count > 0);
            [received appendBytes:chunk length:count];
        }
        assert([received isEqualToData:frame]);
        [sink stop];
    }
    assert(dispatch_group_wait(writer, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
    assert((sink.failure != nil) == broken);
    if (!broken) {
        close(descriptors[0]);
    }
    close(descriptors[1]);
}

int main(int argc, const char **argv)
{
    @autoreleasepool {
        signal(SIGPIPE, SIG_IGN);
        signal(SIGTERM, interrupt_capture);
        assert(argc == 2);
        if (strcmp(argv[1], "clock") == 0) {
            test_clock();
        } else if (strcmp(argv[1], "frame") == 0) {
            test_frame();
        } else if (strcmp(argv[1], "latest") == 0) {
            test_latest();
        } else if (strcmp(argv[1], "pipe") == 0) {
            test_pipe(NO, NO);
        } else if (strcmp(argv[1], "cancel") == 0) {
            test_pipe(YES, NO);
        } else if (strcmp(argv[1], "broken") == 0) {
            test_pipe(NO, YES);
        } else if (strcmp(argv[1], "watch") == 0) {
            watch_parent(NO);
            puts("ready");
            fflush(stdout);
            while (1) pause();
        } else {
            assert(0);
        }
    }
    return 0;
}

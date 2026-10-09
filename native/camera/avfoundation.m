/* SPDX-License-Identifier: MIT */
#import <AVFoundation/AVFoundation.h>
#import <CoreVideo/CoreVideo.h>
#import <Foundation/Foundation.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t interrupted;
static dispatch_source_t watchdog;

static void interrupt_capture(int signal_number)
{
    (void)signal_number;
    interrupted = 1;
}

static void watch_parent(BOOL listing)
{
    pid_t parent = getppid();
    uint64_t started = clock_gettime_nsec_np(CLOCK_MONOTONIC);
    watchdog = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0,
        dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0));
    dispatch_source_set_timer(watchdog, DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC,
                              10 * NSEC_PER_MSEC);
    dispatch_source_set_event_handler(watchdog, ^{
        if (getppid() != parent || interrupted) {
            /* Also covers blocked framework calls and an unanswered TCC prompt. */
            _exit(0);
        }
        if (listing && clock_gettime_nsec_np(CLOCK_MONOTONIC) - started > 4 * NSEC_PER_SEC) {
            fputs("Camera enumeration timed out\n", stderr);
            _exit(1);
        }
    });
    dispatch_resume(watchdog);
}

static uint64_t host_nanoseconds(void)
{
    /* Match GLib's receive timestamps and Mach audio host ticks. Darwin's
     * CLOCK_MONOTONIC includes accumulated sleep time; uptime does not. */
    return clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
}

static void put_le(uint8_t *out, uint64_t value, size_t size)
{
    for (size_t i = 0; i < size; i++) {
        out[i] = value >> (8 * i);
    }
}

/* No rotation or mirroring: board placement and sensor registers own those. */
static NSMutableData *rgb_frame(CVPixelBufferRef pixels, uint64_t timestamp)
{
    size_t width = CVPixelBufferGetWidth(pixels);
    size_t height = CVPixelBufferGetHeight(pixels);
    size_t stride = CVPixelBufferGetBytesPerRow(pixels);
    if (!width || !height || width > 1920 || height > 1080 ||
        stride < width * 4 || CVPixelBufferIsPlanar(pixels) ||
        CVPixelBufferGetPixelFormatType(pixels) != kCVPixelFormatType_32BGRA) {
        return nil;
    }
    if (CVPixelBufferLockBaseAddress(pixels, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) {
        return nil;
    }
    const uint8_t *source = CVPixelBufferGetBaseAddress(pixels);
    NSMutableData *frame = source ? [NSMutableData dataWithLength:32 + width * height * 3] : nil;
    if (frame) {
        uint8_t *out = frame.mutableBytes;
        memcpy(out, "LCAMRGB1", 8);
        put_le(out + 8, width, 4);
        put_le(out + 12, height, 4);
        put_le(out + 16, width * height * 3, 4);
        put_le(out + 20, 0, 4);
        put_le(out + 24, timestamp, 8);
        out += 32;
        for (size_t y = 0; y < height; y++) {
            const uint8_t *row = source + y * stride;
            for (size_t x = 0; x < width; x++, out += 3) {
                out[0] = row[x * 4 + 2];
                out[1] = row[x * 4 + 1];
                out[2] = row[x * 4];
            }
        }
    }
    CVPixelBufferUnlockBaseAddress(pixels, kCVPixelBufferLock_ReadOnly);
    return frame;
}

@interface CameraSink : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate> {
    NSCondition *_condition;
    NSMutableData *_latest;
    uint32_t _dropped;
    NSString *_failure;
    BOOL _stopped;
}
- (void)offer:(NSMutableData *)frame;
- (void)recordDrop;
- (NSData *)take;
- (void)fail:(NSString *)message;
- (NSString *)failure;
- (BOOL)stopped;
- (void)stop;
- (void)writeTo:(int)descriptor;
@end

@implementation CameraSink
- (instancetype)init
{
    self = [super init];
    if (self) {
        _condition = [[NSCondition alloc] init];
    }
    return self;
}
- (void)offer:(NSMutableData *)frame
{
    [_condition lock];
    if (!_stopped) {
        if (_latest && _dropped != UINT32_MAX) {
            _dropped++;
        }
        _latest = frame; /* At most one pending frame; writer owns one more. */
        [_condition signal];
    }
    [_condition unlock];
}
- (void)recordDrop
{
    [_condition lock];
    if (!_stopped && _dropped != UINT32_MAX) {
        _dropped++;
    }
    [_condition unlock];
}
- (NSData *)take
{
    [_condition lock];
    if (!_latest && !_stopped && !interrupted) {
        [_condition waitUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
    }
    NSMutableData *frame = _stopped || interrupted ? nil : _latest;
    if (frame) {
        put_le((uint8_t *)frame.mutableBytes + 20, _dropped, 4);
    }
    _latest = nil;
    [_condition unlock];
    return frame;
}
- (void)fail:(NSString *)message
{
    [_condition lock];
    if (!_failure) {
        _failure = message;
    }
    _stopped = YES;
    _latest = nil;
    [_condition broadcast];
    [_condition unlock];
}
- (NSString *)failure
{
    [_condition lock];
    NSString *result = _failure;
    [_condition unlock];
    return result;
}
- (BOOL)stopped
{
    [_condition lock];
    BOOL result = _stopped || interrupted;
    [_condition unlock];
    return result;
}
- (void)stop
{
    [_condition lock];
    _stopped = YES;
    _latest = nil;
    [_condition broadcast];
    [_condition unlock];
}
- (void)writeTo:(int)descriptor
{
    while (![self stopped]) {
        @autoreleasepool {
            NSData *frame = [self take];
            const uint8_t *bytes = frame.bytes;
            size_t offset = 0;
            while (offset < frame.length && ![self stopped]) {
                ssize_t count = write(descriptor, bytes + offset, frame.length - offset);
                if (count > 0) {
                    offset += count;
                } else if (count < 0 && errno == EINTR) {
                    continue;
                } else if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                    struct pollfd fd = { .fd = descriptor, .events = POLLOUT };
                    int result = poll(&fd, 1, 100);
                    if ((result < 0 && errno != EINTR) || (fd.revents & (POLLERR | POLLHUP | POLLNVAL))) {
                        [self fail:@"Camera output pipe closed"];
                    }
                } else {
                    [self fail:@"Camera output write failed"];
                }
            }
        }
    }
}
- (void)captureOutput:(AVCaptureOutput *)output didOutputSampleBuffer:(CMSampleBufferRef)sample
       fromConnection:(AVCaptureConnection *)connection
{
    (void)output;
    (void)connection;
    @autoreleasepool {
        if ([self stopped]) {
            return;
        }
        CVPixelBufferRef pixels = CMSampleBufferGetImageBuffer(sample);
        NSMutableData *frame = pixels ? rgb_frame(pixels, host_nanoseconds()) : nil;
        if (frame) {
            [self offer:frame];
        } else {
            [self fail:@"Unsupported camera pixel buffer"];
        }
    }
}
- (void)captureOutput:(AVCaptureOutput *)output didDropSampleBuffer:(CMSampleBufferRef)sample
       fromConnection:(AVCaptureConnection *)connection
{
    (void)output;
    (void)sample;
    (void)connection;
    [self recordDrop];
}
@end

static NSArray<AVCaptureDevice *> *cameras(void)
{
    return [AVCaptureDeviceDiscoverySession
        discoverySessionWithDeviceTypes:@[AVCaptureDeviceTypeBuiltInWideAngleCamera,
            AVCaptureDeviceTypeExternal, AVCaptureDeviceTypeContinuityCamera]
        mediaType:AVMediaTypeVideo position:AVCaptureDevicePositionUnspecified].devices;
}

static NSString *authorization(void)
{
    switch ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo]) {
    case AVAuthorizationStatusAuthorized: return @"authorized";
    case AVAuthorizationStatusNotDetermined: return @"not-determined";
    case AVAuthorizationStatusDenied: return @"denied";
    case AVAuthorizationStatusRestricted: return @"restricted";
    }
    return @"restricted";
}

static int list_cameras(void)
{
    NSMutableArray *devices = [NSMutableArray array];
    for (AVCaptureDevice *device in cameras()) {
        [devices addObject:@{ @"id":device.uniqueID, @"name":device.localizedName }];
    }
    NSData *json = [NSJSONSerialization dataWithJSONObject:
        @{ @"supported":@YES, @"authorization":authorization(), @"devices":devices }
        options:0 error:nil];
    if (!json || fwrite(json.bytes, 1, json.length, stdout) != json.length || puts("") == EOF) {
        return 1;
    }
    return 0;
}

static BOOL authorize(void)
{
    if ([authorization() isEqualToString:@"not-determined"]) {
        __block BOOL answered = NO;
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL granted) {
            (void)granted;
            dispatch_async(dispatch_get_main_queue(), ^{ answered = YES; });
        }];
        while (!answered && !interrupted) {
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
        }
    }
    return [authorization() isEqualToString:@"authorized"];
}

static int capture(NSString *identifier)
{
    AVCaptureDevice *device = nil;
    for (AVCaptureDevice *candidate in cameras()) {
        if ([candidate.uniqueID isEqualToString:identifier]) {
            device = candidate;
            break;
        }
    }
    if (!device) {
        fputs("Camera device is unavailable\n", stderr);
        return 1;
    }
    if (!authorize()) {
        fprintf(stderr, "Camera permission is %s\n", authorization().UTF8String);
        return 1;
    }
    if (interrupted) {
        return 0;
    }
    int flags = fcntl(STDOUT_FILENO, F_GETFL);
    if (flags < 0 || fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK) < 0) {
        fputs("Cannot configure camera output pipe\n", stderr);
        return 1;
    }
    AVCaptureSession *session = [[AVCaptureSession alloc] init];
    NSError *error = nil;
    AVCaptureDeviceInput *input = [AVCaptureDeviceInput deviceInputWithDevice:device error:&error];
    AVCaptureVideoDataOutput *output = [[AVCaptureVideoDataOutput alloc] init];
    CameraSink *sink = [[CameraSink alloc] init];
    dispatch_queue_t callback = dispatch_queue_create("com.listenai.emulator.camera.capture", DISPATCH_QUEUE_SERIAL);
    output.alwaysDiscardsLateVideoFrames = YES;
    output.videoSettings = @{ (id)kCVPixelBufferPixelFormatTypeKey:@(kCVPixelFormatType_32BGRA) };
    [output setSampleBufferDelegate:sink queue:callback];
    [session beginConfiguration];
    if (!input || ![session canAddInput:input] || ![session canAddOutput:output]) {
        [session commitConfiguration];
        fputs("Cannot configure camera capture\n", stderr);
        return 1;
    }
    [session addInput:input];
    [session addOutput:output];
    NSString *preset = nil;
    for (NSString *candidate in @[AVCaptureSessionPreset640x480,
                                  AVCaptureSessionPreset1280x720, AVCaptureSessionPreset1920x1080]) {
        if ([session canSetSessionPreset:candidate]) {
            preset = candidate;
            break;
        }
    }
    if (!preset) {
        [session commitConfiguration];
        fputs("Camera has no supported capture resolution\n", stderr);
        return 1;
    }
    session.sessionPreset = preset;
    AVCaptureConnection *connection = [output connectionWithMediaType:AVMediaTypeVideo];
    if (connection.isVideoMirroringSupported) {
        connection.automaticallyAdjustsVideoMirroring = NO;
        connection.videoMirrored = NO;
    }
    if ([connection isVideoRotationAngleSupported:0]) {
        connection.videoRotationAngle = 0;
    }
    [session commitConfiguration];
    if ([device lockForConfiguration:&error]) {
        for (AVFrameRateRange *range in device.activeFormat.videoSupportedFrameRateRanges) {
            if (range.minFrameRate <= 30 && range.maxFrameRate >= 30) {
                device.activeVideoMinFrameDuration = CMTimeMake(1, 30);
                device.activeVideoMaxFrameDuration = CMTimeMake(1, 30);
                break;
            }
        }
        [device unlockForConfiguration];
    }
    NSNotificationCenter *notifications = [NSNotificationCenter defaultCenter];
    id runtimeError = [notifications addObserverForName:AVCaptureSessionRuntimeErrorNotification
        object:session queue:nil usingBlock:^(NSNotification *note) {
            (void)note;
            [sink fail:@"Camera capture failed"];
        }];
    id disconnected = [notifications addObserverForName:AVCaptureDeviceWasDisconnectedNotification
        object:device queue:nil usingBlock:^(NSNotification *note) {
            (void)note;
            [sink fail:@"Camera device disconnected"];
        }];
    id interruption = [notifications addObserverForName:AVCaptureSessionWasInterruptedNotification
        object:session queue:nil usingBlock:^(NSNotification *note) {
            (void)note;
            [sink fail:@"Camera capture interrupted"];
        }];
    id stopped = [notifications addObserverForName:AVCaptureSessionDidStopRunningNotification
        object:session queue:nil usingBlock:^(NSNotification *note) {
            (void)note;
            if (![sink stopped]) {
                [sink fail:@"Camera capture stopped"];
            }
        }];
    dispatch_group_t writer = dispatch_group_create();
    dispatch_group_async(writer, dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        [sink writeTo:STDOUT_FILENO];
    });
    [session startRunning];
    if (!session.isRunning) {
        [sink fail:@"Camera capture did not start"];
    }
    while (![sink stopped]) {
        struct pollfd pipe = { .fd = STDOUT_FILENO, .events = 0 };
        if (poll(&pipe, 1, 0) < 0 || (pipe.revents & (POLLERR | POLLHUP | POLLNVAL))) {
            [sink fail:@"Camera output pipe closed"];
            break;
        }
        [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
    }
    [sink stop];
    [session stopRunning];
    [output setSampleBufferDelegate:nil queue:NULL];
    dispatch_sync(callback, ^{});
    dispatch_group_wait(writer, DISPATCH_TIME_FOREVER);
    [notifications removeObserver:runtimeError];
    [notifications removeObserver:disconnected];
    [notifications removeObserver:interruption];
    [notifications removeObserver:stopped];
    if (sink.failure) {
        fprintf(stderr, "%s\n", sink.failure.UTF8String);
        return 1;
    }
    return 0;
}

#ifndef LISEM_CAMERA_TEST
int main(int argc, const char **argv)
{
    @autoreleasepool {
        signal(SIGPIPE, SIG_IGN);
        signal(SIGTERM, interrupt_capture);
        signal(SIGINT, interrupt_capture);
        if (argc == 2 && strcmp(argv[1], "--list") == 0) {
            watch_parent(YES);
            return list_cameras();
        }
        if (argc == 3 && strcmp(argv[1], "--device") == 0 && argv[2][0]) {
            watch_parent(NO);
            return capture([NSString stringWithUTF8String:argv[2]]);
        }
        fputs("Usage: lisa-camera --list | --device ID\n", stderr);
        return 2;
    }
}
#endif

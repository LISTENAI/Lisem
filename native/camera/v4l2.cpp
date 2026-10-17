/* SPDX-License-Identifier: MIT */
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <signal.h>
#include <unistd.h>
#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>
#include <time.h>

static int request(int fd, unsigned long op, void *value)
{
    int result;
    do { result = ioctl(fd, op, value); } while (result < 0 && errno == EINTR);
    return result;
}

static void check(bool success, const char *operation)
{
    if (!success) { throw std::runtime_error(std::string(operation) + ": " + strerror(errno)); }
}

static uint64_t host_ns()
{
    timespec value{};
    check(clock_gettime(CLOCK_MONOTONIC, &value) == 0, "Camera clock");
    return uint64_t(value.tv_sec) * UINT64_C(1000000000) + value.tv_nsec;
}

static std::string quoted(const std::string &text)
{
    std::string result = "\"";
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') { result += '\\'; }
        if (c < 32) {
            char escaped[7];
            snprintf(escaped, sizeof(escaped), "\\u%04x", c);
            result += escaped;
        } else { result += char(c); }
    }
    return result + '"';
}

static std::string text(const __u8 *value, size_t length)
{
    return std::string(reinterpret_cast<const char *>(value), strnlen(
        reinterpret_cast<const char *>(value), length));
}

static std::string identity(const std::string &path, const v4l2_capability &cap)
{
    return path + "|" + text(cap.bus_info, sizeof(cap.bus_info));
}

static bool capture_device(const v4l2_capability &cap)
{
    uint32_t flags = cap.capabilities & V4L2_CAP_DEVICE_CAPS ? cap.device_caps : cap.capabilities;
    return (flags & (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_STREAMING)) ==
                   (V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_STREAMING);
}

static void list_devices()
{
    glob_t paths{};
    int result = glob("/dev/video*", 0, nullptr, &paths);
    if (result != 0 && result != GLOB_NOMATCH) { throw std::runtime_error("Cannot enumerate cameras"); }
    std::string devices;
    bool denied = false;
    for (size_t i = 0; i < paths.gl_pathc; i++) {
        int fd = open(paths.gl_pathv[i], O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0) { denied |= errno == EACCES || errno == EPERM; continue; }
        v4l2_capability cap{};
        if (request(fd, VIDIOC_QUERYCAP, &cap) == 0 && capture_device(cap)) {
            if (!devices.empty()) { devices += ','; }
            devices += "{\"id\":" + quoted(identity(paths.gl_pathv[i], cap)) +
                ",\"name\":" + quoted(text(cap.card, sizeof(cap.card))) + "}";
        }
        close(fd);
    }
    globfree(&paths);
    printf("{\"supported\":true,\"authorization\":\"%s\",\"devices\":[%s]}\n",
           denied && devices.empty() ? "denied" : "unknown", devices.c_str());
}

static void put_le(uint8_t *out, uint64_t value, unsigned bytes)
{
    for (unsigned i = 0; i < bytes; i++) { out[i] = uint8_t(value >> (i * 8)); }
}

static uint8_t channel(int value)
{
    return uint8_t(std::clamp((value + 128) >> 8, 0, 255));
}

static std::vector<uint8_t> rgb_frame(const uint8_t *input, size_t length,
                                     const v4l2_pix_format &format, uint64_t timestamp)
{
    bool rgb = format.pixelformat == V4L2_PIX_FMT_RGB24;
    bool bgr = format.pixelformat == V4L2_PIX_FMT_BGR24;
    bool uyvy = format.pixelformat == V4L2_PIX_FMT_UYVY;
    bool yuyv = format.pixelformat == V4L2_PIX_FMT_YUYV;
    unsigned width = format.width, height = format.height;
    if ((!rgb && !bgr && !uyvy && !yuyv) || !width || !height || width > 1920 ||
        height > 1080 || ((yuyv || uyvy) && width % 2)) {
        throw std::runtime_error("Unsupported camera pixel format or dimensions");
    }
    size_t row_bytes = size_t(width) * (rgb || bgr ? 3 : 2);
    size_t stride = format.bytesperline;
    if (stride < row_bytes || length < stride * (height - 1) + row_bytes) {
        throw std::runtime_error("Truncated camera pixel buffer");
    }
    uint32_t encoding = format.ycbcr_enc;
    if (!encoding) { encoding = V4L2_MAP_YCBCR_ENC_DEFAULT(format.colorspace); }
    if ((uyvy || yuyv) && encoding != V4L2_YCBCR_ENC_601 && encoding != V4L2_YCBCR_ENC_709) {
        throw std::runtime_error("Unsupported camera YCbCr encoding");
    }
    uint32_t quantization = format.quantization;
    if (!quantization) {
        quantization = V4L2_MAP_QUANTIZATION_DEFAULT(rgb || bgr, format.colorspace, encoding);
    }
    if (quantization != V4L2_QUANTIZATION_FULL_RANGE && quantization != V4L2_QUANTIZATION_LIM_RANGE) {
        throw std::runtime_error("Unsupported camera quantization range");
    }
    bool full = quantization == V4L2_QUANTIZATION_FULL_RANGE;
    bool hd = encoding == V4L2_YCBCR_ENC_709;
    int rv = full ? (hd ? 403 : 359) : (hd ? 459 : 409);
    int gu = full ? (hd ? 48 : 88) : (hd ? 55 : 100);
    int gv = full ? (hd ? 120 : 183) : (hd ? 136 : 208);
    int bu = full ? (hd ? 475 : 454) : (hd ? 541 : 516);
    std::vector<uint8_t> frame(32 + size_t(width) * height * 3);
    memcpy(frame.data(), "LCAMRGB1", 8);
    put_le(frame.data() + 8, width, 4);
    put_le(frame.data() + 12, height, 4);
    put_le(frame.data() + 16, frame.size() - 32, 4);
    put_le(frame.data() + 24, timestamp, 8);
    uint8_t *out = frame.data() + 32;
    for (unsigned y = 0; y < height; y++) {
        const uint8_t *row = input + y * stride;
        for (unsigned x = 0; x < width; x++, out += 3) {
            if (rgb || bgr) {
                out[0] = row[x * 3 + (bgr ? 2 : 0)];
                out[1] = row[x * 3 + 1];
                out[2] = row[x * 3 + (bgr ? 0 : 2)];
            } else {
                const uint8_t *pair = row + (x & ~1u) * 2;
                int yy = pair[(x % 2) * 2 + (uyvy ? 1 : 0)];
                int u = int(pair[uyvy ? 0 : 1]) - 128;
                int v = int(pair[uyvy ? 2 : 3]) - 128;
                int base = full ? yy * 256 : (yy - 16) * 298;
                out[0] = channel(base + rv * v);
                out[1] = channel(base - gu * u - gv * v);
                out[2] = channel(base + bu * u);
            }
        }
    }
    return frame;
}

/* Retain one in-flight packet and one latest frame. Never replace a partial
 * packet: its length header has already reached the consumer. */
class Output {
    std::vector<uint8_t> active, latest;
    size_t offset = 0;
    uint32_t dropped = 0;
public:
    bool pending() const { return !active.empty() || !latest.empty(); }
    void offer(std::vector<uint8_t> frame) {
        if (!latest.empty() && dropped != UINT32_MAX) { dropped++; }
        latest = std::move(frame);
    }
    void flush(int fd) {
        if (active.empty()) {
            active.swap(latest);
            if (active.empty()) { return; }
            put_le(active.data() + 20, dropped, 4);
            dropped = 0;
        }
        ssize_t count = write(fd, active.data() + offset,
                              std::min<size_t>(65536, active.size() - offset));
        if (count < 0 && (errno == EAGAIN || errno == EINTR)) { return; }
        check(count > 0, "Camera output pipe");
        offset += size_t(count);
        if (offset == active.size()) { active.clear(); offset = 0; }
    }
};

class Camera {
    struct Buffer { void *data; size_t length; };
    int fd = -1;
    bool streaming = false;
    std::vector<Buffer> buffers;
public:
    Camera() = default;
    Camera(const Camera &) = delete;
    Camera &operator=(const Camera &) = delete;
    ~Camera() {
        if (streaming) { int type = V4L2_BUF_TYPE_VIDEO_CAPTURE; request(fd, VIDIOC_STREAMOFF, &type); }
        for (const Buffer &buffer : buffers) { munmap(buffer.data, buffer.length); }
        if (fd >= 0) { close(fd); }
    }
    void capture(const std::string &id) {
        auto separator = id.find('|');
        std::string path = id.substr(0, separator);
        if (separator == std::string::npos || path.rfind("/dev/video", 0) != 0) {
            throw std::runtime_error("Invalid camera device ID");
        }
        fd = open(path.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
        check(fd >= 0, "Open selected camera");
        v4l2_capability cap{};
        check(request(fd, VIDIOC_QUERYCAP, &cap) == 0, "Camera capabilities");
        if (!capture_device(cap) || identity(path, cap) != id) {
            throw std::runtime_error("Selected camera is no longer available");
        }
        v4l2_format format{};
        format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        format.fmt.pix.width = 640; format.fmt.pix.height = 480;
        format.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV;
        format.fmt.pix.field = V4L2_FIELD_NONE;
        check(request(fd, VIDIOC_S_FMT, &format) == 0, "Camera pixel format");
        // Validate the negotiated layout before allocating device buffers.
        const auto &pix = format.fmt.pix;
        if (pix.field != V4L2_FIELD_NONE) { throw std::runtime_error("Interlaced camera capture is unsupported"); }
        if (!pix.sizeimage || pix.sizeimage > 16 * 1024 * 1024) {
            throw std::runtime_error("Unsupported camera buffer size");
        }
        std::vector<uint8_t> probe(pix.sizeimage);
        rgb_frame(probe.data(), probe.size(), pix, 0);
        v4l2_streamparm rate{};
        rate.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        rate.parm.capture.timeperframe.numerator = 1;
        rate.parm.capture.timeperframe.denominator = 30;
        if (request(fd, VIDIOC_S_PARM, &rate) < 0 && errno != EINVAL && errno != ENOTTY) {
            check(false, "Camera frame rate");
        }
        v4l2_requestbuffers allocation{};
        allocation.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        allocation.memory = V4L2_MEMORY_MMAP;
        allocation.count = 4;
        check(request(fd, VIDIOC_REQBUFS, &allocation) == 0, "Camera buffers");
        if (allocation.count < 2 || allocation.count > 8) { throw std::runtime_error("Invalid camera buffer count"); }
        for (unsigned i = 0; i < allocation.count; i++) {
            v4l2_buffer buffer{};
            buffer.type = allocation.type; buffer.memory = allocation.memory; buffer.index = i;
            check(request(fd, VIDIOC_QUERYBUF, &buffer) == 0, "Camera buffer layout");
            if (buffer.length < pix.sizeimage || buffer.length > 16 * 1024 * 1024) {
                throw std::runtime_error("Invalid camera buffer length");
            }
            void *data = mmap(nullptr, buffer.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, buffer.m.offset);
            check(data != MAP_FAILED, "Map camera buffer");
            buffers.push_back({data, buffer.length});
            check(request(fd, VIDIOC_QBUF, &buffer) == 0, "Queue camera buffer");
        }
        int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        check(request(fd, VIDIOC_STREAMON, &type) == 0, "Start camera");
        streaming = true;
        int flags = fcntl(STDOUT_FILENO, F_GETFL);
        check(flags >= 0 && fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK) == 0, "Camera output mode");
        Output output;
        pid_t parent = getppid();
        uint64_t deadline = host_ns() + UINT64_C(5000000000);
        while (getppid() == parent) {
            pollfd descriptors[2] = {{fd, POLLIN, 0}, {STDOUT_FILENO, short(output.pending() ? POLLOUT : 0), 0}};
            int ready = poll(descriptors, 2, 1000);
            if (ready < 0 && errno == EINTR) { continue; }
            check(ready >= 0, "Camera poll");
            if ((descriptors[0].revents | descriptors[1].revents) & (POLLERR | POLLHUP | POLLNVAL)) {
                throw std::runtime_error("Camera or output pipe disconnected");
            }
            if (descriptors[0].revents & POLLIN) {
                v4l2_buffer buffer{};
                buffer.type = allocation.type; buffer.memory = allocation.memory;
                int result = request(fd, VIDIOC_DQBUF, &buffer);
                if (result < 0 && errno == EAGAIN) { continue; }
                check(result == 0, "Read camera buffer");
                if (buffer.index >= buffers.size() || buffer.bytesused > buffers[buffer.index].length) {
                    throw std::runtime_error("Invalid captured camera buffer");
                }
                if (!(buffer.flags & V4L2_BUF_FLAG_ERROR)) {
                    uint64_t stamp = host_ns();
                    if ((buffer.flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) == V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC &&
                        buffer.timestamp.tv_sec >= 0 && buffer.timestamp.tv_usec >= 0 && buffer.timestamp.tv_usec < 1000000) {
                        stamp = uint64_t(buffer.timestamp.tv_sec) * UINT64_C(1000000000) + buffer.timestamp.tv_usec * UINT64_C(1000);
                    }
                    output.offer(rgb_frame(static_cast<const uint8_t *>(buffers[buffer.index].data), buffer.bytesused, pix, stamp));
                    deadline = host_ns() + UINT64_C(5000000000);
                }
                check(request(fd, VIDIOC_QBUF, &buffer) == 0, "Return camera buffer");
            }
            if (descriptors[1].revents & POLLOUT) { output.flush(STDOUT_FILENO); }
            if (host_ns() >= deadline) { throw std::runtime_error("Camera stopped delivering frames"); }
        }
    }
};

int main(int argc, char **argv)
{
    signal(SIGPIPE, SIG_IGN);
    try {
        pid_t parent = getppid();
        check(prctl(PR_SET_PDEATHSIG, SIGTERM) == 0, "Camera parent watchdog");
        if (getppid() != parent) { return 0; }
        if (argc == 2 && !strcmp(argv[1], "--list")) { list_devices(); return 0; }
        if (argc == 3 && !strcmp(argv[1], "--device")) { Camera camera; camera.capture(argv[2]); return 0; }
        throw std::runtime_error("Usage: lisa-camera --list | --device DEVICE_ID");
    } catch (const std::exception &error) {
        fprintf(stderr, "%s\n", error.what());
        return 2;
    }
}

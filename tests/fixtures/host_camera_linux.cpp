/* SPDX-License-Identifier: MIT */
#define main camera_main
#include "../../native/camera/v4l2.cpp"
#undef main
#include <cassert>

static uint32_t le32(const uint8_t *p)
{
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}

int main()
{
    char program[] = "lisa-camera", mode[] = "--device", missing[] = "not-a-camera";
    char *arguments[] = {program, mode, missing};
    assert(camera_main(3, arguments) == 2);

    v4l2_pix_format format{};
    format.width = format.height = 2;
    format.pixelformat = V4L2_PIX_FMT_BGR24;
    format.bytesperline = 8;
    uint8_t bgr[] = {3, 2, 1, 6, 5, 4, 99, 99, 9, 8, 7, 12, 11, 10};
    auto frame = rgb_frame(bgr, sizeof(bgr), format, UINT64_C(0x0102030405060708));
    assert(frame.size() == 44 && !memcmp(frame.data(), "LCAMRGB1", 8));
    assert(le32(frame.data() + 8) == 2 && le32(frame.data() + 12) == 2);
    assert(le32(frame.data() + 16) == 12 && le32(frame.data() + 20) == 0);
    assert(le32(frame.data() + 24) == 0x05060708 && le32(frame.data() + 28) == 0x01020304);
    for (unsigned i = 0; i < 12; i++) { assert(frame[i + 32] == i + 1); }
    bool rejected = false;
    try { rgb_frame(bgr, sizeof(bgr) - 1, format, 0); }
    catch (const std::exception &) { rejected = true; }
    assert(rejected);
    format.width = 1; format.height = 1; format.bytesperline = 3;
    format.pixelformat = V4L2_PIX_FMT_RGB24;
    frame = rgb_frame(bgr, 3, format, 0);
    assert(frame[32] == 3 && frame[34] == 1);
    format.width = 2; format.height = 1; format.bytesperline = 6;
    format.pixelformat = V4L2_PIX_FMT_YUYV;
    format.colorspace = V4L2_COLORSPACE_SMPTE170M;
    uint8_t yuyv[] = {16, 128, 235, 128, 99, 99};
    frame = rgb_frame(yuyv, sizeof(yuyv), format, 0);
    for (unsigned i = 0; i < 3; i++) { assert(frame[32 + i] == 0 && frame[35 + i] == 255); }
    format.pixelformat = V4L2_PIX_FMT_UYVY;
    format.quantization = V4L2_QUANTIZATION_FULL_RANGE;
    uint8_t uyvy[] = {128, 0, 128, 255, 99, 99};
    frame = rgb_frame(uyvy, sizeof(uyvy), format, 0);
    for (unsigned i = 0; i < 3; i++) { assert(frame[32 + i] == 0 && frame[35 + i] == 255); }
    format.pixelformat = V4L2_PIX_FMT_YUYV;
    format.quantization = V4L2_QUANTIZATION_LIM_RANGE;
    uint8_t red[] = {81, 90, 81, 240, 0, 0};
    frame = rgb_frame(red, sizeof(red), format, 0);
    assert(frame[32] >= 253 && frame[33] <= 1 && frame[34] <= 1);
    format.ycbcr_enc = V4L2_YCBCR_ENC_709;
    uint8_t red709[] = {63, 102, 63, 240, 0, 0};
    frame = rgb_frame(red709, sizeof(red709), format, 0);
    assert(frame[32] >= 254 && frame[33] <= 2 && frame[34] <= 1);
    format.ycbcr_enc = V4L2_YCBCR_ENC_BT2020;
    rejected = false;
    try { rgb_frame(red, sizeof(red), format, 0); }
    catch (const std::exception &) { rejected = true; }
    assert(rejected);
    assert(quoted("a\"\\\n") == "\"a\\\"\\\\\\u000a\"");
    v4l2_capability cap{};
    strcpy(reinterpret_cast<char *>(cap.bus_info), "usb-1");
    assert(identity("/dev/video0", cap) == "/dev/video0|usb-1");
    cap.capabilities = V4L2_CAP_DEVICE_CAPS;
    cap.device_caps = V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_STREAMING;
    assert(capture_device(cap));
    cap.device_caps = V4L2_CAP_VIDEO_CAPTURE_MPLANE | V4L2_CAP_STREAMING;
    assert(!capture_device(cap));

    int pipefd[2];
    assert(pipe2(pipefd, O_NONBLOCK | O_CLOEXEC) == 0);
    // Force a frame to span writes and hold the reader back while a newer
    // frame replaces the pending one. The first packet must remain intact.
    format = {};
    format.width = 640; format.height = 480;
    format.bytesperline = 640 * 3; format.pixelformat = V4L2_PIX_FMT_RGB24;
    std::vector<uint8_t> pixels(640 * 480 * 3, 17);
    auto first = rgb_frame(pixels.data(), pixels.size(), format, 1);
    Output output;
    output.offer(first);
    output.flush(pipefd[1]);
    auto second = rgb_frame(pixels.data(), pixels.size(), format, 2);
    output.offer(second);
    pixels.assign(pixels.size(), 23);
    auto last = rgb_frame(pixels.data(), pixels.size(), format, 3);
    output.offer(last);
    output.flush(pipefd[1]); // Nonblocking EAGAIN is not a disconnection.
    std::vector<uint8_t> received;
    uint8_t bytes[4096];
    while (output.pending()) {
        ssize_t size;
        while ((size = read(pipefd[0], bytes, sizeof(bytes))) > 0) {
            received.insert(received.end(), bytes, bytes + size);
        }
        output.flush(pipefd[1]);
    }
    ssize_t size;
    while ((size = read(pipefd[0], bytes, sizeof(bytes))) > 0) {
        received.insert(received.end(), bytes, bytes + size);
    }
    assert(received.size() == first.size() + last.size());
    assert(std::equal(first.begin(), first.end(), received.begin()));
    put_le(last.data() + 20, 1, 4);
    assert(std::equal(last.begin(), last.end(), received.begin() + first.size()));
    close(pipefd[0]);
    signal(SIGPIPE, SIG_IGN);
    output.offer(last);
    rejected = false;
    try { output.flush(pipefd[1]); }
    catch (const std::exception &) { rejected = true; }
    assert(rejected);
    close(pipefd[1]);
    return 0;
}

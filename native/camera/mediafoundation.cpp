/* SPDX-License-Identifier: MIT */
#define NOMINMAX
#include <windows.h>
#include <tlhelp32.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mferror.h>
#include <atomic>
#include <climits>
#include <cstddef>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

template<class T> class Com {
    T *value = nullptr;
public:
    ~Com() { if (value) { value->Release(); } }
    Com() = default;
    Com(const Com &) = delete;
    Com &operator=(const Com &) = delete;
    T *operator->() const { return value; }
    T *get() const { return value; }
    T **put() { if (value) { value->Release(); value = nullptr; } return &value; }
};

static void check(HRESULT result, const char *operation)
{
    if (FAILED(result)) {
        char text[192];
        snprintf(text, sizeof(text), "%s failed (HRESULT 0x%08lx)%s", operation,
                 static_cast<unsigned long>(result),
                 result == E_ACCESSDENIED ? ": camera permission denied" : "");
        throw std::runtime_error(text);
    }
}

static std::string utf8(const wchar_t *text)
{
    int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, -1,
                                   nullptr, 0, nullptr, nullptr);
    if (!size) { throw std::runtime_error("Invalid camera device text"); }
    std::string value(static_cast<size_t>(size), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, -1,
                             value.data(), size, nullptr, nullptr)) {
        throw std::runtime_error("Cannot encode camera device text");
    }
    value.pop_back();
    return value;
}

static std::string attribute(IMFAttributes *attributes, REFGUID key)
{
    wchar_t *text = nullptr;
    UINT32 length;
    check(attributes->GetAllocatedString(key, &text, &length), "Camera attribute");
    try {
        std::string value = utf8(text);
        CoTaskMemFree(text);
        return value;
    } catch (...) {
        CoTaskMemFree(text);
        throw;
    }
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
        } else { result += static_cast<char>(c); }
    }
    return result + '"';
}

static uint64_t host_ns()
{
    LARGE_INTEGER counter, frequency;
    QueryPerformanceCounter(&counter);
    QueryPerformanceFrequency(&frequency);
    uint64_t ticks = static_cast<uint64_t>(counter.QuadPart);
    uint64_t rate = static_cast<uint64_t>(frequency.QuadPart);
    return ticks / rate * UINT64_C(1000000000) +
           ticks % rate * UINT64_C(1000000000) / rate;
}

static void put_le(uint8_t *out, uint64_t value, unsigned bytes)
{
    for (unsigned i = 0; i < bytes; i++) { out[i] = static_cast<uint8_t>(value >> (8 * i)); }
}

static std::vector<uint8_t> rgb_frame(const BYTE *top, LONG stride,
                                     UINT32 width, UINT32 height, uint64_t timestamp)
{
    if (!width || width > 1920 || !height || height > 1080 ||
        stride == LONG_MIN || static_cast<uint64_t>(stride < 0 ? -stride : stride) < width * 4) {
        throw std::runtime_error("Unsupported camera pixel buffer geometry");
    }
    std::vector<uint8_t> frame(32 + static_cast<size_t>(width) * height * 3);
    memcpy(frame.data(), "LCAMRGB1", 8);
    put_le(frame.data() + 8, width, 4);
    put_le(frame.data() + 12, height, 4);
    put_le(frame.data() + 16, static_cast<uint64_t>(width) * height * 3, 4);
    put_le(frame.data() + 24, timestamp, 8);
    auto *out = frame.data() + 32;
    for (UINT32 y = 0; y < height; y++) {
        const BYTE *row = top + static_cast<ptrdiff_t>(y) * stride;
        for (UINT32 x = 0; x < width; x++, out += 3) {
            out[0] = row[x * 4 + 2]; out[1] = row[x * 4 + 1]; out[2] = row[x * 4];
        }
    }
    return frame;
}

static std::atomic<uint64_t> last_frame;

class Mailbox {
    std::mutex mutex;
    std::condition_variable ready;
    std::vector<uint8_t> latest;
    uint32_t dropped = 0;
public:
    void offer(std::vector<uint8_t> frame) {
        std::lock_guard<std::mutex> lock(mutex);
        if (!latest.empty() && dropped != UINT32_MAX) { dropped++; }
        latest = std::move(frame);
        last_frame.store(host_ns());
        ready.notify_one();
    }
    std::vector<uint8_t> take() {
        std::unique_lock<std::mutex> lock(mutex);
        ready.wait(lock, [&] { return !latest.empty(); });
        std::vector<uint8_t> result;
        result.swap(latest);
        put_le(result.data() + 20, dropped, 4);
        return result;
    }
};

static void writer(Mailbox &mailbox)
{
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    for (;;) {
        auto frame = mailbox.take();
        for (size_t offset = 0; offset < frame.size();) {
            DWORD count = 0;
            if (!WriteFile(output, frame.data() + offset,
                           static_cast<DWORD>(frame.size() - offset), &count, nullptr) || !count) {
                fputs("Camera output pipe closed\n", stderr);
                ExitProcess(1);
            }
            offset += count;
        }
    }
}

static HANDLE parent_process()
{
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) { throw std::runtime_error("Cannot inspect camera parent"); }
    PROCESSENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    DWORD parent = 0;
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (entry.th32ProcessID == GetCurrentProcessId()) { parent = entry.th32ParentProcessID; break; }
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    HANDLE process = parent ? OpenProcess(SYNCHRONIZE, FALSE, parent) : nullptr;
    if (!process) { throw std::runtime_error("Camera parent is unavailable"); }
    return process;
}

static void watch_parent(bool listing)
{
    HANDLE parent = parent_process();
    std::thread([parent, listing] {
        uint64_t started = host_ns();
        for (;;) {
            if (WaitForSingleObject(parent, 100) != WAIT_TIMEOUT) { ExitProcess(0); }
            uint64_t frame = last_frame.load(), now = host_ns();
            if ((listing && now - started > UINT64_C(4000000000)) ||
                (!listing && !frame && now - started > UINT64_C(120000000000)) ||
                (frame && now - frame > UINT64_C(5000000000))) {
                fputs("Camera stream timed out or was disconnected\n", stderr);
                ExitProcess(1);
            }
        }
    }).detach();
}

static void list_cameras()
{
    Com<IMFAttributes> attributes;
    check(MFCreateAttributes(attributes.put(), 1), "Camera enumeration attributes");
    check(attributes->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                             MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID), "Camera source type");
    IMFActivate **devices = nullptr;
    UINT32 count = 0;
    check(MFEnumDeviceSources(attributes.get(), &devices, &count), "Camera enumeration");
    std::string result = "{\"supported\":true,\"authorization\":\"unknown\",\"devices\":[";
    try {
        for (UINT32 i = 0; i < count; i++) {
            if (i) { result += ','; }
            result += "{\"id\":" + quoted(attribute(devices[i], MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK)) +
                      ",\"name\":" + quoted(attribute(devices[i], MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME)) + '}';
        }
        result += "]}\n";
    } catch (...) {
        for (UINT32 i = 0; i < count; i++) { devices[i]->Release(); }
        CoTaskMemFree(devices);
        throw;
    }
    for (UINT32 i = 0; i < count; i++) { devices[i]->Release(); }
    CoTaskMemFree(devices);
    if (fwrite(result.data(), 1, result.size(), stdout) != result.size()) {
        throw std::runtime_error("Cannot write camera enumeration");
    }
}

static void capture(const wchar_t *identifier)
{
    constexpr DWORD video_stream = static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM);
    Com<IMFAttributes> attributes;
    Com<IMFMediaSource> source;
    Com<IMFSourceReader> reader;
    check(MFCreateAttributes(attributes.put(), 3), "Camera capture attributes");
    check(attributes->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE,
                             MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID), "Camera source type");
    check(attributes->SetString(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK, identifier), "Camera selection");
    check(MFCreateDeviceSource(attributes.get(), source.put()), "Open selected camera");
    check(MFCreateAttributes(attributes.put(), 2), "Camera reader attributes");
    check(attributes->SetUINT32(MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING, TRUE), "Camera RGB conversion");
    check(attributes->SetUINT32(MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, TRUE), "Camera hardware transforms");
    check(MFCreateSourceReaderFromMediaSource(source.get(), attributes.get(), reader.put()), "Camera reader");
    Com<IMFMediaType> native;
    UINT32 best_index = UINT32_MAX;
    uint64_t best_score = UINT64_MAX;
    for (DWORD index = 0; index < 1024; index++) {
        HRESULT result = reader->GetNativeMediaType(video_stream, index, native.put());
        if (result == MF_E_NO_MORE_TYPES) { break; }
        check(result, "Camera formats");
        UINT32 width, height;
        if (FAILED(MFGetAttributeSize(native.get(), MF_MT_FRAME_SIZE, &width, &height)) ||
            !width || !height || width > 1920 || height > 1080) { continue; }
        uint64_t score = static_cast<uint64_t>(abs(static_cast<int>(width) - 640)) +
                         static_cast<uint64_t>(abs(static_cast<int>(height) - 480));
        UINT32 numerator, denominator;
        if (SUCCEEDED(MFGetAttributeRatio(native.get(), MF_MT_FRAME_RATE, &numerator, &denominator)) && denominator) {
            score = score * 1000 + static_cast<uint64_t>(abs(static_cast<int>(numerator / denominator) - 30));
        } else { score = score * 1000 + 100; }
        if (score < best_score) { best_score = score; best_index = index; }
    }
    if (best_index == UINT32_MAX) { throw std::runtime_error("Camera has no supported capture resolution"); }
    check(reader->GetNativeMediaType(video_stream, best_index, native.put()), "Camera capture format");
    check(reader->SetCurrentMediaType(video_stream, nullptr, native.get()), "Camera native format");
    Com<IMFMediaType> type;
    check(MFCreateMediaType(type.put()), "Camera RGB type");
    check(type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video), "Camera video type");
    check(type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_RGB32), "Camera RGB type");
    check(reader->SetCurrentMediaType(video_stream, nullptr, type.get()), "Camera RGB output");
    check(reader->SetStreamSelection(video_stream, TRUE), "Camera stream selection");
    auto mailbox = std::make_shared<Mailbox>();
    std::thread output([mailbox] { writer(*mailbox); });
    output.detach();
    for (;;) {
        DWORD flags = 0;
        LONGLONG sample_time = 0;
        Com<IMFSample> sample;
        check(reader->ReadSample(video_stream, 0, nullptr, &flags, &sample_time, sample.put()), "Camera read");
        if (flags & (MF_SOURCE_READERF_ERROR | MF_SOURCE_READERF_ENDOFSTREAM)) { throw std::runtime_error("Camera was disconnected"); }
        if (!sample.get()) { continue; }
        check(reader->GetCurrentMediaType(video_stream, type.put()), "Camera current format");
        UINT32 width, height;
        GUID format;
        check(type->GetGUID(MF_MT_SUBTYPE, &format), "Camera pixel format");
        check(MFGetAttributeSize(type.get(), MF_MT_FRAME_SIZE, &width, &height), "Camera dimensions");
        if (format != MFVideoFormat_RGB32 || !width || width > 1920 || !height || height > 1080) {
            throw std::runtime_error("Unsupported camera pixel format");
        }
        Com<IMFMediaBuffer> buffer;
        check(sample->ConvertToContiguousBuffer(buffer.put()), "Camera sample buffer");
        Com<IMF2DBuffer> plane;
        BYTE *pixels;
        LONG stride;
        if (SUCCEEDED(buffer->QueryInterface(IID_IMF2DBuffer, reinterpret_cast<void **>(plane.put())))) {
            check(plane->Lock2D(&pixels, &stride), "Camera 2D buffer lock");
            try { mailbox->offer(rgb_frame(pixels, stride, width, height, host_ns())); }
            catch (...) { plane->Unlock2D(); throw; }
            check(plane->Unlock2D(), "Camera 2D buffer unlock");
        } else {
            UINT32 raw_stride;
            if (SUCCEEDED(type->GetUINT32(MF_MT_DEFAULT_STRIDE, &raw_stride))) { stride = static_cast<LONG>(raw_stride); }
            else { check(MFGetStrideForBitmapInfoHeader(MFVideoFormat_RGB32.Data1, width, &stride), "Camera buffer stride"); }
            DWORD length;
            check(buffer->Lock(&pixels, nullptr, &length), "Camera buffer lock");
            try {
                if (stride == LONG_MIN || static_cast<uint64_t>(stride < 0 ? -stride : stride) * height > length) {
                    throw std::runtime_error("Truncated camera pixel buffer");
                }
                if (stride < 0) { pixels += static_cast<size_t>(-stride) * (height - 1); }
                mailbox->offer(rgb_frame(pixels, stride, width, height, host_ns()));
            } catch (...) { buffer->Unlock(); throw; }
            check(buffer->Unlock(), "Camera buffer unlock");
        }
    }
}

int wmain(int argc, wchar_t **argv)
{
    bool listing = argc == 2 && !wcscmp(argv[1], L"--list");
    if (!listing && (argc != 3 || wcscmp(argv[1], L"--device") || !argv[2][0])) {
        fputs("Usage: lisa-camera --list | --device DEVICE_ID\n", stderr);
        return 2;
    }
    try {
        watch_parent(listing);
        check(CoInitializeEx(nullptr, COINIT_MULTITHREADED), "Camera COM initialization");
        check(MFStartup(MF_VERSION), "Media Foundation initialization");
        if (listing) { list_cameras(); MFShutdown(); CoUninitialize(); return 0; }
        capture(argv[2]);
    } catch (const std::exception &error) {
        fprintf(stderr, "%s\n", error.what());
        /* The writer can be blocked in a pipe; process teardown also closes
         * the selected camera after a framework failure. */
        ExitProcess(1);
    }
    return 0;
}

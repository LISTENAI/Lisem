/* SPDX-License-Identifier: MIT */
#define wmain camera_endpoint_entry
#include "../../native/camera/mediafoundation.cpp"
#undef wmain
#include <cassert>

int main()
{
    const BYTE pixels[] = {3, 2, 1, 0, 6, 5, 4, 0, 255, 255, 255, 255,
                           9, 8, 7, 0, 12, 11, 10, 0, 255, 255, 255, 255};
    auto positive = rgb_frame(pixels, 12, 2, 2, UINT64_C(0x0102030405060708));
    assert(positive.size() == 44 && !memcmp(positive.data(), "LCAMRGB1", 8));
    for (unsigned i = 0; i < 12; i++) { assert(positive[32 + i] == static_cast<uint8_t>(i + 1)); }
    auto negative = rgb_frame(pixels + 12, -12, 2, 2, 17);
    for (unsigned i = 0; i < 6; i++) {
        assert(negative[32 + i] == static_cast<uint8_t>(i + 7));
        assert(negative[38 + i] == static_cast<uint8_t>(i + 1));
    }
    assert(positive[8] == 2 && positive[12] == 2 && positive[16] == 12);
    assert(positive[24] == 8 && positive[31] == 1);
    assert(quoted("a\"\\\n") == "\"a\\\"\\\\\\u000a\"");
    assert(utf8(L"Camera \u76f8\u673a") == "Camera \xe7\x9b\xb8\xe6\x9c\xba");
    for (LONG stride : { 0L, 7L, LONG_MIN }) {
        bool rejected = false;
        try { rgb_frame(pixels, stride, 2, 2, 0); }
        catch (const std::runtime_error &) { rejected = true; }
        assert(rejected);
    }
    Mailbox mailbox;
    mailbox.offer(positive);
    mailbox.offer(negative);
    mailbox.offer(positive);
    auto latest = mailbox.take();
    assert(latest[20] == 2 && latest[32] == 1 && latest[24] == 8);
    assert(host_ns() > 0);
    return 0;
}

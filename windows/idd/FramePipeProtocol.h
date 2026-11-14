#pragma once
#include <cstdint>

// Private, local-only control protocol between SamaFrameBroker.exe and FrameClient.exe.
// It deliberately exposes one read-only operation: copy the newest desktop frame.
namespace sama::pipe {
inline constexpr wchar_t Name[] = L"\\\\.\\pipe\\SamaFrameBroker.v1";
inline constexpr char Magic[8] = {'S','A','M','A','P','I','P','1'};
inline constexpr uint32_t Version = 1;
inline constexpr uint32_t GetFrame = 1;
inline constexpr size_t RequestBytes = 20;
inline constexpr size_t ResponseBytes = 20;

#pragma pack(push, 1)
struct Request {
    char magic[8];
    uint32_t version;
    uint32_t command;
    uint32_t reserved;
};
struct Response {
    char magic[8];
    uint32_t version;
    uint32_t status;
    uint32_t payload_bytes;
};
#pragma pack(pop)

static_assert(sizeof(Request) == RequestBytes);
static_assert(sizeof(Response) == ResponseBytes);

inline Request MakeFrameRequest() {
    return Request{{'S','A','M','A','P','I','P','1'}, Version, GetFrame, 0};
}
inline bool IsMagic(const char value[8]) {
    for (size_t i = 0; i != 8; ++i) if (value[i] != Magic[i]) return false;
    return true;
}
} // namespace sama::pipe

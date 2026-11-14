// Project CPU-frame boundary. Uses D3D11 only; shared with the WARP self-test.
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <d3d11.h>
#include <cstdint>
#include <atomic>
#include <cstring>
#include <memory>
#include <mutex>
#include <utility>
#include <vector>

namespace sama {
constexpr UINT Width = 720, Height = 1568;
constexpr size_t HeaderBytes = 8 + 4*4 + 2*8; // Python struct <8s4I2Q = 40.
constexpr size_t PixelBytes = static_cast<size_t>(Width) * Height * 4;

template<class T> class ComOwner {
    T* value_ = nullptr;
public:
    ComOwner() = default;
    ~ComOwner() { Reset(); }
    ComOwner(const ComOwner&) = delete;
    ComOwner& operator=(const ComOwner&) = delete;
    void Reset() { if (value_) { value_->Release(); value_ = nullptr; } }
    T* Get() const { return value_; }
    T** Put() { Reset(); return &value_; }
    T* operator->() const { return value_; }
};

// Encode fields explicitly rather than relying on struct padding or alignment.
inline void Put32(std::vector<uint8_t>& out, size_t pos, uint32_t value) {
    for (size_t i = 0; i != 4; ++i) out[pos+i] = static_cast<uint8_t>(value >> (8*i));
}
inline void Put64(std::vector<uint8_t>& out, size_t pos, uint64_t value) {
    for (size_t i = 0; i != 8; ++i) out[pos+i] = static_cast<uint8_t>(value >> (8*i));
}
inline uint64_t QpcNanoseconds() {
    LARGE_INTEGER counter{}, frequency{};
    QueryPerformanceCounter(&counter);
    QueryPerformanceFrequency(&frequency);
    // Split before multiplying to avoid the ~5-hour overflow of counter*1e9.
    const auto ticks = static_cast<uint64_t>(counter.QuadPart);
    const auto hz = static_cast<uint64_t>(frequency.QuadPart);
    return (ticks/hz)*1000000000ull + (ticks%hz)*1000000000ull/hz;
}

class FrameCapture {
    ComOwner<ID3D11Texture2D> staging_;
    ComOwner<ID3D11Query> ready_;
public:
    // Caller owns the immediate context and source for the whole operation.
    // Nothing is published until GPU completion, CPU copy, and Unmap finish.
    HRESULT Capture(ID3D11Device* device, ID3D11DeviceContext* context,
                    ID3D11Texture2D* source, HANDLE stop, uint64_t sequence,
                    std::shared_ptr<const std::vector<uint8_t>>& packet);
};

class LatestPacket {
    std::mutex mutex_;
    std::shared_ptr<const std::vector<uint8_t>> pending_;
    uint64_t sequence_ = 0;
    bool seen_ = false;
    std::atomic<uint64_t> next_{0};
public:
    uint64_t NextSequence() { return ++next_; }
    uint64_t accepted = 0, overwritten = 0;
    bool Put(uint64_t sequence, std::shared_ptr<const std::vector<uint8_t>> packet) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!packet || (seen_ && sequence <= sequence_)) return false;
        overwritten += !!pending_;
        ++accepted;
        sequence_ = sequence;
        seen_ = true;
        pending_ = std::move(packet);
        return true;
    }
    std::shared_ptr<const std::vector<uint8_t>> Take() {
        std::lock_guard<std::mutex> lock(mutex_);
        return std::exchange(pending_, nullptr);
    }
};
} // namespace sama

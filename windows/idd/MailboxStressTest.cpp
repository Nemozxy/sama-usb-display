// Concurrent host stress for the exact LatestPacket class linked into the UMDF driver.
#include "FrameCapture.h"
#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <thread>
#include <vector>

int main() {
    constexpr size_t producers = 8;
    constexpr size_t perProducer = 20000;
    constexpr size_t total = producers * perProducer;

    sama::LatestPacket mailbox;
    std::vector<uint64_t> sequences(total);
    std::atomic<size_t> nextIndex{0};
    std::vector<std::thread> workers;
    workers.reserve(producers);
    for (size_t threadIndex = 0; threadIndex < producers; ++threadIndex) {
        workers.emplace_back([&]() {
            for (size_t i = 0; i < perProducer; ++i) {
                const size_t index = nextIndex.fetch_add(1, std::memory_order_relaxed);
                sequences[index] = mailbox.NextSequence();
            }
        });
    }
    for (auto& worker : workers) worker.join();
    std::sort(sequences.begin(), sequences.end());
    for (size_t i = 0; i < sequences.size(); ++i) {
        if (sequences[i] != static_cast<uint64_t>(i + 1)) {
            std::fprintf(stderr, "sequence uniqueness failed at %zu: %llu\n", i,
                         static_cast<unsigned long long>(sequences[i]));
            return 2;
        }
    }

    std::atomic<uint64_t> accepted{0}, rejected{0};
    std::atomic<bool> producersDone{false};
    std::vector<uint64_t> taken;
    taken.reserve(total);

    std::thread consumer([&]() {
        for (;;) {
            auto packet = mailbox.Take();
            if (packet) {
                if (packet->size() != sizeof(uint64_t)) {
                    std::fprintf(stderr, "packet size corruption\n");
                    std::terminate();
                }
                uint64_t sequence = 0;
                std::memcpy(&sequence, packet->data(), sizeof(sequence));
                taken.push_back(sequence);
            } else if (producersDone.load(std::memory_order_acquire)) {
                // One final drain closes the race between the last empty take and
                // the producers publishing their final packet.
                packet = mailbox.Take();
                if (packet) {
                    uint64_t sequence = 0;
                    std::memcpy(&sequence, packet->data(), sizeof(sequence));
                    taken.push_back(sequence);
                }
                if (!mailbox.Take()) break;
            } else {
                std::this_thread::yield();
            }
        }
    });

    workers.clear();
    workers.reserve(producers);
    for (size_t threadIndex = 0; threadIndex < producers; ++threadIndex) {
        workers.emplace_back([&]() {
            for (size_t i = 0; i < perProducer; ++i) {
                const uint64_t sequence = mailbox.NextSequence();
                auto packet = std::make_shared<const std::vector<uint8_t>>(
                    reinterpret_cast<const uint8_t*>(&sequence),
                    reinterpret_cast<const uint8_t*>(&sequence) + sizeof(sequence));
                if (mailbox.Put(sequence, std::move(packet))) {
                    accepted.fetch_add(1, std::memory_order_relaxed);
                } else {
                    rejected.fetch_add(1, std::memory_order_relaxed);
                }
            }
        });
    }
    for (auto& worker : workers) worker.join();
    producersDone.store(true, std::memory_order_release);
    consumer.join();

    if (accepted + rejected != total) {
        std::fprintf(stderr, "loss: accepted=%llu rejected=%llu total=%zu\n",
                     static_cast<unsigned long long>(accepted.load()),
                     static_cast<unsigned long long>(rejected.load()), total);
        return 3;
    }
    if (accepted.load() != mailbox.accepted || mailbox.overwritten > mailbox.accepted) {
        std::fprintf(stderr, "mailbox counters disagree\n");
        return 4;
    }
    if (taken.size() > accepted.load() || taken.empty()) {
        std::fprintf(stderr, "taken/accept mismatch\n");
        return 5;
    }
    for (size_t i = 1; i < taken.size(); ++i) {
        if (taken[i] <= taken[i - 1]) {
            std::fprintf(stderr, "consumer sequence regressed\n");
            return 6;
        }
    }
    if (mailbox.Take()) {
        std::fprintf(stderr, "mailbox not drained\n");
        return 7;
    }
    std::printf("MAILBOX_STRESS_OK total=%zu accepted=%llu overwritten=%llu taken=%zu rejected=%llu\n",
                total, static_cast<unsigned long long>(mailbox.accepted),
                static_cast<unsigned long long>(mailbox.overwritten), taken.size(),
                static_cast<unsigned long long>(rejected.load()));
    return 0;
}

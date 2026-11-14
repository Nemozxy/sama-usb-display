#include "DriverDiagnostics.h"
#include <TraceLoggingProvider.h>
#include <winmeta.h>
#include <atomic>
#include <windows.h>

TRACELOGGING_DEFINE_PROVIDER(
    g_SamaIddDiagnostics, "SamaScreen.Idd.Diagnostics",
    (0x76be8746, 0xa7f4, 0x4fa4, 0x97, 0x73, 0xdf, 0x56, 0xd1, 0x72, 0x17, 0x87));

namespace sama::diagnostics {
namespace {
std::atomic<bool> registered{false};
std::atomic<unsigned long> counter{0};

// A breadcrumb that survives the host dying: ETW only helps when registration
// succeeded inside the host, and when a driver is refused early the provider never
// registers at all. The host runs as LocalSystem, which may create this key. Only a
// stage name, a status and a counter are written - never frames, paths or pixels.
void WriteBreadcrumb(const char* name, LONG status) noexcept {
    HKEY key = nullptr;
    if (RegCreateKeyExW(HKEY_LOCAL_MACHINE, L"SOFTWARE\\SamaIdd", 0, nullptr, 0,
                        KEY_SET_VALUE, nullptr, &key, nullptr) != ERROR_SUCCESS) {
        return;
    }
    const DWORD sequence = counter.fetch_add(1, std::memory_order_relaxed) + 1;
    RegSetValueExA(key, "LastStage", 0, REG_SZ, reinterpret_cast<const BYTE*>(name),
                   static_cast<DWORD>(lstrlenA(name) + 1));
    RegSetValueExW(key, L"LastStatus", 0, REG_DWORD,
                   reinterpret_cast<const BYTE*>(&status), sizeof(status));
    RegSetValueExW(key, L"Count", 0, REG_DWORD,
                   reinterpret_cast<const BYTE*>(&sequence), sizeof(sequence));
    RegCloseKey(key);
}
}  // namespace

ULONG Initialize() noexcept {
    const ULONG error = TraceLoggingRegister(g_SamaIddDiagnostics);
    registered.store(error == ERROR_SUCCESS, std::memory_order_release);
    return error;
}

void Shutdown() noexcept {
    if (registered.exchange(false, std::memory_order_acq_rel))
        TraceLoggingUnregister(g_SamaIddDiagnostics);
}

void Stage(const char* name, LONG status, ULONGLONG value) noexcept {
    WriteBreadcrumb(name, status);
    if (!registered.load(std::memory_order_acquire)) return;
    TraceLoggingWrite(g_SamaIddDiagnostics, "DriverStage",
        TraceLoggingLevel(WINEVENT_LEVEL_INFO), TraceLoggingKeyword(1),
        TraceLoggingString(name, "Stage"),
        TraceLoggingHexUInt32(static_cast<ULONG>(status), "Status"),
        TraceLoggingUInt64(value, "Value"));
}
}

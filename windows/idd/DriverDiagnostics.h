#pragma once
#include <windows.h>

// Metadata only: never log frame contents, paths, or desktop pixels.
namespace sama::diagnostics {
// Trace registration is best effort and must never change driver status.
ULONG Initialize() noexcept;
void Shutdown() noexcept;
void Stage(const char* name, LONG status = 0, ULONGLONG value = 0) noexcept;
}

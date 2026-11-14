#pragma once
#include <windows.h>
#include <winioctl.h>
// {CAB5322A-5AE1-45F5-9321-49107946C88C}; project interface, not a USB VID/PID.
inline constexpr GUID SamaFrameInterface =
    {0xcab5322a,0x5ae1,0x45f5,{0x93,0x21,0x49,0x10,0x79,0x46,0xc8,0x8c}};
inline constexpr DWORD SamaGetFrame = CTL_CODE(FILE_DEVICE_UNKNOWN,0x801,METHOD_BUFFERED,FILE_READ_ACCESS);

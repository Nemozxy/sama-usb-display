// Privileged, read-only frame broker for SamaIddDriver.
// The driver device remains restricted to SYSTEM/Administrators. This process
// owns that handle and exposes only one bounded GetFrame operation to the
// currently signed-in interactive console user over a local named pipe.
#include "FrameCapture.h"
#include "FrameIoctl.h"
#include "FramePipeProtocol.h"
#include <setupapi.h>
#include <sddl.h>
#include <wtsapi32.h>
#include <windows.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {
constexpr wchar_t ServiceName[] = L"SamaFrameBroker";
constexpr DWORD PipeTimeoutMs = 5000;
constexpr DWORD NoSession = 0xffffffffUL;

HANDLE g_stopEvent = nullptr;
HANDLE g_activePipe = nullptr;
CRITICAL_SECTION g_pipeLock{};
bool g_pipeLockReady = false;
SERVICE_STATUS_HANDLE g_statusHandle = nullptr;
SERVICE_STATUS g_serviceStatus{};

uint32_t Read32(const std::vector<uint8_t>& packet,size_t pos) {
    uint32_t value=0;
    for (size_t i=0;i<4;++i) value |= static_cast<uint32_t>(packet[pos+i])<<(8*i);
    return value;
}
bool ValidPacket(const std::vector<uint8_t>& packet) {
    return packet.size()==sama::HeaderBytes+sama::PixelBytes &&
        !std::memcmp(packet.data(),"SAMAIDD1",8) && Read32(packet,8)==sama::Width &&
        Read32(packet,12)==sama::Height && Read32(packet,16)==sama::Width*4 &&
        Read32(packet,20)==87;
}
std::wstring DevicePath() {
    HDEVINFO info=SetupDiGetClassDevsW(&SamaFrameInterface,nullptr,nullptr,
                                       DIGCF_PRESENT|DIGCF_DEVICEINTERFACE);
    if (info==INVALID_HANDLE_VALUE) return {};
    std::wstring path;
    SP_DEVICE_INTERFACE_DATA device{};device.cbSize=sizeof(device);
    if (SetupDiEnumDeviceInterfaces(info,nullptr,&SamaFrameInterface,0,&device)) {
        DWORD size=0;
        SetupDiGetDeviceInterfaceDetailW(info,&device,nullptr,0,&size,nullptr);
        if (size>=sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) && size<=65536) {
            std::vector<uint8_t> memory(size);
            auto detail=reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W*>(memory.data());
            detail->cbSize=sizeof(*detail);
            if (SetupDiGetDeviceInterfaceDetailW(info,&device,detail,size,nullptr,nullptr))
                path=detail->DevicePath;
        }
        SP_DEVICE_INTERFACE_DATA another{};another.cbSize=sizeof(another);
        if (SetupDiEnumDeviceInterfaces(info,nullptr,&SamaFrameInterface,1,&another)) path.clear();
    }
    SetupDiDestroyDeviceInfoList(info);
    return path;
}
bool LoadMockPacket(const wchar_t* path,std::vector<uint8_t>& packet) {
    FILE* file=nullptr;_wfopen_s(&file,path,L"rb");if (!file) return false;
    packet.assign(sama::HeaderBytes+sama::PixelBytes,0);
    const size_t count=std::fread(packet.data(),1,packet.size(),file);
    const bool end=std::fgetc(file)==EOF && !std::ferror(file);
    std::fclose(file);
    if (count!=packet.size() || !end || !ValidPacket(packet)) {
        packet.clear();return false;
    }
    return true;
}
bool TokenUserSid(HANDLE token,std::vector<uint8_t>& sid) {
    DWORD needed=0;
    GetTokenInformation(token,TokenUser,nullptr,0,&needed);
    if (!needed || GetLastError()!=ERROR_INSUFFICIENT_BUFFER) return false;
    std::vector<uint8_t> buffer(needed);
    if (!GetTokenInformation(token,TokenUser,buffer.data(),needed,&needed)) return false;
    const auto* user=reinterpret_cast<const TOKEN_USER*>(buffer.data());
    const DWORD bytes=GetLengthSid(user->User.Sid);
    sid.resize(bytes);
    return CopySid(bytes,sid.data(),user->User.Sid)!=FALSE;
}
bool ProcessUserSid(DWORD pid,std::vector<uint8_t>& sid) {
    HANDLE process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,FALSE,pid);
    if (!process) return false;
    HANDLE token=nullptr;
    const bool opened=OpenProcessToken(process,TOKEN_QUERY,&token)!=FALSE;
    CloseHandle(process);
    if (!opened) return false;
    const bool result=TokenUserSid(token,sid);
    CloseHandle(token);
    return result;
}
bool CurrentUserSid(std::vector<uint8_t>& sid) {
    HANDLE token=nullptr;
    if (!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token)) return false;
    const bool result=TokenUserSid(token,sid);
    CloseHandle(token);
    return result;
}
bool ConsoleUserSid(DWORD session,std::vector<uint8_t>& sid) {
    HANDLE token=nullptr;
    if (!WTSQueryUserToken(session,&token)) return false;
    const bool result=TokenUserSid(token,sid);
    CloseHandle(token);
    return result;
}
// The pipe DACL limits callers to SYSTEM, Administrators, and INTERACTIVE
// tokens. This second check binds a connection to the exact console-session
// user in production, so another concurrently signed-in account is rejected.
bool AuthorizeClient(HANDLE pipe,bool mockMode,DWORD& error) {
    ULONG clientPid=0,clientSession=0;
    if (!GetNamedPipeClientProcessId(pipe,&clientPid) ||
        !GetNamedPipeClientSessionId(pipe,&clientSession)) {
        error=GetLastError();return false;
    }
    DWORD active=WTSGetActiveConsoleSessionId();
    if (mockMode) {
        DWORD processSession=NoSession;
        if (!ProcessIdToSessionId(GetCurrentProcessId(),&processSession)) processSession=NoSession;
        active=processSession;
    }
    if (active==NoSession || clientSession!=active) {
        error=ERROR_ACCESS_DENIED;return false;
    }
    std::vector<uint8_t> clientSid,expectedSid;
    if (!ProcessUserSid(clientPid,clientSid)) {error=GetLastError();return false;}
    const bool haveExpected=mockMode ? CurrentUserSid(expectedSid) : ConsoleUserSid(active,expectedSid);
    if (!haveExpected || !EqualSid(clientSid.data(),expectedSid.data())) {
        error=ERROR_ACCESS_DENIED;return false;
    }
    error=ERROR_SUCCESS;return true;
}
void SetActivePipe(HANDLE pipe) {
    if (!g_pipeLockReady) return;
    EnterCriticalSection(&g_pipeLock);
    g_activePipe=pipe;
    LeaveCriticalSection(&g_pipeLock);
}
void CancelActivePipe() {
    if (!g_pipeLockReady) return;
    EnterCriticalSection(&g_pipeLock);
    if (g_activePipe) CancelIoEx(g_activePipe,nullptr);
    LeaveCriticalSection(&g_pipeLock);
}
bool WaitStopped(DWORD milliseconds) {
    return g_stopEvent && WaitForSingleObject(g_stopEvent,milliseconds)==WAIT_OBJECT_0;
}
void CancelAndDrain(HANDLE pipe,OVERLAPPED& overlapped) {
    // Cancellation is only a request. Both OVERLAPPED and the caller's buffer
    // must remain alive until Windows has completed (or cancelled) the I/O.
    CancelIoEx(pipe,&overlapped);
    DWORD ignored=0;
    GetOverlappedResult(pipe,&overlapped,&ignored,TRUE);
}
bool PipeIo(HANDLE pipe,HANDLE event,bool write,void* data,DWORD bytes,DWORD& count,DWORD& error,
            ULONGLONG deadline=0) {
    if (WaitStopped(0)) {count=0;error=ERROR_OPERATION_ABORTED;return false;}
    if (!deadline) deadline=GetTickCount64()+PipeTimeoutMs;
    if (GetTickCount64()>=deadline) {count=0;error=ERROR_TIMEOUT;return false;}
    OVERLAPPED overlapped{};
    overlapped.hEvent=event;
    ResetEvent(event);
    const BOOL began=write ? WriteFile(pipe,data,bytes,&count,&overlapped)
                           : ReadFile(pipe,data,bytes,&count,&overlapped);
    if (!began) {
        error=GetLastError();
        if (error!=ERROR_IO_PENDING) return false;
        HANDLE waits[2]={event,g_stopEvent};
        const ULONGLONG now=GetTickCount64();
        const DWORD remaining=now<deadline ? static_cast<DWORD>(deadline-now) : 0;
        const DWORD wait=WaitForMultipleObjects(2,waits,FALSE,remaining);
        if (wait!=WAIT_OBJECT_0) {
            CancelAndDrain(pipe,overlapped);
            if (wait==WAIT_TIMEOUT) error=ERROR_TIMEOUT;
            else error=ERROR_OPERATION_ABORTED;
            return false;
        }
    }
    if (!GetOverlappedResult(pipe,&overlapped,&count,FALSE)) {
        error=GetLastError();return false;
    }
    error=ERROR_SUCCESS;return true;
}
bool ReadExact(HANDLE pipe,HANDLE event,void* data,size_t bytes,DWORD& error) {
    // One request owns one deadline; partial progress never renews it.
    const ULONGLONG deadline=GetTickCount64()+PipeTimeoutMs;
    auto* current=static_cast<uint8_t*>(data);
    while (bytes) {
        DWORD count=0;
        const DWORD request=static_cast<DWORD>(bytes);
        if (!PipeIo(pipe,event,false,current,request,count,error,deadline) || !count) {
            if (error==ERROR_SUCCESS) error=ERROR_HANDLE_EOF;
            return false;
        }
        if (count>request) {error=ERROR_INVALID_DATA;return false;}
        current+=count;bytes-=count;
    }
    error=ERROR_SUCCESS;return true;
}
bool WriteExact(HANDLE pipe,HANDLE event,const void* data,size_t bytes,DWORD& error,
                ULONGLONG deadline=0) {
    if (!deadline) deadline=GetTickCount64()+PipeTimeoutMs;
    auto* current=static_cast<const uint8_t*>(data);
    while (bytes) {
        DWORD count=0;
        const DWORD request=static_cast<DWORD>(bytes);
        if (!PipeIo(pipe,event,true,const_cast<uint8_t*>(current),request,count,error,deadline) || !count) {
            if (error==ERROR_SUCCESS) error=ERROR_WRITE_FAULT;
            return false;
        }
        if (count>request) {error=ERROR_INVALID_DATA;return false;}
        current+=count;bytes-=count;
    }
    error=ERROR_SUCCESS;return true;
}
bool SendResponse(HANDLE pipe,HANDLE event,DWORD status,const std::vector<uint8_t>* packet) {
    // The header and payload share a deadline, not two five-second windows.
    const ULONGLONG deadline=GetTickCount64()+PipeTimeoutMs;
    sama::pipe::Response response{{'S','A','M','A','P','I','P','1'},sama::pipe::Version,status,
        packet ? static_cast<uint32_t>(packet->size()) : 0};
    DWORD error=ERROR_SUCCESS;
    if (!WriteExact(pipe,event,&response,sizeof(response),error,deadline)) return false;
    return !packet || WriteExact(pipe,event,packet->data(),packet->size(),error,deadline);
}
void ServeConnectedClient(HANDLE pipe,HANDLE driver,const std::vector<uint8_t>* mock,bool mockMode) {
    DWORD authorizationError=ERROR_SUCCESS;
    if (!AuthorizeClient(pipe,mockMode,authorizationError)) {
        if (mockMode) std::fprintf(stderr,"mock client denied: %lu\n",authorizationError);
        return;
    }
    HANDLE event=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    if (!event) return;
    SetActivePipe(pipe);
    std::vector<uint8_t> packet(sama::HeaderBytes+sama::PixelBytes);
    while (!WaitStopped(0)) {
        sama::pipe::Request request{};
        DWORD error=ERROR_SUCCESS;
        if (!ReadExact(pipe,event,&request,sizeof(request),error)) break;
        if (!sama::pipe::IsMagic(request.magic) || request.version!=sama::pipe::Version ||
            request.command!=sama::pipe::GetFrame || request.reserved!=0) break;
        // A connection may outlive its console user (fast user switching,
        // logoff, or disconnect). Never carry connect-time authority forward
        // to a later frame request. Recheck after reading the whole request.
        if (!AuthorizeClient(pipe,mockMode,authorizationError)) break;
        DWORD status=ERROR_SUCCESS;
        if (mock) {
            std::memcpy(packet.data(),mock->data(),packet.size());
        } else {
            DWORD count=0;
            if (!DeviceIoControl(driver,SamaGetFrame,nullptr,0,packet.data(),
                                 static_cast<DWORD>(packet.size()),&count,nullptr)) {
                status=GetLastError();
            } else if (count!=packet.size() || !ValidPacket(packet)) {
                status=ERROR_INVALID_DATA;
            }
        }
        const std::vector<uint8_t>* reply=status==ERROR_SUCCESS ? &packet : nullptr;
        if (!SendResponse(pipe,event,status,reply)) break;
    }
    SetActivePipe(nullptr);
    CloseHandle(event);
}
bool ConnectPipe(HANDLE pipe,HANDLE event,DWORD& error) {
    if (WaitStopped(0)) {error=ERROR_OPERATION_ABORTED;return false;}
    OVERLAPPED overlapped{};
    overlapped.hEvent=event;
    ResetEvent(event);
    if (ConnectNamedPipe(pipe,&overlapped)) {error=ERROR_SUCCESS;return true;}
    error=GetLastError();
    if (error==ERROR_PIPE_CONNECTED) {error=ERROR_SUCCESS;return true;}
    if (error!=ERROR_IO_PENDING) return false;
    HANDLE waits[2]={event,g_stopEvent};
    const DWORD wait=WaitForMultipleObjects(2,waits,FALSE,INFINITE);
    if (wait!=WAIT_OBJECT_0) {
        CancelAndDrain(pipe,overlapped);
        error=ERROR_OPERATION_ABORTED;return false;
    }
    DWORD count=0;
    if (!GetOverlappedResult(pipe,&overlapped,&count,FALSE)) {error=GetLastError();return false;}
    error=ERROR_SUCCESS;return true;
}
void CloseClientPipe(HANDLE pipe) {
    SetActivePipe(nullptr);
    // FlushFileBuffers waits for the peer to read ALL buffered bytes, without
    // a deadline or a cancellable OVERLAPPED. A stalled client must not block
    // service stop or the next connection. Failed/incomplete replies are lost;
    // a successful client already validates its entire fixed-size response.
    DisconnectNamedPipe(pipe);
    CloseHandle(pipe);
}
int RunBroker(bool mockMode,const std::vector<uint8_t>* mock) {
    HANDLE driver=INVALID_HANDLE_VALUE;
    if (!mockMode) {
        // The service can start before its virtual display finishes booting.
        // Wait for the exact project interface; never substitute another device.
        while (!WaitStopped(0)) {
            const std::wstring path=DevicePath();
            if (!path.empty()) {
                driver=CreateFileW(path.c_str(),GENERIC_READ,0,nullptr,OPEN_EXISTING,
                                   FILE_ATTRIBUTE_NORMAL,nullptr);
                if (driver!=INVALID_HANDLE_VALUE) break;
            }
            WaitStopped(2000);
        }
        if (driver==INVALID_HANDLE_VALUE) return 0;
    }
    PSECURITY_DESCRIPTOR descriptor=nullptr;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
            L"D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;IU)",SDDL_REVISION_1,&descriptor,nullptr)) {
        if (driver!=INVALID_HANDLE_VALUE) CloseHandle(driver);return 7;
    }
    SECURITY_ATTRIBUTES attributes{sizeof(attributes),descriptor,FALSE};
    HANDLE connectEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    if (!connectEvent) {LocalFree(descriptor);if (driver!=INVALID_HANDLE_VALUE) CloseHandle(driver);return 8;}
    int result=0;
    while (!WaitStopped(0)) {
        HANDLE pipe=CreateNamedPipeW(sama::pipe::Name,PIPE_ACCESS_DUPLEX|FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT|PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_UNLIMITED_INSTANCES,65536,4096,PipeTimeoutMs,&attributes);
        if (pipe==INVALID_HANDLE_VALUE) {result=9;break;}
        SetActivePipe(pipe);
        DWORD error=ERROR_SUCCESS;
        if (!ConnectPipe(pipe,connectEvent,error)) {
            SetActivePipe(nullptr);CloseHandle(pipe);
            if (WaitStopped(0)) break;
            result=10;break;
        }
        ServeConnectedClient(pipe,driver,mock,mockMode);
        CloseClientPipe(pipe);
    }
    CloseHandle(connectEvent);LocalFree(descriptor);
    if (driver!=INVALID_HANDLE_VALUE) CloseHandle(driver);
    return result;
}
BOOL WINAPI ConsoleCtrl(DWORD type) {
    if (type==CTRL_C_EVENT || type==CTRL_BREAK_EVENT || type==CTRL_CLOSE_EVENT) {
        if (g_stopEvent) SetEvent(g_stopEvent);
        CancelActivePipe();
        return TRUE;
    }
    return FALSE;
}
void SetServiceStatus(DWORD state,DWORD exitCode=ERROR_SUCCESS,DWORD waitHint=0) {
    if (!g_statusHandle) return;
    g_serviceStatus.dwServiceType=SERVICE_WIN32_OWN_PROCESS;
    g_serviceStatus.dwCurrentState=state;
    g_serviceStatus.dwWin32ExitCode=exitCode;
    g_serviceStatus.dwWaitHint=waitHint;
    g_serviceStatus.dwControlsAccepted=(state==SERVICE_RUNNING) ? SERVICE_ACCEPT_STOP|SERVICE_ACCEPT_SHUTDOWN : 0;
    SetServiceStatus(g_statusHandle,&g_serviceStatus);
}
DWORD WINAPI ServiceHandler(DWORD control,DWORD,void*,void*) {
    if (control==SERVICE_CONTROL_STOP || control==SERVICE_CONTROL_SHUTDOWN) {
        SetServiceStatus(SERVICE_STOP_PENDING,ERROR_SUCCESS,10000);
        if (g_stopEvent) SetEvent(g_stopEvent);
        CancelActivePipe();
        return NO_ERROR;
    }
    if (control==SERVICE_CONTROL_INTERROGATE) {SetServiceStatus(g_serviceStatus.dwCurrentState);return NO_ERROR;}
    return ERROR_CALL_NOT_IMPLEMENTED;
}
void WINAPI ServiceMain(DWORD,LPWSTR*) {
    g_statusHandle=RegisterServiceCtrlHandlerExW(ServiceName,ServiceHandler,nullptr);
    if (!g_statusHandle) return;
    SetServiceStatus(SERVICE_START_PENDING,ERROR_SUCCESS,10000);
    g_stopEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    InitializeCriticalSection(&g_pipeLock);g_pipeLockReady=true;
    if (!g_stopEvent) {
        SetServiceStatus(SERVICE_STOPPED,GetLastError());return;
    }
    SetServiceStatus(SERVICE_RUNNING);
    const int result=RunBroker(false,nullptr);
    SetServiceStatus(SERVICE_STOPPED,result==0?ERROR_SUCCESS:static_cast<DWORD>(result));
    g_pipeLockReady=false;DeleteCriticalSection(&g_pipeLock);
    CloseHandle(g_stopEvent);g_stopEvent=nullptr;
}
} // namespace

std::wstring Utf8ToWide(const char* text) {
    const int bytes=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text,-1,nullptr,0);
    if (bytes<=0) return {};
    std::wstring value(static_cast<size_t>(bytes-1),L'\0');
    if (!MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text,-1,value.data(),bytes)) return {};
    return value;
}
int main(int argc,char** argv) {
    if (argc==2 && !std::strcmp(argv[1],"--service")) {
        SERVICE_TABLE_ENTRYW table[]={{const_cast<LPWSTR>(ServiceName),ServiceMain},{nullptr,nullptr}};
        if (!StartServiceCtrlDispatcherW(table)) return static_cast<int>(GetLastError());
        return 0;
    }
    if (argc==3 && !std::strcmp(argv[1],"--console-mock")) {
        std::vector<uint8_t> packet;
        const std::wstring path=Utf8ToWide(argv[2]);
        if (path.empty() || !LoadMockPacket(path.c_str(),packet)) {
            std::fprintf(stderr,"mock frame is absent, truncated, or invalid\n");return 3;
        }
        g_stopEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
        InitializeCriticalSection(&g_pipeLock);g_pipeLockReady=true;
        SetConsoleCtrlHandler(ConsoleCtrl,TRUE);
        std::fprintf(stderr,"SAMA_FRAME_BROKER_MOCK_READY\n");std::fflush(stderr);
        const int result=RunBroker(true,&packet);
        SetConsoleCtrlHandler(ConsoleCtrl,FALSE);
        g_pipeLockReady=false;DeleteCriticalSection(&g_pipeLock);
        if (g_stopEvent) CloseHandle(g_stopEvent);
        return result;
    }
    std::fprintf(stderr,"usage: SamaFrameBroker.exe --service | --console-mock packet\n");
    return 2;
}

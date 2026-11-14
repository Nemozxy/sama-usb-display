// stdout is the same SAMAIDD1 byte stream consumed by the Python bridge.
#include "FrameCapture.h"
#include "FrameIoctl.h"
#include "FramePipeProtocol.h"
#include <setupapi.h>
#include <io.h>
#include <fcntl.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>
#include <string>
#include <cerrno>

static bool Limit(const wchar_t* text,unsigned long maximum,unsigned long& value) {
    if (!text || !*text || *text==L'-' || *text==L'+') return false;
    wchar_t* end=nullptr;errno=0;value=std::wcstoul(text,&end,10);
    return !errno && end && !*end && value>=1 && value<=maximum;
}
static void Write64(std::vector<uint8_t>& packet,size_t pos,uint64_t value) {
    for (size_t i=0;i<8;++i) packet[pos+i]=static_cast<uint8_t>(value>>(i*8));
}
static ULONGLONG NextSample(ULONGLONG started,ULONGLONG finished,unsigned long fps) {
    // Skip elapsed slots after either a frame OR an empty mailbox response.
    // Empty polling must not bypass the user's source-rate/CPU budget.
    return started+(((finished-started)*fps/1000ULL+1)*1000ULL+fps-1)/fps;
}
static bool WriteFrame(const std::vector<uint8_t>& packet) {
    return std::fwrite(packet.data(),1,packet.size(),stdout)==packet.size() && std::fflush(stdout)==0;
}
static uint32_t Read32(const std::vector<uint8_t>& packet,size_t pos) {
    uint32_t value=0;for (size_t i=0;i<4;++i) value |= static_cast<uint32_t>(packet[pos+i])<<(8*i);
    return value;
}
static bool Valid(const std::vector<uint8_t>& packet) {
    return packet.size()==sama::HeaderBytes+sama::PixelBytes &&
        !std::memcmp(packet.data(),"SAMAIDD1",8) && Read32(packet,8)==sama::Width &&
        Read32(packet,12)==sama::Height && Read32(packet,16)==sama::Width*4 && Read32(packet,20)==87;
}
static std::wstring DevicePath() {
    HDEVINFO info=SetupDiGetClassDevsW(&SamaFrameInterface,nullptr,nullptr,DIGCF_PRESENT|DIGCF_DEVICEINTERFACE);
    if (info==INVALID_HANDLE_VALUE) return {};
    std::wstring path;
    SP_DEVICE_INTERFACE_DATA device{};device.cbSize=sizeof(device);
    if (SetupDiEnumDeviceInterfaces(info,nullptr,&SamaFrameInterface,0,&device)) {
        DWORD size=0;SetupDiGetDeviceInterfaceDetailW(info,&device,nullptr,0,&size,nullptr);
        if (size>=sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) && size<=65536) {
            std::vector<uint8_t> memory(size);
            auto detail=reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W*>(memory.data());
            detail->cbSize=sizeof(*detail);
            if (SetupDiGetDeviceInterfaceDetailW(info,&device,detail,size,nullptr,nullptr)) path=detail->DevicePath;
        }
        // Refuse ambiguity rather than silently picking one display adapter.
        SP_DEVICE_INTERFACE_DATA another{};another.cbSize=sizeof(another);
        if (SetupDiEnumDeviceInterfaces(info,nullptr,&SamaFrameInterface,1,&another)) path.clear();
    }
    SetupDiDestroyDeviceInfoList(info);
    return path;
}
static bool BrokerIo(HANDLE handle,HANDLE event,bool write,void* data,DWORD bytes,
                     DWORD& count,DWORD& error,ULONGLONG deadline) {
    const ULONGLONG now=GetTickCount64();
    if (now>=deadline) {count=0;error=ERROR_TIMEOUT;return false;}
    OVERLAPPED overlapped{};overlapped.hEvent=event;ResetEvent(event);
    const BOOL began=write ? WriteFile(handle,data,bytes,&count,&overlapped)
                           : ReadFile(handle,data,bytes,&count,&overlapped);
    if (!began) {
        error=GetLastError();
        if (error!=ERROR_IO_PENDING) return false;
        const ULONGLONG current=GetTickCount64();
        const DWORD remaining=current<deadline ? static_cast<DWORD>(deadline-current) : 0;
        const DWORD wait=WaitForSingleObject(event,remaining);
        if (wait!=WAIT_OBJECT_0) {
            // CancelIoEx merely requests cancellation. Do not release either
            // the OVERLAPPED or its buffer before completion has been drained.
            CancelIoEx(handle,&overlapped);
            DWORD ignored=0;GetOverlappedResult(handle,&overlapped,&ignored,TRUE);
            error=wait==WAIT_TIMEOUT ? ERROR_TIMEOUT : ERROR_OPERATION_ABORTED;
            return false;
        }
    }
    if (!GetOverlappedResult(handle,&overlapped,&count,FALSE)) {error=GetLastError();return false;}
    error=ERROR_SUCCESS;return true;
}
static bool ReadAll(HANDLE handle,HANDLE event,void* data,size_t bytes,DWORD& error,ULONGLONG deadline) {
    auto* current=static_cast<uint8_t*>(data);
    while (bytes) {
        DWORD count=0;
        if (!BrokerIo(handle,event,false,current,static_cast<DWORD>(bytes),count,error,deadline) || !count) {
            if (!error) error=ERROR_HANDLE_EOF;return false;
        }
        if (count>bytes) {error=ERROR_INVALID_DATA;return false;}
        current+=count;bytes-=count;
    }
    error=ERROR_SUCCESS;return true;
}
static bool WriteAll(HANDLE handle,HANDLE event,const void* data,size_t bytes,DWORD& error,ULONGLONG deadline) {
    auto* current=static_cast<const uint8_t*>(data);
    while (bytes) {
        DWORD count=0;
        if (!BrokerIo(handle,event,true,const_cast<uint8_t*>(current),static_cast<DWORD>(bytes),count,error,deadline) || !count) {
            if (!error) error=ERROR_WRITE_FAULT;return false;
        }
        if (count>bytes) {error=ERROR_INVALID_DATA;return false;}
        current+=count;bytes-=count;
    }
    error=ERROR_SUCCESS;return true;
}
// One request retrieves at most one already captured mailbox frame. No path,
// method, buffer pointer, or arbitrary IOCTL can cross the broker boundary.
static bool ReadBrokerFrame(HANDLE broker,std::vector<uint8_t>& packet,DWORD& status,ULONGLONG sessionDeadline=0) {
    const ULONGLONG limit=GetTickCount64()+5000;
    const ULONGLONG deadline=sessionDeadline && sessionDeadline<limit ? sessionDeadline : limit;
    HANDLE event=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    if (!event) {status=GetLastError();return false;}
    struct EventOwner {HANDLE value;~EventOwner(){CloseHandle(value);}} owner{event};
    const auto request=sama::pipe::MakeFrameRequest();
    DWORD error=ERROR_SUCCESS;
    if (!WriteAll(broker,event,&request,sizeof(request),error,deadline)) {status=error;return false;}
    sama::pipe::Response response{};
    if (!ReadAll(broker,event,&response,sizeof(response),error,deadline)) {status=error;return false;}
    if (!sama::pipe::IsMagic(response.magic) || response.version!=sama::pipe::Version) {
        status=ERROR_INVALID_DATA;return false;
    }
    if (response.status!=ERROR_SUCCESS) {
        status=response.payload_bytes==0 ? response.status : ERROR_INVALID_DATA;
        return false;
    }
    if (response.payload_bytes!=packet.size() || !ReadAll(broker,event,packet.data(),packet.size(),error,deadline)) {
        status=error?error:ERROR_INVALID_DATA;return false;
    }
    status=ERROR_SUCCESS;return true;
}
int wmain(int argc,wchar_t** argv) {
    if (_setmode(_fileno(stdout),_O_BINARY)==-1) return 2;
    if (argc==3 && !std::wcscmp(argv[1],L"--check-file")) {
        FILE* file=nullptr;_wfopen_s(&file,argv[2],L"rb");if (!file) return 3;
        std::vector<uint8_t> packet(sama::HeaderBytes+sama::PixelBytes);
        const size_t count=std::fread(packet.data(),1,packet.size(),file);
        const bool end=std::fgetc(file)==EOF && !std::ferror(file);
        std::fclose(file);
        return count==packet.size() && end && Valid(packet) && WriteFrame(packet) ? 0 : 4;
    }
    const bool replay=argc>=3 && !std::wcscmp(argv[1],L"--replay-file");
    const bool driver=argc>=2 && !std::wcscmp(argv[1],L"--driver");
    const bool broker=argc>=2 && !std::wcscmp(argv[1],L"--broker");
    unsigned long seconds=30,maximumFrames=120,sourceFps=30;
    int first=replay ? 3 : 2;
    if (!replay && !driver && !broker) {
        std::fprintf(stderr,"usage: frame-client --driver | --broker | --check-file packet | --replay-file packet [--seconds 1..86400 --max-frames 1..2592000 --source-fps 1..30]\n");return 2;
    }
    for (int i=first;i<argc;i+=2) {
        if (i+1>=argc) return 2;
        if (!std::wcscmp(argv[i],L"--seconds")) { if (!Limit(argv[i+1],86400,seconds)) return 2; }
        else if (!std::wcscmp(argv[i],L"--max-frames")) { if (!Limit(argv[i+1],2592000,maximumFrames)) return 2; }
        else if (!std::wcscmp(argv[i],L"--source-fps")) { if (!Limit(argv[i+1],30,sourceFps)) return 2; }
        else return 2;
    }
    // Explicit, bounded consumer; replay never enumerates/opens the driver.
    std::vector<uint8_t> packet(sama::HeaderBytes+sama::PixelBytes);
    HANDLE device=INVALID_HANDLE_VALUE,brokerPipe=INVALID_HANDLE_VALUE;
    if (replay) {
        FILE* file=nullptr;_wfopen_s(&file,argv[2],L"rb");if (!file) return 3;
        const size_t count=std::fread(packet.data(),1,packet.size(),file);
        const bool end=std::fgetc(file)==EOF && !std::ferror(file);std::fclose(file);
        if (count!=packet.size() || !end || !Valid(packet)) return 4;
    } else if (driver) {
        auto path=DevicePath();
        if (path.empty()) { std::fprintf(stderr,"unique SAMA driver interface not found\n");return 5; }
        device=CreateFileW(path.c_str(),GENERIC_READ,0,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
        if (device==INVALID_HANDLE_VALUE) {
            std::fprintf(stderr,"open driver failed: %lu\n",GetLastError());return 6;
        }
    } else {
        if (!WaitNamedPipeW(sama::pipe::Name,5000)) {
            std::fprintf(stderr,"open frame broker failed: %lu\n",GetLastError());return 6;
        }
        brokerPipe=CreateFileW(sama::pipe::Name,GENERIC_READ|GENERIC_WRITE,0,nullptr,
                               OPEN_EXISTING,FILE_FLAG_OVERLAPPED,nullptr);
        if (brokerPipe==INVALID_HANDLE_VALUE) {
            std::fprintf(stderr,"open frame broker failed: %lu\n",GetLastError());return 6;
        }
    }
    const ULONGLONG started=GetTickCount64(),until=started+static_cast<ULONGLONG>(seconds)*1000;
    ULONGLONG nextSample=started;size_t frames=0;int result=0;
    while (GetTickCount64()<until && frames<maximumFrames) {
        const ULONGLONG now=GetTickCount64();
        if (now<nextSample) {
            const ULONGLONG waitUntil=nextSample<until ? nextSample : until;
            if (waitUntil>now) Sleep(static_cast<DWORD>(waitUntil-now));
            continue;
        }
        DWORD count=0;
        if (replay) {
            Write64(packet,24,frames+1);Write64(packet,32,GetTickCount64()*1000000ULL);
            count=static_cast<DWORD>(packet.size());
        } else if (broker) {
            DWORD status=ERROR_SUCCESS;
            if (!ReadBrokerFrame(brokerPipe,packet,status,until)) {
                if (status==ERROR_NO_MORE_ITEMS) {nextSample=NextSample(started,GetTickCount64(),sourceFps);continue;}
                std::fprintf(stderr,"broker frame failed: %lu\n",status);result=7;break;
            }
            count=static_cast<DWORD>(packet.size());
        } else if (!DeviceIoControl(device,SamaGetFrame,nullptr,0,packet.data(),static_cast<DWORD>(packet.size()),&count,nullptr)) {
            const DWORD error=GetLastError();
            if (error==ERROR_NO_MORE_ITEMS) {nextSample=NextSample(started,GetTickCount64(),sourceFps);continue;}
            std::fprintf(stderr,"frame ioctl failed: %lu\n",error);result=7;break;
        }
        if (count!=packet.size() || !Valid(packet) || !WriteFrame(packet)) { result=8;break; }
        ++frames;
        // Skip elapsed sample slots: no catch-up burst when a pipe/USB consumer
        // is slow. The driver mailbox retains the newest unconsumed desktop.
        const ULONGLONG finished=GetTickCount64();
        nextSample=NextSample(started,finished,sourceFps);
    }
    if (device!=INVALID_HANDLE_VALUE) CloseHandle(device);
    if (brokerPipe!=INVALID_HANDLE_VALUE) CloseHandle(brokerPipe);
    std::fprintf(stderr,"frames=%zu result=%d\n",frames,result);
    return result;
}

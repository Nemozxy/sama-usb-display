#include "FrameCapture.h"
#include <new>

namespace sama {
HRESULT FrameCapture::Capture(ID3D11Device* device, ID3D11DeviceContext* context,
        ID3D11Texture2D* source, HANDLE stop, uint64_t sequence,
        std::shared_ptr<const std::vector<uint8_t>>& packet) {
    packet.reset();
    if (!device || !context || !source) return E_INVALIDARG;
    if (stop && WaitForSingleObject(stop,0) == WAIT_OBJECT_0) return E_ABORT;
    D3D11_TEXTURE2D_DESC desc{};
    source->GetDesc(&desc);
    if (desc.Width != Width || desc.Height != Height || desc.MipLevels != 1 ||
        desc.ArraySize != 1 || desc.SampleDesc.Count != 1 ||
        desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM) return E_INVALIDARG;
    if (!staging_.Get()) {
        desc.Usage = D3D11_USAGE_STAGING;
        desc.BindFlags = 0;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        desc.MiscFlags = 0;
        HRESULT hr = device->CreateTexture2D(&desc,nullptr,staging_.Put());
        if (FAILED(hr)) return hr;
    }
    if (!ready_.Get()) {
        D3D11_QUERY_DESC query{D3D11_QUERY_EVENT,0};
        HRESULT hr = device->CreateQuery(&query,ready_.Put());
        if (FAILED(hr)) return hr;
    }
    context->CopyResource(staging_.Get(),source);
    context->End(ready_.Get());
    context->Flush();
    const ULONGLONG deadline = GetTickCount64()+250;
    for (;;) {
        if (stop && WaitForSingleObject(stop,0) == WAIT_OBJECT_0) return E_ABORT;
        BOOL done = FALSE;
        HRESULT hr = context->GetData(ready_.Get(),&done,sizeof(done),D3D11_ASYNC_GETDATA_DONOTFLUSH);
        if (FAILED(hr)) return hr;
        if (hr == S_OK && done) break;
        if (GetTickCount64() >= deadline) return HRESULT_FROM_WIN32(ERROR_TIMEOUT);
        Sleep(1);
    }
    D3D11_MAPPED_SUBRESOURCE mapped{};
    HRESULT hr = context->Map(staging_.Get(),0,D3D11_MAP_READ,D3D11_MAP_FLAG_DO_NOT_WAIT,&mapped);
    if (FAILED(hr)) return hr;
    struct AutoUnmap {
        ID3D11DeviceContext* context;
        ID3D11Resource* resource;
        ~AutoUnmap() { context->Unmap(resource,0); }
    } unmap{context,staging_.Get()};
    if (!mapped.pData || mapped.RowPitch < Width*4) return E_UNEXPECTED;
    try {
        // Tight rows: never leak uninitialised staging padding into IPC.
        auto bytes = std::make_shared<std::vector<uint8_t>>(HeaderBytes+PixelBytes);
        std::memcpy(bytes->data(),"SAMAIDD1",8);
        Put32(*bytes,8,Width);Put32(*bytes,12,Height);Put32(*bytes,16,Width*4);
        Put32(*bytes,20,static_cast<uint32_t>(DXGI_FORMAT_B8G8R8A8_UNORM));
        Put64(*bytes,24,sequence);Put64(*bytes,32,QpcNanoseconds());
        auto src = static_cast<const uint8_t*>(mapped.pData);
        for (size_t row=0;row<Height;++row)
            std::memcpy(bytes->data()+HeaderBytes+row*Width*4,src+row*mapped.RowPitch,Width*4);
        packet = std::move(bytes);
    } catch (const std::bad_alloc&) {
        return E_OUTOFMEMORY;
    }
    return S_OK;
}
} // namespace sama

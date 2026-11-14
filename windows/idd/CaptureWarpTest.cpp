// Real Windows D3D11/WARP, generated pixels only. No desktop/driver/USB access.
#include "FrameCapture.h"
#include <cstdio>
#include <stdexcept>

static void Check(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
static void Hr(HRESULT value, const char* message) {
    if (FAILED(value)) {
        std::fprintf(stderr,"%s HRESULT=%08lx\n",message,static_cast<unsigned long>(value));
        throw std::runtime_error(message);
    }
}

int main(int argc, char** argv) {
    try {
        Check(argc==2,"expected output packet path");
        sama::ComOwner<ID3D11Device> device;
        sama::ComOwner<ID3D11DeviceContext> context;
        D3D_FEATURE_LEVEL level{};
        Hr(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                            nullptr,0,D3D11_SDK_VERSION,device.Put(),&level,context.Put()),"create WARP");
        D3D11_TEXTURE2D_DESC desc{};
        desc.Width=sama::Width;desc.Height=sama::Height;desc.MipLevels=1;desc.ArraySize=1;
        desc.Format=DXGI_FORMAT_B8G8R8A8_UNORM;desc.SampleDesc.Count=1;
        desc.Usage=D3D11_USAGE_DEFAULT;desc.BindFlags=D3D11_BIND_SHADER_RESOURCE;
        constexpr UINT pitch=3072;
        std::vector<uint8_t> source(static_cast<size_t>(pitch)*sama::Height,0xee);
        for (UINT row=0;row<sama::Height;++row) for (UINT col=0;col<sama::Width;++col) {
            const size_t off=static_cast<size_t>(row)*pitch+col*4;
            source[off]=static_cast<uint8_t>(col%256);
            source[off+1]=static_cast<uint8_t>(row%256);
            source[off+2]=static_cast<uint8_t>((col+row)%256);
            source[off+3]=255;
        }
        D3D11_SUBRESOURCE_DATA data{source.data(),pitch,0};
        sama::ComOwner<ID3D11Texture2D> texture;
        Hr(device->CreateTexture2D(&desc,&data,texture.Put()),"create texture");
        sama::FrameCapture capture;
        std::shared_ptr<const std::vector<uint8_t>> packet, next;
        Hr(capture.Capture(device.Get(),context.Get(),texture.Get(),nullptr,1,packet),"capture");
        Check(packet && packet->size()==sama::HeaderBytes+sama::PixelBytes,"packet size");
        for (UINT row=0;row<sama::Height;++row)
            Check(!std::memcmp(packet->data()+sama::HeaderBytes+static_cast<size_t>(row)*sama::Width*4,
                               source.data()+static_cast<size_t>(row)*pitch,sama::Width*4),"pixel mismatch");
        std::fill(source.begin(),source.end(),uint8_t{0});
        context->UpdateSubresource(texture.Get(),0,nullptr,source.data(),pitch,0);
        Hr(capture.Capture(device.Get(),context.Get(),texture.Get(),nullptr,2,next),"capture reused staging");
        Check(next->at(sama::HeaderBytes+3)==0 && packet->at(sama::HeaderBytes+3)==255,"source lifetime");
        sama::LatestPacket mailbox;
        Check(mailbox.Put(1,packet) && mailbox.Put(2,next),"mailbox insert");
        Check(!mailbox.Put(1,packet) && mailbox.overwritten==1,"mailbox stale/overwrite");
        Check(mailbox.Take()==next && !mailbox.Take(),"mailbox bounded");
        HANDLE stop=CreateEventW(nullptr,TRUE,TRUE,nullptr);
        Check(stop!=nullptr,"stop event");
        std::shared_ptr<const std::vector<uint8_t>> rejected;
        HRESULT cancelled=capture.Capture(device.Get(),context.Get(),texture.Get(),stop,3,rejected);
        CloseHandle(stop);
        Check(cancelled==E_ABORT && !rejected,"cancel publishes no packet");
        desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM;
        sama::ComOwner<ID3D11Texture2D> wrong;
        Hr(device->CreateTexture2D(&desc,nullptr,wrong.Put()),"wrong format texture");
        Check(capture.Capture(device.Get(),context.Get(),wrong.Get(),nullptr,4,rejected)==E_INVALIDARG && !rejected,
              "wrong format rejected");
        FILE* file=nullptr;fopen_s(&file,argv[1],"wb");Check(file!=nullptr,"open output");
        size_t written=std::fwrite(packet->data(),1,packet->size(),file);
        int closed=std::fclose(file);
        Check(written==packet->size() && closed==0,"write packet");
        std::printf("WARP_CAPTURE_OK bytes=%zu feature=%x frames=2 overwritten=1\n",packet->size(),level);
        return 0;
    } catch (const std::exception& exc) {
        std::fprintf(stderr,"FAIL: %s\n",exc.what());return 1;
    }
}

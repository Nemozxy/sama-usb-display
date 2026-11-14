# SAMA USB Display — 非官方研究型驱动

**[English README](README_EN.md)**

面向已测试的 **6.5 英寸、720×1568 SAMA/TURZX USB 竖屏副屏**的开源、非官方 Windows 驱动与主机工具集。该设备采用全志 T113-S3。

An open-source, unofficial Windows driver and host toolkit for the tested **6.5-inch 720×1568 SAMA/TURZX USB portrait panel** using an Allwinner T113-S3.

本仓库是对更大规模私有研究工作区的干净公开重写，包含自主编写的主机工具、协议文档、清洗后的测量数据，以及基于 Microsoft 示例修改的 IddCx 驱动。不包含原厂安装程序、固件、设备镜像、私有证书、个人媒体或原始桌面截图。

## 目前实现的功能

- Windows UMDF 2.25 / IddCx 虚拟显示器，提供 720×1568、30 Hz 输入模式。
- 使用 D3D11 捕获帧，并编码为带版本的 `SAMAIDD1` 二进制数据包。
- 支持直接特权读取，或由 LocalSystem 代理按请求授权控制台会话读取。
- 通过副屏的 CDC-ACM 接口，使用 Python 显示系统监控面板或直接发送图片。
- 将媒体写入副屏固定存储槽，支持主机端预处理、大小/MD5 校验、带返回检查的 `sync`、原子替换和上一版备份；图片和视频由 T113 本地解码显示或循环播放。自定义视频已通过一次手动冷启动检查。
- 支持整帧 `cmd 200` 输出，以及需要显式开启的 `cmd 204` 差分输出。
- 最新帧优先队列和有界编码/发送重叠，避免旧帧堆积。
- 离线模拟运行、合成 WARP 捕获、协议测试和原生并发测试。

## 已测试设备

| 属性 | 已测试值 |
|---|---|
| 副屏 | 6.5 英寸竖屏，720×1568 |
| 图像接口 | USB `1D6B:A065`，CDC-ACM，High-Speed |
| 报告的固件身份 | `chs_65inch.dev1_rom1.91` |
| 像素格式 | BGRA，每像素 4 字节 |
| 另一 USB 接口 | `1A86:CA65`，不用于图像传输 |

COM 编号由 Windows 分配，不是稳定标识。主机通过 USB VID/PID 查找图像接口。本仓库不声明支持其他副屏、其他 ROM 版本或早期加密的 6.2 英寸协议系列。

## 快速开始：仅离线验证

环境要求：Windows 10/11 x64、Python 3.12、带 MSVC x64 的 Visual Studio Build Tools、Windows SDK，以及用于示例命令的 PowerShell 7。

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python -m pip install -r host/requirements-dev.txt
pwsh -NoProfile -File windows/build-host.ps1
./build/host/capture-warp-test.exe ./build/host/warp.frame
./build/host/mailbox-stress-test.exe
$env:PYTHONPATH = 'host'
./.venv/Scripts/python -m pytest -q --basetemp build/pytest-tmp
```

在不打开串口的情况下预览 GUI：

```powershell
./.venv/Scripts/python host/dashboard_app.py --mock
```

使用模拟传输，将真实原生帧数据包送入完整 Python 发送链路：

```powershell
./.venv/Scripts/python host/idd_run.py --packet-file build/host/warp.frame --run
```

添加 `--differ` 可测试差分编码器。使用离线数据包且不带 `--usb` 时，上述命令不会访问驱动或 USB 设备。

## 驱动构建与安装

除 MSVC 和 Windows SDK 外，驱动还需要 WDK 头文件和库，详见[构建与验证文档](docs/building.md)。构建脚本不会安装驱动、创建证书、修改信任设置、启用测试签名或重启系统。

公开预览版特意**不提供私钥和一键系统安装程序**。安装自签名显示驱动会降低正常的驱动签名安全边界，需要管理员权限、明确的签名/信任决定，通常还需要重启。请不要在无法恢复显示配置的机器上安装。

## 性能与成熟度

当前差分候选参数为 `--differ-change-threshold 32 --differ-reinit-interval 32`。一次清洗后的 3,600 帧主机发送测试在 254.691 秒内完成，稳定阶段为 **13.87 次更新/秒**，全程一个连接且没有重连。这不是 30/60 fps 承诺，不是 LCD 光学测量，也不是 30 分钟稳定性或视觉质量认证。详见[性能证据](docs/performance.md)。

整帧路径明显更慢，因为每帧在线路上携带约 4.53 MB 数据。Windows 的 30 Hz 模式只控制帧产生节奏，不表示实体副屏能显示 30 fps。

请将其视为研究预览版，而不是生产级签名驱动。睡眠/唤醒、所有 USB 恢复场景、非零阈值的视觉质量、长时间差分运行，以及广泛的 Windows 版本和硬件兼容性仍未得到完整验证。

## 文档

- [统一 GUI：监控面板、板载媒体与 Windows 桌面](docs/usage.md)
- [协议与硬件研究结果](docs/protocol.md)
- [构建与验证](docs/building.md)
- [性能证据](docs/performance.md)
- [研究发现与现有限制](docs/research-findings.md)
- [清洗后的性能数据](evidence/differ-3600-summary.json)与[板载媒体验证数据](evidence/onboard-media-validation.json)
- [贡献指南、硬件边界与隐私规则](CONTRIBUTING.md)

## 许可证与归属

项目原创代码采用 MIT 许可证。基于 Microsoft 示例修改的间接显示驱动仍采用 MS-PL，详见 `windows/idd/driver/LICENSE` 和 `NOTICE`。SAMA、TURZX、Microsoft 及其他名称归各自所有者所有。本项目与设备厂商没有隶属关系，也未获得其背书。

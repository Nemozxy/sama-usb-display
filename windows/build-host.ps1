[CmdletBinding()]
param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$output = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $root 'build/host' }
$vswhere = Join-Path ([Environment]::GetEnvironmentVariable('ProgramFiles(x86)')) 'Microsoft Visual Studio/Installer/vswhere.exe'
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vs) { throw 'Visual Studio with the MSVC x64 C++ toolset is required.' }
$version = (Get-Content -LiteralPath (Join-Path $vs 'VC/Auxiliary/Build/Microsoft.VCToolsVersion.default.txt')).Trim()
$vc = Join-Path $vs "VC/Tools/MSVC/$version"
$cl = Join-Path $vc 'bin/Hostx64/x64/cl.exe'
$sdk = Join-Path ([Environment]::GetEnvironmentVariable('ProgramFiles(x86)')) 'Windows Kits/10'
$sdkVersion = Get-ChildItem -LiteralPath (Join-Path $sdk 'Include') -Directory |
    Where-Object { $_.Name -like '10.0.*' } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1 -ExpandProperty Name
if (!$sdkVersion) { throw 'A Windows 10/11 SDK is required.' }
New-Item -ItemType Directory -Path $output -Force | Out-Null
$oldInclude, $oldLib = $env:INCLUDE, $env:LIB
try {
    $env:INCLUDE = @((Join-Path $vc 'include'), (Join-Path $sdk "Include/$sdkVersion/um"),
        (Join-Path $sdk "Include/$sdkVersion/shared"), (Join-Path $sdk "Include/$sdkVersion/ucrt"),
        (Join-Path $sdk "Include/$sdkVersion/winrt")) -join ';'
    $env:LIB = @((Join-Path $vc 'lib/x64'), (Join-Path $sdk "Lib/$sdkVersion/um/x64"),
        (Join-Path $sdk "Lib/$sdkVersion/ucrt/x64")) -join ';'
    function Build-Native([string]$name, [string[]]$sources, [string[]]$libraries) {
        $arguments = @('/nologo', '/std:c++17', '/EHsc', '/O2', '/W4', '/WX', '/DUNICODE', '/D_UNICODE',
            "/Fo$output/", "/Fe$output/$name") + $sources + '/link' + '/INCREMENTAL:NO' + $libraries
        & $cl @arguments
        if ($LASTEXITCODE -ne 0) { throw "Failed to build $name (exit $LASTEXITCODE)." }
    }
    $source = Join-Path $root 'windows/idd'
    Build-Native 'capture-warp-test.exe' @((Join-Path $source 'FrameCapture.cpp'), (Join-Path $source 'CaptureWarpTest.cpp')) @('d3d11.lib', 'dxgi.lib', 'ole32.lib')
    Build-Native 'mailbox-stress-test.exe' @((Join-Path $source 'MailboxStressTest.cpp')) @()
    Build-Native 'frame-client.exe' @((Join-Path $source 'FrameClient.cpp')) @('setupapi.lib')
    Build-Native 'SamaFrameBroker.exe' @((Join-Path $source 'FrameBroker.cpp')) @('setupapi.lib', 'advapi32.lib', 'userenv.lib', 'wtsapi32.lib')
    Get-ChildItem -LiteralPath $output -Filter '*.exe' | Get-FileHash -Algorithm SHA256
} finally {
    $env:INCLUDE, $env:LIB = $oldInclude, $oldLib
}

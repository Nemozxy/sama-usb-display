[CmdletBinding()]
param([string]$WdkRoot, [string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$output = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $root 'build/driver' }
if (!$WdkRoot) { $WdkRoot = [Environment]::GetEnvironmentVariable('WDKContentRoot') }
if (!$WdkRoot) { $WdkRoot = Join-Path ([Environment]::GetEnvironmentVariable('ProgramFiles(x86)')) 'Windows Kits/10' }
$vswhere = Join-Path ([Environment]::GetEnvironmentVariable('ProgramFiles(x86)')) 'Microsoft Visual Studio/Installer/vswhere.exe'
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vs) { throw 'Visual Studio with the MSVC x64 C++ toolset is required.' }
$version = (Get-Content -LiteralPath (Join-Path $vs 'VC/Auxiliary/Build/Microsoft.VCToolsVersion.default.txt')).Trim()
$vc = Join-Path $vs "VC/Tools/MSVC/$version"
$cl = Join-Path $vc 'bin/Hostx64/x64/cl.exe'
$dumpbin = Join-Path $vc 'bin/Hostx64/x64/dumpbin.exe'
$sdk = Join-Path ([Environment]::GetEnvironmentVariable('ProgramFiles(x86)')) 'Windows Kits/10'
$sdkVersion = Get-ChildItem -LiteralPath (Join-Path $sdk 'Include') -Directory |
    Where-Object { $_.Name -like '10.0.*' } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1 -ExpandProperty Name
$includes = @((Join-Path $vc 'include'), (Join-Path $sdk "Include/$sdkVersion/um"),
    (Join-Path $sdk "Include/$sdkVersion/shared"), (Join-Path $sdk "Include/$sdkVersion/ucrt"),
    (Join-Path $sdk "Include/$sdkVersion/winrt"), (Join-Path $WdkRoot 'Include/wdf/umdf/2.25'),
    (Join-Path $WdkRoot "Include/$sdkVersion/um/iddcx/1.6"))
$wdfLib = Join-Path $WdkRoot 'Lib/wdf/umdf/x64/2.25/WdfDriverStubUm.lib'
$iddLib = Join-Path $WdkRoot "Lib/$sdkVersion/um/x64/iddcx/1.6/iddcxstub.lib"
foreach ($file in @($cl, $wdfLib, $iddLib)) {
    if (!(Test-Path -LiteralPath $file)) { throw "Missing build dependency: $file" }
}
New-Item -ItemType Directory -Path $output -Force | Out-Null
$oldInclude, $oldLib = $env:INCLUDE, $env:LIB
try {
    $env:INCLUDE = $includes -join ';'
    $env:LIB = @((Join-Path $vc 'lib/x64'), (Join-Path $sdk "Lib/$sdkVersion/um/x64"),
        (Join-Path $sdk "Lib/$sdkVersion/ucrt/x64")) -join ';'
    $source = Join-Path $root 'windows/idd'
    $arguments = @('/nologo', '/std:c++17', '/EHsc', '/MD', '/O2', '/W4', '/LD', '/DUMDF_DRIVER',
        '/DUMDF_VERSION_MAJOR=2', '/DUMDF_VERSION_MINOR=25', '/DUMDF_MINIMUM_VERSION_REQUIRED=25',
        '/D_AMD64_', '/DAMD64', '/DIDDCX_VERSION_MAJOR=1', '/DIDDCX_VERSION_MINOR=6',
        '/DIDDCX_MINIMUM_VERSION_REQUIRED=4', '/DUNICODE', '/D_UNICODE', "/Fo$output/",
        "/Fe$output/SamaIddDriver.dll", (Join-Path $source 'driver/Driver.cpp'),
        (Join-Path $source 'FrameCapture.cpp'), (Join-Path $source 'DriverDiagnostics.cpp'),
        '/link', '/INCREMENTAL:NO', '/SUBSYSTEM:WINDOWS', '/DYNAMICBASE', '/NXCOMPAT',
        '/INCLUDE:FxDriverEntryUm', "/MAP:$output/SamaIddDriver.map", $wdfLib, $iddLib,
        'OneCoreUAP.lib', 'ntdll.lib', 'avrt.lib', 'd3d11.lib', 'dxgi.lib', 'ole32.lib', 'advapi32.lib')
    & $cl @arguments 2>&1 | Tee-Object -FilePath (Join-Path $output 'build.log')
    if ($LASTEXITCODE -ne 0) { throw "Driver build failed (exit $LASTEXITCODE)." }
    & $dumpbin /headers /imports /exports (Join-Path $output 'SamaIddDriver.dll') |
        Set-Content -LiteralPath (Join-Path $output 'pe-inspection.txt')
    Get-FileHash -LiteralPath (Join-Path $output 'SamaIddDriver.dll') -Algorithm SHA256
} finally {
    $env:INCLUDE, $env:LIB = $oldInclude, $oldLib
}

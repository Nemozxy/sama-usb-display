[CmdletBinding()]
param([string]$WdkRoot, [string]$BuildDirectory, [switch]$UsePowerShellCoreHost)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (!$WdkRoot) { $WdkRoot = [Environment]::GetEnvironmentVariable('WDKContentRoot') }
if (!$WdkRoot) { $WdkRoot = Join-Path ([Environment]::GetEnvironmentVariable('ProgramFiles(x86)')) 'Windows Kits/10' }
$build = if ($BuildDirectory) { [IO.Path]::GetFullPath($BuildDirectory) } else { Join-Path $root 'build/driver' }
$package = Join-Path $build 'package'
$dll = Join-Path $build 'SamaIddDriver.dll'
if (!(Test-Path -LiteralPath $dll)) { throw 'Build the driver first.' }
$sdkVersion = Get-ChildItem -LiteralPath (Join-Path $WdkRoot 'tools') -Directory |
    Where-Object { $_.Name -like '10.0.*' } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1 -ExpandProperty Name
if (!$sdkVersion) { throw 'No versioned WDK tools directory was found.' }
$infverif = Join-Path $WdkRoot "tools/$sdkVersion/x64/infverif.exe"
New-Item -ItemType Directory -Path $package -Force | Out-Null
Copy-Item -LiteralPath $dll -Destination $package -Force
Copy-Item -LiteralPath (Join-Path $root 'windows/idd/driver/SamaIddDriver.inf') -Destination $package -Force
Copy-Item -LiteralPath (Join-Path $root 'windows/idd/driver/LICENSE') -Destination $package -Force
$inf = Join-Path $package 'SamaIddDriver.inf'
$cat = Join-Path $package 'SamaIddDriver.cat'
if (Test-Path -LiteralPath $cat) { Remove-Item -LiteralPath $cat -Force }
& $infverif /u /v $inf 2>&1 | Tee-Object -FilePath (Join-Path $build 'infverif.log')
if ($LASTEXITCODE -ne 0) { throw "InfVerif failed (exit $LASTEXITCODE)." }
if ($UsePowerShellCoreHost) {
    & (Join-Path $root 'windows/invoke-inf2cat.ps1') -WdkRoot $WdkRoot -DriverDirectory $package
} else {
    $inf2cat = Join-Path $WdkRoot "bin/$sdkVersion/x86/Inf2Cat.exe"
    & $inf2cat "/driver:$package" '/os:10_VB_X64,10_NI_X64,10_GE_X64' /uselocaltime /verbose 2>&1 |
        Tee-Object -FilePath (Join-Path $build 'inf2cat.log')
    if ($LASTEXITCODE -ne 0) { throw "Inf2Cat failed (exit $LASTEXITCODE)." }
}
if (!(Test-Path -LiteralPath $cat)) { throw 'Inf2Cat did not create the catalog.' }
Get-ChildItem -LiteralPath $package -File | Get-FileHash -Algorithm SHA256

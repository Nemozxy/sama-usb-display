[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$WdkRoot,
      [Parameter(Mandatory=$true)][string]$DriverDirectory)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Core') { throw 'This compatibility host requires PowerShell 7.' }
$sdkVersion = Get-ChildItem -LiteralPath (Join-Path $WdkRoot 'bin') -Directory |
    Where-Object { $_.Name -like '10.0.*' } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1 -ExpandProperty Name
$exe = Join-Path $WdkRoot "bin/$sdkVersion/x86/Inf2Cat.exe"
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Reflection;
public static class SamaInfCatHost {
 public static int Run(string exe,string directory) {
  ResolveEventHandler resolver=(s,e)=>{
   var path=Path.Combine(Path.GetDirectoryName(exe),new AssemblyName(e.Name).Name+".dll");
   return File.Exists(path)?Assembly.LoadFrom(path):null;
  };
  AppDomain.CurrentDomain.AssemblyResolve+=resolver;
  try {
   var asm=Assembly.LoadFrom(exe);
   string[] args={"/driver:"+directory,"/os:10_VB_X64,10_NI_X64,10_GE_X64","/uselocaltime","/verbose"};
   var result=asm.EntryPoint.Invoke(null,new object[]{args});
   return result is int?(int)result:Environment.ExitCode;
  } finally {AppDomain.CurrentDomain.AssemblyResolve-=resolver;}
 }
}
'@
$exit = [SamaInfCatHost]::Run($exe, (Resolve-Path -LiteralPath $DriverDirectory).Path)
if ($exit -ne 0) { throw "Inf2Cat failed (exit $exit)." }

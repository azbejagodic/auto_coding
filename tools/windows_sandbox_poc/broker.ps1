param([switch]$ValidateOnly)
# Trusted host-only package activation. Never mapped into the guest.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$started = [DateTime]::UtcNow
$packages = @(Get-AppxPackage -Name 'MicrosoftWindows.WindowsSandbox')
if ($packages.Count -ne 1) { throw 'Expected one registered Windows Sandbox package.' }
$package = $packages[0]
$publisher = 'CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond, S=Washington, C=US'
if ($package.Publisher -cne $publisher -or $package.Status -ne 'Ok' -or
    $package.PackageFamilyName -cne 'MicrosoftWindows.WindowsSandbox_cw5n1h2txyewy' -or
    $package.SignatureKind.ToString() -notin @('Store', 'System')) { throw 'Untrusted Sandbox package registration.' }
$location = [IO.Path]::GetFullPath($package.InstallLocation)
if (!$location.StartsWith((Join-Path $env:ProgramFiles 'WindowsApps\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Sandbox package is outside the protected WindowsApps directory.'
}
$image = Join-Path $location 'WindowsSandboxServer.exe'
$cli = Join-Path $location 'wsb.exe'
$signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $location 'AppxSignature.p7x')
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -cne $publisher) {
    throw 'The package does not have a valid Microsoft Windows signature.'
}
[xml]$manifest = [IO.File]::ReadAllText((Join-Path $location 'AppxManifest.xml'))
$apps = @($manifest.Package.Applications.Application | Where-Object { $_.Id -ceq 'AppService' })
if ($apps.Count -ne 1 -or $apps[0].Executable -cne 'WindowsSandboxServer.exe' -or
    $apps[0].EntryPoint -cne 'SandboxServer.Program') { throw 'Unexpected broker activation registration.' }
Add-Type -ReferencedAssemblies @('System.dll', 'System.Core.dll', 'System.Xml.dll') -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.IO;
using System.Xml;
using System.Security.Cryptography;
[ComImport, Guid("beb94909-e451-438b-b5a7-d79e767b75d8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ISandboxPackageFactory {
    void CreatePackageWriter(IntPtr stream, IntPtr settings, out IntPtr writer);
    void CreatePackageReader(IntPtr stream, out IntPtr reader);
    void CreateManifestReader(IntPtr stream, out IntPtr reader);
    void CreateBlockMapReader(IntPtr stream, out IntPtr reader);
    [PreserveSig] int CreateValidatedBlockMapReader(System.Runtime.InteropServices.ComTypes.IStream stream,
        [MarshalAs(UnmanagedType.LPWStr)] string signature, out IntPtr reader);
}
[ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ISandboxActivation {
    [PreserveSig] int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string app,
        [MarshalAs(UnmanagedType.LPWStr)] string args, uint options, out uint pid);
    [PreserveSig] int ActivateForFile(IntPtr app, IntPtr items, IntPtr verb, out uint pid);
    [PreserveSig] int ActivateForProtocol(IntPtr app, IntPtr items, out uint pid);
}
public static class SandboxActivation {
    [DllImport("shlwapi.dll", CharSet=CharSet.Unicode)]
    static extern int SHCreateStreamOnFileEx(string path, uint mode, uint attributes, bool create,
        IntPtr reserved, out System.Runtime.InteropServices.ComTypes.IStream stream);
    public static int ValidatePayload(string root) {
        var factory=Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("5842a140-ff9f-4166-8f5c-62f5b7b0c781")));
        System.Runtime.InteropServices.ComTypes.IStream stream=null; IntPtr reader=IntPtr.Zero;
        string map=Path.Combine(root,"AppxBlockMap.xml");
        try {
            Marshal.ThrowExceptionForHR(SHCreateStreamOnFileEx(map,0x20,0,false,IntPtr.Zero,out stream));
            Marshal.ThrowExceptionForHR(((ISandboxPackageFactory)factory).CreateValidatedBlockMapReader(
                stream,Path.Combine(root,"AppxSignature.p7x"),out reader));
            var xml=new XmlDocument(); xml.XmlResolver=null; xml.Load(map);
            if(xml.DocumentElement.GetAttribute("HashMethod")!="http://www.w3.org/2001/04/xmlenc#sha256")
                throw new InvalidOperationException("Unsupported package block hash algorithm");
            int count=0; bool broker=false,cli=false,manifest=false;
            foreach(XmlElement file in xml.DocumentElement.ChildNodes) {
                string name=file.GetAttribute("Name");
                string path=Path.GetFullPath(Path.Combine(root,name));
                if(!path.StartsWith(root+"\\",StringComparison.OrdinalIgnoreCase) ||
                    (File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0)
                    throw new InvalidOperationException("Unsafe package payload path");
                using(var input=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read))
                using(var sha=SHA256.Create()) {
                    if(input.Length!=long.Parse(file.GetAttribute("Size"))) throw new InvalidOperationException("Package file length mismatch");
                    byte[] buffer=new byte[65536];
                    foreach(XmlElement block in file.ChildNodes) {
                        if(block.LocalName=="FileHash" && block.NamespaceURI=="http://schemas.microsoft.com/appx/2021/blockmap") {
                            long position=input.Position; input.Position=0;
                            if(Convert.ToBase64String(sha.ComputeHash(input))!=block.GetAttribute("Hash"))
                                throw new InvalidOperationException("Signed package whole-file hash mismatch: "+name);
                            input.Position=position; continue;
                        }
                        if(block.LocalName!="Block" || block.NamespaceURI!="http://schemas.microsoft.com/appx/2010/blockmap")
                            throw new InvalidOperationException("Unsupported package block map extension");
                        int wanted=(int)Math.Min(65536,input.Length-input.Position),length=0;
                        while(length<wanted) {int read=input.Read(buffer,length,wanted-length); if(read==0)throw new EndOfStreamException();length+=read;}
                        if(Convert.ToBase64String(sha.ComputeHash(buffer,0,length))!=block.GetAttribute("Hash"))
                            throw new InvalidOperationException("Signed package block hash mismatch: "+name);
                    }
                    if(input.Position!=input.Length)throw new InvalidOperationException("Incomplete package block inventory");
                }
                if(name=="WindowsSandboxServer.exe")broker=true;
                if(name=="wsb.exe")cli=true;
                if(name=="AppxManifest.xml")manifest=true;
                count++;
            }
            if(!broker || !cli || !manifest)throw new InvalidOperationException("Required package payload omitted");
            return count;
        } finally {
            if(reader!=IntPtr.Zero)Marshal.Release(reader);
            if(stream!=null)Marshal.ReleaseComObject(stream);
            Marshal.ReleaseComObject(factory);
        }
    }
    public static uint Activate(string app) {
        var instance = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")));
        try { uint pid; Marshal.ThrowExceptionForHR(((ISandboxActivation)instance).ActivateApplication(app, "", 2, out pid)); return pid; }
        finally { Marshal.ReleaseComObject(instance); }
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)]
    static extern int GetPackageFullName(IntPtr process, ref uint length, StringBuilder name);
    public static string Package(IntPtr process) {
        uint length=0; int code=GetPackageFullName(process, ref length, null);
        if(code!=122) throw new InvalidOperationException("No process package identity: "+code);
        var name=new StringBuilder((int)length);
        code=GetPackageFullName(process, ref length, name);
        if(code!=0) throw new InvalidOperationException("Cannot verify process package: "+code);
        return name.ToString();
    }
}
'@
$payloadCount = [SandboxActivation]::ValidatePayload($location)
if ($ValidateOnly) {
    $managementHost = Join-Path $env:SYSTEMROOT 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $hostSignature = Get-AuthenticodeSignature -LiteralPath $managementHost
    if ($hostSignature.Status -ne 'Valid' -or $hostSignature.SignerCertificate.Subject -cne $publisher) {
        throw 'The fixed management host does not have a valid Microsoft Windows signature.'
    }
    [ordered]@{ signatureVerified=$true; packagePayloadVerified=$true; payloadFileCount=$payloadCount;
        packageFullName=$package.PackageFullName; activationAttempted=$false; cli=$cli; image=$image;
        brokerSha256=(Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant();
        managementHostVerified=$true } | ConvertTo-Json -Compress
    return
}
function Confirm-Broker($Process) {
    if ($Process.HasExited -or $Process.SessionId -ne [Diagnostics.Process]::GetCurrentProcess().SessionId -or
        ![string]::Equals($Process.MainModule.FileName, $image, [StringComparison]::OrdinalIgnoreCase) -or
        [SandboxActivation]::Package($Process.Handle) -cne $package.PackageFullName) {
        throw 'Broker PID does not have the expected signed image, package and session identity.'
    }
}
$existing = @([Diagnostics.Process]::GetProcessesByName('WindowsSandboxServer'))
if ($existing.Count -gt 1) { throw 'Ambiguous existing broker processes; no process will be stopped.' }
$cold = $existing.Count -eq 0
if ($cold) {
    $activationStarted = [DateTime]::UtcNow
    $brokerId = [SandboxActivation]::Activate($package.PackageFamilyName + '!AppService')
    $broker = [Diagnostics.Process]::GetProcessById($brokerId)
    Confirm-Broker $broker
    if ($broker.StartTime.ToUniversalTime() -lt $activationStarted.AddSeconds(-1)) { throw 'Cold activation returned a stale process.' }
} else {
    $broker = $existing[0]
    Confirm-Broker $broker
    $brokerId = $broker.Id
}
[ordered]@{ schemaVersion=1; coldStart=$cold; processId=$brokerId;
    processStartedUtc=$broker.StartTime.ToUniversalTime().ToString('o');
    image=$image; cli=$cli; packageFullName=$package.PackageFullName;
    publisher=$package.Publisher; signatureVerified=$true; packageIdentityVerified=$true;
    packagePayloadVerified=$true; payloadFileCount=$payloadCount;
    activation='IApplicationActivationManager'; startedUtc=$started.ToString('o');
    finishedUtc=[DateTime]::UtcNow.ToString('o') } | ConvertTo-Json -Compress

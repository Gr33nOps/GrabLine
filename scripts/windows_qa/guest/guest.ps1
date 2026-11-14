[CmdletBinding()]
param(
    [ValidateSet('Preflight','Install','Inspect','Uninstall')][string]$Stage='Preflight',
    [string]$Evidence='C:\GrabLine-QA',
    [string]$Installer,
    [string]$InstallDirectory='C:\GrabLine-QA\Installed',
    [string]$SyntheticDownloads
)
$ErrorActionPreference='Stop'
$machine=Get-CimInstance Win32_ComputerSystem
if ($env:COMPUTERNAME -eq 'DESKTOP-J5QDQCG' -or
    ($machine.Model -notmatch 'Virtual|VMware|KVM|HVM|Parallels' -and $machine.Manufacturer -notmatch 'QEMU|Xen')) {
    throw 'Guest-only runner: this machine is not a verified disposable virtual machine.'
}
$root=[IO.Path]::GetFullPath($Evidence)
if ($root -eq [IO.Path]::GetPathRoot($root)) { throw 'Evidence must be a dedicated directory.' }
New-Item -ItemType Directory -Force -Path $root | Out-Null
$cases=Join-Path $root 'cases'
New-Item -ItemType Directory -Force -Path $cases | Out-Null
function Save-Case($Id,$Status,$Detail) {
    @{id=$Id;status=$Status;level='VM';time_utc=[DateTime]::UtcNow.ToString('o');detail=$Detail} |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $cases ($Id+'.json')) -Encoding utf8
}
try {
    switch ($Stage) {
        'Preflight' {
            $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
            $admin=([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
            $data=@{computer=$env:COMPUTERNAME;model=$machine.Model;manufacturer=$machine.Manufacturer;
                    os=(Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber);
                    administrator=$admin;power_states=(& powercfg /a | Out-String);
                    disks=(Get-PSDrive -PSProvider FileSystem | Select-Object Name,Free,Used)}
            $data | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $root 'preflight.json') -Encoding utf8
            Save-Case 'VM-PREFLIGHT' 'PASS' $data
        }
        'Install' {
            if (-not $Installer -or -not (Test-Path -LiteralPath $Installer -PathType Leaf)) { throw 'Supply the exact installer path.' }
            $signature=Get-AuthenticodeSignature -LiteralPath $Installer
            @{sha256=(Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash;signature=$signature.Status.ToString();signer=$signature.SignerCertificate.Subject} |
                ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'installer-identity.json') -Encoding utf8
            $p=Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/DIR="'+$InstallDirectory+'"'),('/LOG="'+(Join-Path $root 'install.log')+'"')) -PassThru -Wait -WindowStyle Hidden
            if ($p.ExitCode -ne 0) { throw "Installer exit $($p.ExitCode)" }
            Save-Case 'VM-INSTALL-COMMAND' 'PASS' @{exit=$p.ExitCode;directory=$InstallDirectory;limitation='UI/first-run assertions are separate cases'}
        }
        'Inspect' {
            $keys=@('HKCU:\Software\Google\Chrome\NativeMessagingHosts\dev.grabline.host','HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\dev.grabline.host','HKCU:\Software\Mozilla\NativeMessagingHosts\dev.grabline.host','HKCU:\Software\Classes\magnet','HKCU:\Software\Classes\.torrent')
            $entries=@{}
            foreach($key in $keys) { $entries[$key]=if(Test-Path -LiteralPath $key){Get-ItemProperty -LiteralPath $key | Select-Object * -ExcludeProperty PSPath,PSParentPath,PSDrive,PSProvider}else{$null} }
            $entries | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $root 'registration.json') -Encoding utf8
            Get-ChildItem -LiteralPath $InstallDirectory -File -Recurse | Get-FileHash -Algorithm SHA256 |
                Select-Object Path,Hash | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'installed-hashes.json') -Encoding utf8
        }
        'Uninstall' {
            if (-not $SyntheticDownloads) { throw 'Supply a dedicated synthetic download directory to verify preservation.' }
            $downloads=(Resolve-Path -LiteralPath $SyntheticDownloads).Path
            if ($downloads -eq [IO.Path]::GetPathRoot($downloads)) { throw 'A drive root is not a synthetic download directory.' }
            $before=@(Get-ChildItem -LiteralPath $downloads -File -Recurse | Get-FileHash -Algorithm SHA256 | Select-Object Path,Hash)
            $uninstaller=Join-Path $InstallDirectory 'unins000.exe'
            if (-not(Test-Path -LiteralPath $uninstaller -PathType Leaf)) { throw 'Exact Inno uninstaller unavailable.' }
            $p=Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -PassThru -Wait -WindowStyle Hidden
            if($p.ExitCode -ne 0){throw "Uninstaller exit $($p.ExitCode)"}
            foreach($entry in $before){if(-not(Test-Path -LiteralPath $entry.Path) -or (Get-FileHash -LiteralPath $entry.Path -Algorithm SHA256).Hash -ne $entry.Hash){throw "Synthetic download changed: $($entry.Path)"}}
            Save-Case 'VM-UNINSTALL-DOWNLOAD-PRESERVATION' 'PASS' @{files=$before.Count;limitation='Shortcut and registry cleanup require Inspect comparison'}
        }
    }
} catch {
    Save-Case ('VM-'+$Stage.ToUpperInvariant()) 'FAIL' $_.Exception.Message
    throw
}

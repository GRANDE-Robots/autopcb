[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Install', 'Qualify')]
    [string] $Phase,

    [Parameter(Mandatory)]
    [string] $Installer,

    [Parameter(Mandatory)]
    [string] $EvidenceDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Check($Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}

function Read-Json([string] $Path) {
    Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
}

function Join-NativeArguments([string[]] $Tokens) {
    # Quote for Windows PowerShell 5.1, whose process API takes one command line.
    $quoted = foreach ($token in $Tokens) {
        $escaped = $token -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1'
        '"' + $escaped + '"'
    }
    $quoted -join ' '
}

function Invoke-Recorded([string] $Name, [string] $File, [string[]] $Arguments, [int] $Seconds = 180) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $File
    $start.Arguments = Join-NativeArguments $Arguments
    $start.WorkingDirectory = $cliWorkspace
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($nameToClear in @('PYTHONHOME', 'PYTHONPATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM',
                               'QML2_IMPORT_PATH', 'KICAD_CLI', 'KICAD_PYTHON')) {
        $start.EnvironmentVariables.Remove($nameToClear)
    }
    $start.EnvironmentVariables['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
    $stdout = Join-Path $evidence "$Name.stdout.txt"
    $stderr = Join-Path $evidence "$Name.stderr.txt"
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        try { [void] $process.Start() }
        catch { throw "Could not launch $File. $($_.Exception.GetBaseException().Message)" }
        $out = $process.StandardOutput.ReadToEndAsync()
        $err = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Seconds * 1000)) {
            if (-not $process.HasExited) { $process.Kill() }
            throw "$Name timed out after $Seconds seconds."
        }
        [IO.File]::WriteAllText($stdout, $out.GetAwaiter().GetResult())
        [IO.File]::WriteAllText($stderr, $err.GetAwaiter().GetResult())
        [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = $stdout; Stderr = $stderr }
    }
    finally { $process.Dispose() }
}

function Assert-SourceHashes([string] $Root, $Hashes) {
    foreach ($entry in $Hashes.PSObject.Properties) {
        $file = Join-Path $Root $entry.Name
        Assert-Check (Test-Path -LiteralPath $file -PathType Leaf) "Missing file $file."
        Assert-Check ((Get-FileHash -LiteralPath $file).Hash -eq $entry.Value) "File changed: $file."
    }
}

function Test-DraftPackages([string] $ExportRoot, [int] $Count) {
    $packages = @(Get-ChildItem -LiteralPath $ExportRoot -Filter manifest.json -File -Recurse)
    Assert-Check ($packages.Count -eq $Count) "Expected $Count package manifest(s) under $ExportRoot."
    foreach ($package in $packages) {
        $manifest = Read-Json $package.FullName
        Assert-Check ($manifest.status -eq 'draft' -and $manifest.production_approved -eq $false) "Unexpected package status at $package."
        $entries = @($manifest.artifacts.PSObject.Properties.Name)
        $required = @('bom.csv', 'placements.csv', 'schematic.pdf', 'assembly.pdf',
                      'evidence/erc.json', 'evidence/drc.json', 'evidence/reference.json')
        foreach ($name in $required) {
            Assert-Check ($name -in $entries) "Missing $name in $package."
        }
        Assert-Check ($entries -like 'gerbers/*.gbrjob') "Missing Gerber job in $package."
        Assert-Check ($entries -like 'drills/*.drl') "Missing drill file in $package."
        Assert-SourceHashes $package.DirectoryName $manifest.artifacts
    }
    $packages
}

$installerFile = (Resolve-Path -LiteralPath $Installer).Path
$installerHash = (Get-FileHash -LiteralPath $installerFile).Hash
$evidence = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EvidenceDir)
$cliWorkspace = Join-Path $evidence 'CLI Workspace'
$reportPath = Join-Path $evidence 'windows-qualification.json'
$appDir = Join-Path $env:LOCALAPPDATA 'Programs\AutoPCB'
$installed = Join-Path $appDir 'AutoPCB.exe'
$workspace = Join-Path $env:LOCALAPPDATA 'AutoPCB\Workspace'
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'AutoPCB.lnk'
$registration = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoPCB_is1'
$kiCadBin = Join-Path $env:ProgramFiles 'KiCad\10.0\bin'
$kiCadCli = Join-Path $kiCadBin 'kicad-cli.exe'
$kiCadPython = Join-Path $kiCadBin 'python.exe'
$os = Get-CimInstance -ClassName Win32_OperatingSystem -Property BuildNumber,ProductType
$cpu = @(Get-CimInstance -ClassName Win32_Processor -Property Architecture | Select-Object -ExpandProperty Architecture)
Assert-Check ([Environment]::Is64BitProcess -and $cpu.Count -gt 0 -and
              @($cpu | Where-Object { $_ -ne 9 }).Count -eq 0 -and
              $os.ProductType -eq 1 -and [int] $os.BuildNumber -ge 22000) 'Run 64-bit PowerShell on a Windows 11 x64 test VM.'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
Assert-Check (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) 'Run this test without elevation.'

if ($Phase -eq 'Install') {
    Assert-Check (-not (Test-Path -LiteralPath $evidence)) "Choose a new evidence directory: $evidence."
    [void] [IO.Directory]::CreateDirectory($cliWorkspace)
    $signature = Get-AuthenticodeSignature -LiteralPath $installerFile
    $report = [pscustomobject] [ordered] @{
        schema_version = 1
        status = 'running'
        step = 'clean_vm'
        installer = $installerFile
        installer_sha256 = $installerHash
        authenticode = [string] $signature.Status
        windows_build = [string] $os.BuildNumber
        powershell = [string] $PSVersionTable.PSVersion
        installed_program_sha256 = $null
        kicad_version = $null
        reference_qualification = $null
        desktop_evidence = $null
        failure = $null
    }
}
else {
    $report = Read-Json $reportPath
    Assert-Check ($report.status -eq 'awaiting_kicad' -and $report.installer_sha256 -eq $installerHash) 'Run the Install phase on this VM with the same installer and evidence directory first.'
    Assert-Check (Test-Path -LiteralPath $kiCadCli) "Install KiCad 10.0.6 under $kiCadBin first."
    Assert-Check (Test-Path -LiteralPath $kiCadPython) "KiCad Python is missing from $kiCadBin."
}

try {
    if ($Phase -eq 'Install') {
        foreach ($path in @($appDir, $workspace, $shortcut, $registration, $kiCadCli)) {
            Assert-Check (-not (Test-Path -LiteralPath $path)) "The clean VM already has $path."
        }
        $report.step = 'install'
        Write-Host 'Installing AutoPCB for the test user.'
        $log = Join-Path $evidence 'install.log'
        $setup = Invoke-Recorded 'setup' $installerFile @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/LOG=$log") 600
        Assert-Check ($setup.ExitCode -eq 0 -and (Test-Path -LiteralPath $installed)) "Setup failed. See $log."
        Assert-Check (Test-Path -LiteralPath $shortcut) 'The installer did not create the Start shortcut.'
        Assert-Check (Test-Path -LiteralPath $registration) 'The per-user uninstall entry is missing.'
        $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
        Assert-Check ($link.TargetPath -eq $installed -and $link.Arguments -eq 'desktop' -and
                      $link.WorkingDirectory -eq $appDir) 'The Start shortcut points to the wrong command or working directory.'
        $report.installed_program_sha256 = (Get-FileHash -LiteralPath $installed).Hash

        $report.step = 'missing_kicad'
        $help = Invoke-Recorded 'help' $installed @('--help')
        Assert-Check ($help.ExitCode -eq 0 -and (Get-Content -LiteralPath $help.Stdout -Raw) -match 'qualify') 'The installed CLI could not show help.'
        $missing = Invoke-Recorded 'doctor-without-kicad' $installed @('--workspace', $cliWorkspace, 'doctor')
        $diagnostic = Read-Json $missing.Stdout
        Assert-Check ($missing.ExitCode -eq 2 -and $diagnostic.status -eq 'blocked' -and
                      $diagnostic.error -like 'KiCad CLI was not found*') 'The installed CLI did not report the missing KiCad dependency.'
        $report.step = 'install_passed'
        $report.status = 'awaiting_kicad'
        Write-Host 'Install KiCad 10.0.6 in its standard location, then rerun with -Phase Qualify.'
    }
    else {
        $report.step = 'toolchain'
        Assert-Check ((Get-FileHash -LiteralPath $installed).Hash -eq $report.installed_program_sha256) 'The installed launcher differs from the Install phase.'
        $native = @('--workspace', $cliWorkspace, '--kicad-cli', $kiCadCli, '--kicad-python', $kiCadPython)
        $doctor = Invoke-Recorded 'doctor-with-kicad' $installed ($native + 'doctor')
        $toolchain = Read-Json $doctor.Stdout
        Assert-Check ($doctor.ExitCode -eq 0 -and $toolchain.status -eq 'ok' -and
                      $toolchain.kicad_version -eq '10.0.6') 'The installed AutoPCB did not find the matching KiCad 10.0.6 toolchain.'
        $report.kicad_version = $toolchain.kicad_version

        $report.step = 'reference_projects'
        Write-Host 'Running the USB and temperature-sensor reference projects with KiCad.'
        $qualification = Join-Path $evidence 'reference-projects'
        $run = Invoke-Recorded 'qualify' $installed ($native + @('qualify', '--output', $qualification,
                                  '--rounds', '45', '--seconds', '8', '--memory-gib', '2')) 3600
        $report.reference_qualification = Join-Path $qualification 'qualification.json'
        $result = Read-Json $report.reference_qualification
        $names = @($result.cases.name | Sort-Object)
        Assert-Check ($run.ExitCode -eq 2 -and $result.status -eq 'qualification_incomplete' -and
                      ($names -join '|') -eq 'Temperature Sensor|USB Breakout') 'Unexpected reference qualification outcome.'
        foreach ($case in $result.cases) {
            Assert-Check ($case.stage -eq 'done' -and $case.native_passed -and $case.saved_opens -eq 0 -and
                          $case.applied -and $case.applied.only_pcb_changed -and $case.applied.locked_editor_rejected -and
                          $case.applied.stale_rules_rejected) "Native checks or guarded Apply failed for $($case.name)."
            $reviewRequired = $case.checked_export -eq 'blocked' -and @($case.blockers).Count -eq 1 -and
                              $case.blockers[0] -eq 'A source-bound electrical review report is required.'
            Assert-Check $reviewRequired "Unexpected checked-export result for $($case.name)."
        }
        [void] (Test-DraftPackages (Join-Path $qualification 'results\exports') 2)

        $report.step = 'desktop'
        Assert-Check (-not (Test-Path -LiteralPath $workspace)) 'The default desktop workspace must be fresh.'
        Start-Process -FilePath $shortcut
        Write-Host 'In Requirements, enter "Four-layer full-speed Type-B USB breakout at 5 V".'
        Write-Host 'Save requirements, propose the design, and approve creation as USB Desktop QA.'
        [void] (Read-Host 'Press Enter once AutoPCB shows the new board in Layout')
        $projectDir = Join-Path $workspace 'results\projects\USB Desktop QA'
        $project = Join-Path $projectDir 'USB Desktop QA.kicad_pro'
        Assert-Check (Test-Path -LiteralPath $project) 'The Start menu launch did not create the expected native project in the default workspace.'
        $beforeJob = Invoke-Recorded 'desktop-before' $installed ($native + @('--project', $project, 'verify'))
        $before = Read-Json $beforeJob.Stdout
        Assert-Check ($beforeJob.ExitCode -in @(0, 2) -and $before.native.open_connections -gt 0) 'The desktop fixture should have an open connection before preview.'
        Write-Host 'Click Preview routing. AutoPCB already created a routing policy. Wait for the side-by-side comparison.'
        [void] (Read-Host 'Press Enter with the comparison visible and Apply preview available')
        Assert-SourceHashes $projectDir $before.inputs
        Write-Host 'Apply the preview, run independent verification, and export a draft package from the desktop.'
        [void] (Read-Host 'Close AutoPCB after the draft export finishes, then press Enter')
        Assert-Check (-not (Get-Process -Name AutoPCB -ErrorAction SilentlyContinue)) 'Close all AutoPCB windows before uninstalling.'
        $verified = @(Get-ChildItem -LiteralPath (Join-Path $workspace 'results\jobs') -Filter verification.json -File -Recurse)
        Assert-Check ($verified.Count -eq 1) 'Expected one independent verification report from the installed desktop.'
        $after = Read-Json $verified[0].FullName
        $connected = $after.entry -eq $project -and $after.native.passed -and $after.native.open_connections -eq 0
        Assert-Check $connected 'Desktop verification did not confirm a connected board.'
        Assert-Check (@($before.inputs.PSObject.Properties).Count -eq @($after.inputs.PSObject.Properties).Count) 'Apply changed the project input list.'
        foreach ($item in $before.inputs.PSObject.Properties) {
            $new = $after.inputs.PSObject.Properties[$item.Name]
            $changed = $item.Name -eq 'USB Desktop QA.kicad_pcb'
            Assert-Check ($null -ne $new -and (($item.Value -ne $new.Value) -eq $changed)) "Unexpected Apply result for $($item.Name)."
        }
        Assert-SourceHashes $projectDir $after.inputs
        $draft = Test-DraftPackages (Join-Path $workspace 'results\exports') 1
        Assert-SourceHashes $projectDir (Read-Json $draft.FullName).inputs

        $report.step = 'uninstall'
        $uninstallers = @(Get-ChildItem -LiteralPath $appDir -Filter 'unins*.exe' -File)
        Assert-Check ($uninstallers.Count -eq 1) 'Expected one AutoPCB uninstaller.'
        $uninstallLog = Join-Path $evidence 'uninstall.log'
        $uninstall = Invoke-Recorded 'uninstall' $uninstallers[0].FullName @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$uninstallLog") 600
        Assert-Check ($uninstall.ExitCode -eq 0) "Uninstall failed. See $uninstallLog."
        $installedPaths = @($installed, $shortcut, $registration)
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        while ([DateTime]::UtcNow -lt $deadline -and ($installedPaths | Where-Object { Test-Path -LiteralPath $_ })) {
            Start-Sleep -Milliseconds 200
        }
        foreach ($path in $installedPaths) {
            Assert-Check (-not (Test-Path -LiteralPath $path)) "Uninstall left $path."
        }
        Assert-SourceHashes $projectDir $after.inputs
        Assert-Check (Test-Path -LiteralPath $verified[0].FullName) 'Uninstall removed the desktop verification report.'
        [void] (Test-DraftPackages (Join-Path $workspace 'results\exports') 1)

        $desktop = Join-Path $evidence 'desktop'
        [void] [IO.Directory]::CreateDirectory($desktop)
        Copy-Item -LiteralPath $projectDir -Destination (Join-Path $desktop 'project') -Recurse
        Copy-Item -LiteralPath $draft.DirectoryName -Destination (Join-Path $desktop 'draft') -Recurse
        Copy-Item -LiteralPath $verified[0].FullName -Destination (Join-Path $desktop 'verification.json')
        $report.desktop_evidence = $desktop
        $report.step = 'done'
        $report.status = 'passed'
    }
}
catch {
    $report.status = 'failed'
    $report.failure = $_.Exception.Message
    throw
}
finally {
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host "Qualification evidence: $reportPath"
}

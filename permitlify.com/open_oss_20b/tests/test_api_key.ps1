#requires -Version 5.1
<#
Offline regressions for the real helper, with no Pester dependency. Only a copy
of api_key.ps1 is executed; all credentials and ACL changes are temporary.
Run with Windows PowerShell 5.1, not with the Python client unittest suite.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSVersionTable.PSEdition -ne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
    throw 'Run these tests with Windows PowerShell 5.1.'
}
$tempParent = 'C:\Users\ADMINI~1\AppData\Local\Temp\2\opencode'
if (-not (Test-Path -LiteralPath $tempParent -PathType Container)) {
    throw 'The approved temporary parent directory must already exist.'
}
$sourceHelper = Join-Path (Split-Path $PSScriptRoot -Parent) 'api_key.ps1'
$scratchRoot = Join-Path $tempParent ('api-key-tests-' + [Guid]::NewGuid().ToString('N'))
$originalEnvironment = $env:PERMITLIFY_AI_API_KEY
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$allowedSids = @($userSid.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function New-TestBundle {
    $bundle = Join-Path $scratchRoot ([Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($bundle) | Out-Null
    Copy-Item -LiteralPath $sourceHelper -Destination (Join-Path $bundle 'api_key.ps1')
    return $bundle
}

function Get-KeyHash([string]$Key) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($Key))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Assert-NoCredentialOutput([string]$Text) {
    Assert-True ($Text -notmatch 'plai_[A-Za-z0-9_-]{43}|01000000d08c9ddf') 'The helper printed a credential.'
}

function Invoke-TempHelper([string]$Bundle, [switch]$Rotate) {
    $captured = New-Object 'System.Collections.Generic.List[string]'
    $succeeded = $false
    try {
        & (Join-Path $Bundle 'api_key.ps1') -Rotate:$Rotate *>&1 | ForEach-Object { $captured.Add([string]$_) }
        $succeeded = $true
    } catch { $captured.Add([string]$_) }
    $text = $captured -join "`n"
    Assert-NoCredentialOutput $text
    return [pscustomobject]@{ Succeeded = $succeeded; Text = $text }
}

function Assert-KeyAgrees([string]$Bundle) {
    $credentialPath = Join-Path $Bundle '.secrets\api-key.dpapi'
    $verifierPath = Join-Path $Bundle '.secrets\api-key.sha256'
    Assert-True ($env:PERMITLIFY_AI_API_KEY -cmatch '^plai_[A-Za-z0-9_-]{43}$') 'The key must contain 256 random bits in the expected format.'
    $secure = ConvertTo-SecureString ([IO.File]::ReadAllText($credentialPath))
    try {
        $credential = New-Object System.Net.NetworkCredential('', $secure)
        Assert-True ($credential.Password -ceq $env:PERMITLIFY_AI_API_KEY) 'User-bound DPAPI must decrypt to the process key.'
        Assert-True ([IO.File]::ReadAllText($verifierPath) -ceq (Get-KeyHash $credential.Password)) 'The verifier must agree with the key.'
    } finally { $secure.Dispose(); $credential = $null }
}

function Assert-PrivateAcl([string]$Path, [bool]$Directory) {
    $acl = Get-Acl -LiteralPath $Path
    Assert-True $acl.AreAccessRulesProtected 'ACL inheritance must be disabled.'
    Assert-True ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq $userSid.Value) 'The current user must own the protected object.'
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    Assert-True ($rules.Count -eq $allowedSids.Count) 'The DACL must contain exactly the allowlisted grants.'
    $expectedInheritance = [Security.AccessControl.InheritanceFlags]::None
    if ($Directory) { $expectedInheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' }
    foreach ($sid in $allowedSids) {
        $matching = @($rules | Where-Object { $_.IdentityReference.Value -eq $sid })
        Assert-True ($matching.Count -eq 1) 'Each allowed SID must have exactly one grant.'
        $rule = $matching[0]
        Assert-True (-not $rule.IsInherited) 'All grants must be explicit.'
        Assert-True ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) 'Only allow rules are expected.'
        Assert-True ($rule.FileSystemRights -eq [Security.AccessControl.FileSystemRights]::FullControl) 'Allowed SIDs must have full control.'
        Assert-True ($rule.InheritanceFlags -eq $expectedInheritance) 'Unexpected ACL inheritance flags.'
        Assert-True ($rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None) 'Unexpected ACL propagation flags.'
    }
}

$tests = [ordered]@{
    'new directory and files have exact private DACLs and current-user ownership' = {
        $bundle = New-TestBundle
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Creation failed.'
        $secretDirectory = Join-Path $bundle '.secrets'
        Assert-PrivateAcl $secretDirectory $true
        foreach ($name in @('api-key.dpapi', 'api-key.sha256', 'api-key.lock')) {
            Assert-PrivateAcl (Join-Path $secretDirectory $name) $false
        }
    }
    'create and ordinary load preserve key, ciphertext and timestamps' = {
        $bundle = New-TestBundle
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Creation failed.'
        Assert-KeyAgrees $bundle
        $key = $env:PERMITLIFY_AI_API_KEY
        $credentialPath = Join-Path $bundle '.secrets\api-key.dpapi'
        $verifierPath = Join-Path $bundle '.secrets\api-key.sha256'
        $encrypted = [IO.File]::ReadAllText($credentialPath)
        $verifier = [IO.File]::ReadAllText($verifierPath)
        $stamp = [DateTime]::new(2020, 1, 2, 3, 4, 5, [DateTimeKind]::Utc)
        [IO.File]::SetLastWriteTimeUtc($credentialPath, $stamp)
        [IO.File]::SetLastWriteTimeUtc($verifierPath, $stamp)
        $env:PERMITLIFY_AI_API_KEY = 'test-sentinel'
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Ordinary load failed.'
        Assert-True ($env:PERMITLIFY_AI_API_KEY -ceq $key) 'Ordinary load changed the key.'
        Assert-True ([IO.File]::ReadAllText($credentialPath) -ceq $encrypted) 'Ordinary load rewrote the credential.'
        Assert-True ([IO.File]::ReadAllText($verifierPath) -ceq $verifier) 'Ordinary load changed the verifier.'
        Assert-True ([IO.File]::GetLastWriteTimeUtc($credentialPath) -eq $stamp) 'Ordinary load rewrote the credential timestamp.'
        Assert-True ([IO.File]::GetLastWriteTimeUtc($verifierPath) -eq $stamp) 'Ordinary load rewrote the verifier timestamp.'
    }
    'rotation revokes the old key and loads the new matching credential' = {
        $bundle = New-TestBundle
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Creation failed.'
        $oldKey = $env:PERMITLIFY_AI_API_KEY
        Assert-True (Invoke-TempHelper $bundle -Rotate).Succeeded 'Rotation failed.'
        Assert-KeyAgrees $bundle
        Assert-True ($env:PERMITLIFY_AI_API_KEY -cne $oldKey) 'Rotation reused the old key.'
        $verifierPath = Join-Path $bundle '.secrets\api-key.sha256'
        Assert-True ([IO.File]::ReadAllText($verifierPath) -cne (Get-KeyHash $oldKey)) 'Rotation still authorizes the old key.'
        $newKey = $env:PERMITLIFY_AI_API_KEY
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Loading the rotated credential failed.'
        Assert-True ($env:PERMITLIFY_AI_API_KEY -ceq $newKey) 'Load restored an old key after rotation.'
    }
    'stale credential cannot repair a mismatched verifier or authorize an old key' = {
        $bundle = New-TestBundle
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Creation failed.'
        $credentialPath = Join-Path $bundle '.secrets\api-key.dpapi'
        $verifierPath = Join-Path $bundle '.secrets\api-key.sha256'
        $oldEncrypted = [IO.File]::ReadAllText($credentialPath)
        $oldHash = Get-KeyHash $env:PERMITLIFY_AI_API_KEY
        Assert-True (Invoke-TempHelper $bundle -Rotate).Succeeded 'Rotation failed.'
        $newHash = [IO.File]::ReadAllText($verifierPath)
        $stamp = [IO.File]::GetLastWriteTimeUtc($verifierPath)
        [IO.File]::WriteAllText($credentialPath, $oldEncrypted)
        $env:PERMITLIFY_AI_API_KEY = 'test-sentinel'
        $result = Invoke-TempHelper $bundle
        Assert-True (-not $result.Succeeded) 'A mismatched verifier must reject ordinary load.'
        Assert-True ($result.Text -match '-Rotate') 'Mismatch must explain the explicit rotation requirement.'
        Assert-True ($env:PERMITLIFY_AI_API_KEY -ceq 'test-sentinel') 'Failure changed the process environment.'
        Assert-True ([IO.File]::ReadAllText($verifierPath) -ceq $newHash) 'Load repaired a verifier using a stale key.'
        Assert-True ([IO.File]::ReadAllText($verifierPath) -cne $oldHash) 'Load reauthorized the old key.'
        Assert-True ([IO.File]::GetLastWriteTimeUtc($verifierPath) -eq $stamp) 'Failed load rewrote the verifier.'
        Assert-True ([IO.File]::ReadAllText($credentialPath) -ceq $oldEncrypted) 'Failed load changed the credential.'
        Assert-True (Invoke-TempHelper $bundle -Rotate).Succeeded 'Explicit rotation did not recover from mismatch.'
        Assert-KeyAgrees $bundle
        Assert-True ((Get-KeyHash $env:PERMITLIFY_AI_API_KEY) -cne $oldHash) 'Recovery reused the stale key.'
    }
    'missing verifier requires rotation without publishing the old key' = {
        $bundle = New-TestBundle
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Creation failed.'
        $credentialPath = Join-Path $bundle '.secrets\api-key.dpapi'
        $verifierPath = Join-Path $bundle '.secrets\api-key.sha256'
        $encrypted = [IO.File]::ReadAllText($credentialPath)
        [IO.File]::Delete($verifierPath)
        $env:PERMITLIFY_AI_API_KEY = $null
        $result = Invoke-TempHelper $bundle
        Assert-True (-not $result.Succeeded) 'Missing verifier must reject ordinary load.'
        Assert-True ($result.Text -match '-Rotate') 'Missing verifier must explain the explicit rotation requirement.'
        Assert-True ([string]::IsNullOrEmpty($env:PERMITLIFY_AI_API_KEY)) 'Failure populated the process environment.'
        Assert-True (-not [IO.File]::Exists($verifierPath)) 'Load restored a missing verifier.'
        Assert-True ([IO.File]::ReadAllText($credentialPath) -ceq $encrypted) 'Failed load changed the credential.'
        Assert-True (Invoke-TempHelper $bundle -Rotate).Succeeded 'Explicit rotation did not recover the missing verifier.'
        Assert-KeyAgrees $bundle
    }
    'verifier without credential requires explicit rotation' = {
        $bundle = New-TestBundle
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Creation failed.'
        $verifierPath = Join-Path $bundle '.secrets\api-key.sha256'
        $verifier = [IO.File]::ReadAllText($verifierPath)
        [IO.File]::Delete((Join-Path $bundle '.secrets\api-key.dpapi'))
        $env:PERMITLIFY_AI_API_KEY = 'test-sentinel'
        $result = Invoke-TempHelper $bundle
        Assert-True (-not $result.Succeeded) 'Verifier-only state must reject implicit creation.'
        Assert-True ($result.Text -match '-Rotate') 'Verifier-only state must require explicit rotation.'
        Assert-True ($env:PERMITLIFY_AI_API_KEY -ceq 'test-sentinel') 'Failure changed the process environment.'
        Assert-True ([IO.File]::ReadAllText($verifierPath) -ceq $verifier) 'Failed creation changed the verifier.'
        Assert-True (Invoke-TempHelper $bundle -Rotate).Succeeded 'Explicit rotation did not recover the missing credential.'
        Assert-KeyAgrees $bundle
    }
    'exclusive cross-process lock rejects create, load and rotate without writes' = {
        foreach ($existing in @($false, $true)) {
            $bundle = New-TestBundle
            if ($existing) { Assert-True (Invoke-TempHelper $bundle).Succeeded 'Creation failed.' }
            $secretDirectory = Join-Path $bundle '.secrets'
            [IO.Directory]::CreateDirectory($secretDirectory) | Out-Null
            $credentialPath = Join-Path $secretDirectory 'api-key.dpapi'
            $verifierPath = Join-Path $secretDirectory 'api-key.sha256'
            if ($existing) {
                $encrypted = [IO.File]::ReadAllText($credentialPath)
                $verifier = [IO.File]::ReadAllText($verifierPath)
                $stamp = [IO.File]::GetLastWriteTimeUtc($verifierPath)
            }
            $heldLock = [IO.File]::Open((Join-Path $secretDirectory 'api-key.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $jobs = @()
            try {
                foreach ($rotate in @($false, $true)) {
                    $jobs += Start-Job -ArgumentList (Join-Path $bundle 'api_key.ps1'), $rotate -ScriptBlock {
                        param($Helper, $Rotate)
                        $ErrorActionPreference = 'Stop'
                        $env:PERMITLIFY_AI_API_KEY = 'child-test-sentinel'
                        $captured = New-Object 'System.Collections.Generic.List[string]'
                        $succeeded = $false
                        try {
                            & $Helper -Rotate:$Rotate *>&1 | ForEach-Object { $captured.Add([string]$_) }
                            $succeeded = $true
                        } catch { $captured.Add([string]$_) }
                        [pscustomobject]@{
                            Succeeded = $succeeded
                            EnvironmentUnchanged = ($env:PERMITLIFY_AI_API_KEY -ceq 'child-test-sentinel')
                            Text = $captured -join "`n"
                            Version = $PSVersionTable.PSVersion.ToString()
                            ProcessId = $PID
                        }
                    }
                }
                $finished = @($jobs | Wait-Job -Timeout 20)
                Assert-True ($finished.Count -eq $jobs.Count) 'Lock contention did not terminate promptly.'
                foreach ($job in $jobs) {
                    Assert-True ($job.State -eq 'Completed') 'The child PowerShell process failed unexpectedly.'
                    $results = @(Receive-Job -Job $job)
                    Assert-True ($results.Count -eq 1) 'The child returned unexpected output.'
                    $result = $results[0]
                    Assert-NoCredentialOutput $result.Text
                    Assert-True ($result.Version.StartsWith('5.1.') -and $result.ProcessId -ne $PID) 'The test must exercise another Windows PowerShell 5.1 process.'
                    Assert-True (-not $result.Succeeded) 'An invocation bypassed the exclusive file lock.'
                    Assert-True $result.EnvironmentUnchanged 'Lock contention changed the child environment.'
                    Assert-True ($result.Text -match 'lock|another|in use') 'Lock rejection needs an actionable diagnostic.'
                }
                if ($existing) {
                    Assert-True ([IO.File]::ReadAllText($credentialPath) -ceq $encrypted) 'Lock contention changed the credential.'
                    Assert-True ([IO.File]::ReadAllText($verifierPath) -ceq $verifier) 'Lock contention changed the verifier.'
                    Assert-True ([IO.File]::GetLastWriteTimeUtc($verifierPath) -eq $stamp) 'Lock contention rewrote the verifier.'
                } else {
                    Assert-True (-not [IO.File]::Exists($credentialPath)) 'Lock contention created a credential.'
                    Assert-True (-not [IO.File]::Exists($verifierPath)) 'Lock contention created a verifier.'
                }
            } finally {
                $jobs | Stop-Job
                $jobs | Remove-Job -Force
                $heldLock.Dispose()
            }
            Assert-True (Invoke-TempHelper $bundle -Rotate).Succeeded 'The helper could not acquire a released lock.'
            Assert-KeyAgrees $bundle
        }
    }
    'load and rotate replace unexpected explicit grants on directory and files' = {
        $bundle = New-TestBundle
        Assert-True (Invoke-TempHelper $bundle).Succeeded 'Creation failed.'
        $secretDirectory = Join-Path $bundle '.secrets'
        $lockPath = Join-Path $secretDirectory 'api-key.lock'
        # Seed a lock for the old helper too, so this regression tests its ACL handling.
        if (-not [IO.File]::Exists($lockPath)) { [IO.File]::WriteAllText($lockPath, '') }
        $paths = @($secretDirectory, (Join-Path $secretDirectory 'api-key.dpapi'), (Join-Path $secretDirectory 'api-key.sha256'), $lockPath)
        foreach ($rotate in @($false, $true)) {
            foreach ($path in $paths) {
                $acl = Get-Acl -LiteralPath $path
                $everyone = [Security.Principal.SecurityIdentifier]::new('S-1-1-0')
                $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($everyone, 'FullControl', 'Allow'))
                Set-Acl -LiteralPath $path -AclObject $acl
            }
            Assert-True (Invoke-TempHelper $bundle -Rotate:$rotate).Succeeded 'Helper failed with an unexpected explicit grant.'
            Assert-KeyAgrees $bundle
            foreach ($path in $paths) { Assert-PrivateAcl $path ($path -eq $secretDirectory) }
        }
    }
}

$failures = 0
try {
    [IO.Directory]::CreateDirectory($scratchRoot) | Out-Null
    foreach ($test in $tests.GetEnumerator()) {
        try {
            & $test.Value
            Write-Host ('PASS: ' + $test.Key)
        } catch {
            $failures++
            # Assertions describe the failure, never the credential or captured output.
            Write-Host ('FAIL: ' + $test.Key + ' -- ' + $_.Exception.Message)
        }
    }
} finally {
    $env:PERMITLIFY_AI_API_KEY = $originalEnvironment
    if (Test-Path -LiteralPath $scratchRoot) { Remove-Item -LiteralPath $scratchRoot -Recurse -Force }
}
Write-Host ('Results: {0} passed, {1} failed (Windows PowerShell {2}).' -f ($tests.Count - $failures), $failures, $PSVersionTable.PSVersion)
if ($failures) { exit 1 }

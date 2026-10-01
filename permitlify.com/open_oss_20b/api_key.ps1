<#
Create/load the dedicated API key into this PowerShell process's environment.
The client key is protected by Windows user-bound DPAPI; Django reads only its
SHA-256 verifier. Run as the Windows user who will run the smoke client.
Use -Rotate explicitly to replace an existing credential.
Ordinary loads require a matching verifier and never rewrite it. Concurrent
invocations fail fast on the exclusive file lock; retry after the holder exits.
#>
[CmdletBinding()]
param([switch]$Rotate)

$ErrorActionPreference = 'Stop'
$secretDirectory = Join-Path $PSScriptRoot '.secrets'
$credentialPath = Join-Path $secretDirectory 'api-key.dpapi'
$verifierPath = Join-Path $secretDirectory 'api-key.sha256'
$lockPath = Join-Path $secretDirectory 'api-key.lock'

if (-not (Test-Path -LiteralPath $PSScriptRoot -PathType Container)) {
    throw 'The model bundle directory does not exist.'
}
# Replace the entire DACL, not just grants for known identities. The owner must
# also be trusted, since an owner can change the DACL. SIDs are locale-independent.
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$allowedSids = @($userSid.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique
function New-PrivateAcl([switch]$Directory) {
    # Security descriptors track (and clear) dirty flags when persisted. Build a
    # fresh descriptor for each object so every DACL and owner is actually set.
    $acl = [Security.AccessControl.FileSecurity]::new()
    $inheritance = [Security.AccessControl.InheritanceFlags]::None
    if ($Directory) {
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($userSid)
    foreach ($sid in $allowedSids) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance, [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow))
    }
    return $acl
}
[IO.Directory]::CreateDirectory($secretDirectory, (New-PrivateAcl -Directory)) | Out-Null
[IO.Directory]::SetAccessControl($secretDirectory, (New-PrivateAcl -Directory))

$lock = $null
$key = $null
$credential = $null
$secureKey = $null
try {
    try {
        $lock = [IO.FileStream]::new($lockPath, [IO.FileMode]::OpenOrCreate,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [IO.FileShare]::None, 4096, [IO.FileOptions]::None, (New-PrivateAcl))
    } catch [IO.IOException] {
        throw 'Unable to acquire the API key file lock; another invocation may be running. Retry after it finishes.'
    }
    # Keep the lock file in place: deleting it could split concurrent lock users.
    $lock.SetAccessControl((New-PrivateAcl))
    foreach ($path in @($credentialPath, $verifierPath)) {
        if (Test-Path -LiteralPath $path) { [IO.File]::SetAccessControl($path, (New-PrivateAcl)) }
    }

    # No credential/verifier state is read until the cross-process lock is held.
    $loadExisting = (Test-Path -LiteralPath $credentialPath) -and -not $Rotate
    if ($loadExisting) {
        $secureKey = ConvertTo-SecureString ([IO.File]::ReadAllText($credentialPath))
        $credential = New-Object System.Net.NetworkCredential('', $secureKey)
        $key = $credential.Password
    } else {
        if ((Test-Path -LiteralPath $verifierPath) -and -not $Rotate) {
            throw 'A verifier already exists without a local credential. Use -Rotate to replace it explicitly.'
        }
        $bytes = New-Object byte[] 32
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $random.GetBytes($bytes) } finally { $random.Dispose() }
        $key = 'plai_' + [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        $secureKey = ConvertTo-SecureString $key -AsPlainText -Force
        $encrypted = ConvertFrom-SecureString $secureKey
        [IO.File]::WriteAllText($credentialPath, $encrypted, [Text.Encoding]::ASCII)
        [IO.File]::SetAccessControl($credentialPath, (New-PrivateAcl))
    }

    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash([Text.Encoding]::ASCII.GetBytes($key))
        $verifier = [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
    } finally { $sha256.Dispose() }

    if ($loadExisting) {
        if (-not (Test-Path -LiteralPath $verifierPath) -or
            [IO.File]::ReadAllText($verifierPath) -cne $verifier) {
            throw 'The verifier is missing or does not match the local credential. Use -Rotate to replace it explicitly.'
        }
    } else {
        # Atomic replacement avoids a partially written verifier during rotation.
        $pending = Join-Path $secretDirectory ('verifier-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        try {
            [IO.File]::WriteAllText($pending, $verifier, [Text.Encoding]::ASCII)
            [IO.File]::SetAccessControl($pending, (New-PrivateAcl))
            if (Test-Path -LiteralPath $verifierPath) {
                # PowerShell 5.1 coerces $null to an empty (invalid) backup path.
                [IO.File]::Replace($pending, $verifierPath, [NullString]::Value)
            } else {
                [IO.File]::Move($pending, $verifierPath)
            }
        } finally {
            if (Test-Path -LiteralPath $pending) { [IO.File]::Delete($pending) }
        }
    }
    $env:PERMITLIFY_AI_API_KEY = $key
} finally {
    $key = $null
    $credential = $null
    if ($null -ne $secureKey) { $secureKey.Dispose() }
    if ($null -ne $lock) { $lock.Dispose() }
}
Write-Host 'Dedicated API key loaded into PERMITLIFY_AI_API_KEY for this PowerShell session.'
Write-Host 'The key is encrypted in .secrets/api-key.dpapi; no plaintext key was printed.'

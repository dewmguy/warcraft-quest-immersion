[CmdletBinding()]
param(
    [string]$Server = "plex@172.16.1.2",
    [string]$IdentityFile = "C:\Users\JeremyTilden\Documents\sshkeys\key.nopass"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw "SSH identity file was not found: $IdentityFile"
}

$secret = Read-Host "Paste a newly created ElevenLabs API key" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $plainText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ($plainText -notmatch '^sk_[A-Za-z0-9_-]{20,}$') {
        throw "The supplied value does not look like an ElevenLabs API key."
    }

    $plainText | & ssh -i $IdentityFile $Server "bash /opt/warcraft-quest-immersion/scripts/set-elevenlabs-key.sh"
    if ($LASTEXITCODE -ne 0) {
        throw "The server rejected the configuration or failed to restart the application."
    }
}
finally {
    $plainText = $null
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

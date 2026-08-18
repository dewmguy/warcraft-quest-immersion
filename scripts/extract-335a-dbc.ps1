[CmdletBinding()]
param(
    [string]$ClientRoot = "D:\World of Warcraft Unmodded",
    [string]$MPQEditorPath = "S:\Games\Warcraft\Utilities\MPQ Editor\MPQEditor.exe",
    [string]$OutputDirectory,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $ProjectRoot "data\sources\azerothcore\3.3.5\enUS\dbc"
}

$ClientRoot = [IO.Path]::GetFullPath($ClientRoot)
$MPQEditorPath = [IO.Path]::GetFullPath($MPQEditorPath)
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$WowExecutable = Join-Path $ClientRoot "Wow.exe"
if (-not (Test-Path -LiteralPath $WowExecutable -PathType Leaf)) {
    throw "WoW.exe was not found beneath $ClientRoot."
}
if (-not (Test-Path -LiteralPath $MPQEditorPath -PathType Leaf)) {
    throw "MPQEditor.exe was not found at $MPQEditorPath."
}

$WowVersion = (Get-Item -LiteralPath $WowExecutable).VersionInfo.FileVersion
$NormalizedVersion = (($WowVersion -split "[^0-9]+") | Where-Object { $_ }) -join "."
if ($NormalizedVersion -ne "3.3.5.12340") {
    throw "The client reports build $NormalizedVersion; this extractor requires 3.3.5.12340."
}

$LocaleRoot = Join-Path $ClientRoot "Data\enUS"
$ArchiveNames = @("locale-enUS.MPQ", "patch-enUS.MPQ", "patch-enUS-2.MPQ", "patch-enUS-3.MPQ")
$Archives = @($ArchiveNames | ForEach-Object { Join-Path $LocaleRoot $_ })
foreach ($Archive in $Archives) {
    if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) {
        throw "Required enUS archive was not found: $Archive"
    }
}

$DBCNames = @(
    "CreatureDisplayInfo.dbc",
    "CreatureDisplayInfoExtra.dbc",
    "CreatureModelData.dbc",
    "FactionTemplate.dbc",
    "Faction.dbc",
    "AreaTable.dbc"
)
$StagingRoot = Join-Path ([IO.Path]::GetTempPath()) ("wqi-dbc-" + [guid]::NewGuid().ToString("N"))
$StagingDBC = Join-Path $StagingRoot "DBFilesClient"
$CommandFile = Join-Path $StagingRoot "extract.txt"
New-Item -ItemType Directory -Path $StagingDBC -Force | Out-Null

function Quote-MPQArgument {
    param([string]$Value)
    if ($Value.Contains('"')) {
        throw "MPQ command arguments may not contain a quote."
    }
    return '"' + $Value + '"'
}

try {
    $BaseArchive = $Archives[0]
    $PatchArguments = ($Archives | ForEach-Object { Quote-MPQArgument $_ }) -join " "
    $Commands = @("openpatch $PatchArguments")
    foreach ($DBCName in $DBCNames) {
        $Commands += "extract $(Quote-MPQArgument $BaseArchive) $(Quote-MPQArgument ("DBFilesClient\" + $DBCName)) $(Quote-MPQArgument $StagingRoot) /fp"
    }
    $Commands += @("close", "exit")
    [IO.File]::WriteAllLines($CommandFile, $Commands, [Text.UTF8Encoding]::new($false))

    $Process = Start-Process -FilePath $MPQEditorPath -ArgumentList @(
        "/console",
        ('"{0}"' -f $CommandFile)
    ) -Wait -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "MPQ Editor exited with code $($Process.ExitCode)."
    }

    $Extracted = @()
    foreach ($DBCName in $DBCNames) {
        $Path = Join-Path $StagingDBC $DBCName
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "MPQ Editor did not extract $DBCName."
        }
        $Stream = [IO.File]::OpenRead($Path)
        try {
            $Reader = [IO.BinaryReader]::new($Stream)
            $Magic = [Text.Encoding]::ASCII.GetString($Reader.ReadBytes(4))
            $Records = $Reader.ReadUInt32()
            $Fields = $Reader.ReadUInt32()
            $RecordSize = $Reader.ReadUInt32()
            $StringBlockSize = $Reader.ReadUInt32()
        } finally {
            $Stream.Dispose()
        }
        $ExpectedLength = 20L + ([long]$Records * $RecordSize) + $StringBlockSize
        $ActualLength = (Get-Item -LiteralPath $Path).Length
        if ($Magic -ne "WDBC" -or $RecordSize -ne ($Fields * 4) -or $ActualLength -ne $ExpectedLength) {
            throw "$DBCName failed WDBC structural validation."
        }
        $Extracted += [ordered]@{
            name = $DBCName
            bytes = $ActualLength
            sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
            records = $Records
            fields = $Fields
            record_size = $RecordSize
            string_block_size = $StringBlockSize
        }
    }

    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $FinalDBC = Join-Path $OutputDirectory "DBFilesClient"
    New-Item -ItemType Directory -Path $FinalDBC -Force | Out-Null
    foreach ($Artifact in $Extracted) {
        $Source = Join-Path $StagingDBC $Artifact.name
        $Destination = Join-Path $FinalDBC $Artifact.name
        if (Test-Path -LiteralPath $Destination -PathType Leaf) {
            $ExistingHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($ExistingHash -ne $Artifact.sha256 -and -not $Force) {
                throw "$Destination differs from the new extraction. Re-run with -Force to replace it."
            }
        }
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }

    $ArchiveArtifacts = @()
    foreach ($Archive in $Archives) {
        $ArchiveInfo = Get-Item -LiteralPath $Archive
        $ArchiveArtifacts += [ordered]@{
            name = $ArchiveInfo.Name
            bytes = $ArchiveInfo.Length
            sha256 = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
            last_write_utc = $ArchiveInfo.LastWriteTimeUtc.ToString("o")
        }
    }
    $EditorVersion = (Get-Item -LiteralPath $MPQEditorPath).VersionInfo.FileVersion
    $Manifest = [ordered]@{
        schema_version = 1
        extracted_at = [DateTime]::UtcNow.ToString("o")
        expansion = "3.3.5"
        locale = "enUS"
        client_build = 12340
        client_version = $NormalizedVersion
        client_root = $ClientRoot
        extractor = [ordered]@{
            name = "Ladik's MPQ Editor"
            version = $EditorVersion
            path = $MPQEditorPath
            mode = "openpatch"
        }
        archives = $ArchiveArtifacts
        files = $Extracted
    }
    $ManifestPath = Join-Path $OutputDirectory "source-manifest.json"
    [IO.File]::WriteAllText(
        $ManifestPath,
        (($Manifest | ConvertTo-Json -Depth 8) + "`n"),
        [Text.UTF8Encoding]::new($false)
    )
    Write-Output ($Manifest | ConvertTo-Json -Depth 8)
} finally {
    if (Test-Path -LiteralPath $StagingRoot) {
        $ResolvedStaging = [IO.Path]::GetFullPath($StagingRoot)
        $ResolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if ($ResolvedStaging.StartsWith($ResolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $ResolvedStaging).StartsWith("wqi-dbc-")) {
            Remove-Item -LiteralPath $ResolvedStaging -Recurse -Force
        }
    }
}

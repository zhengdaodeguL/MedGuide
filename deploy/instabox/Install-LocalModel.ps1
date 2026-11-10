$ErrorActionPreference = 'Stop'

$modelDirectory = $env:MEDGUIDE_MODEL_DIR
if (-not $modelDirectory -or -not [IO.Path]::IsPathRooted($modelDirectory)) {
    throw 'Set MEDGUIDE_MODEL_DIR to an absolute persistent directory.'
}

$repository = 'Qwen/Qwen3-1.7B-GGUF'
$revision = '90862c4b9d2787eaed51d12237eafdfe7c5f6077'
$fileName = 'Qwen3-1.7B-Q8_0.gguf'
$expectedBytes = 1834426016
$expectedHash = '061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a'
$downloadUrl = "https://huggingface.co/$repository/resolve/$revision/$fileName"
$modelPath = Join-Path $modelDirectory $fileName

New-Item -ItemType Directory -Path $modelDirectory -Force | Out-Null
if (-not (Test-Path -LiteralPath $modelPath)) {
    $partialPath = "$modelPath.part"
    Invoke-WebRequest -Uri $downloadUrl -OutFile $partialPath -TimeoutSec 900
    if ((Get-Item -LiteralPath $partialPath).Length -ne $expectedBytes) {
        throw 'Model download size does not match the pinned artifact.'
    }
    if ((Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
        throw 'Model SHA-256 verification failed. The file will not be loaded.'
    }
    Move-Item -LiteralPath $partialPath -Destination $modelPath
}

if ((Get-Item -LiteralPath $modelPath).Length -ne $expectedBytes -or
    (Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
    throw 'Existing model does not match the pinned artifact; refusing to overwrite it.'
}

$manifest = [ordered]@{
    repository = $repository
    revision = $revision
    file = $fileName
    bytes = $expectedBytes
    sha256 = $expectedHash
    license = 'Apache-2.0'
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $modelDirectory 'model.lock.json') -Encoding UTF8
Write-Output ('Verified model: ' + $fileName)

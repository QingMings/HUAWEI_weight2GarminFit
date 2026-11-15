# Local OCR for Huawei Health body-composition report screenshots.
# Uses the built-in Windows OCR engine (zh-Hans-CN). No network, no third-party libs.
# Output: UTF-8 text, one line per OCR line, formatted as YYYY|XXXX|TEXT.
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI.
param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [Parameter(Mandatory = $true)][string]$OutFile
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $type) {
    $m = $asTaskGeneric.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    return $t.Result
}

[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null

$lang = New-Object Windows.Globalization.Language "zh-Hans-CN"
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if ($null -eq $engine) {
    throw "zh-Hans-CN OCR engine unavailable. Install the Simplified Chinese OCR language pack."
}

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = foreach ($line in $result.Lines) {
    $w0 = @($line.Words)[0]
    $y = [int]$w0.BoundingRect.Y
    $x = [int]$w0.BoundingRect.X
    $text = (@($line.Words) | ForEach-Object { $_.Text }) -join " "
    "{0:D4}|{1:D4}|{2}" -f $y, $x, $text
}

# Write UTF-8 file instead of stdout to avoid OEM console encoding corruption.
$utf8 = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
[System.IO.File]::WriteAllText($OutFile, ($lines -join "`n"), $utf8)
Write-Output ("OCR_OK lines=" + $result.Lines.Count)

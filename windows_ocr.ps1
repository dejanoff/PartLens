param(
    [Parameter(Mandatory = $true)]
    [string]$ImagePath,

    [string]$Language = "auto"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

$asTaskMethods = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq "AsTask" -and
        $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1
    }

function Wait-WinRtResult {
    param(
        [Parameter(Mandatory = $true)]$Operation,
        [Parameter(Mandatory = $true)][Type]$ResultType
    )

    $method = $asTaskMethods | Where-Object {
        $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1"
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    try {
        $task.Wait()
    }
    catch {
        if ($null -ne $task.Exception) {
            $details = ($task.Exception.Flatten().InnerExceptions |
                ForEach-Object { $_.ToString() }) -join "`n"
            throw $details
        }
        throw
    }
    return $task.Result
}

$resolvedPath = (Resolve-Path -LiteralPath $ImagePath).Path
$fileStream = [System.IO.File]::OpenRead($resolvedPath)
$stream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($fileStream)
$decoderOperation = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
$decoder = Wait-WinRtResult $decoderOperation ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmapOperation = $decoder.GetSoftwareBitmapAsync()
$bitmap = Wait-WinRtResult $bitmapOperation ([Windows.Graphics.Imaging.SoftwareBitmap])

$available = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages
$selected = $null
if ($Language -ne "auto") {
    $selected = $available | Where-Object { $_.LanguageTag -eq $Language } | Select-Object -First 1
}
if ($null -eq $selected) {
    $selected = $available | Where-Object { $_.LanguageTag -eq "de-DE" } | Select-Object -First 1
}
if ($null -eq $selected) {
    $selected = $available | Select-Object -First 1
}
if ($null -eq $selected) {
    throw "No Windows OCR language is installed."
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($selected)
if ($null -eq $engine) {
    throw "Windows OCR engine could not be created for $($selected.LanguageTag)."
}

$resultOperation = $engine.RecognizeAsync($bitmap)
$result = Wait-WinRtResult $resultOperation ([Windows.Media.Ocr.OcrResult])
$result.Text

$bitmap.Dispose()
$stream.Dispose()
$fileStream.Dispose()

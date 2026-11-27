param([int]$Seconds = 10, [int]$Baud = 115200, [switch]$Reset)
# One-shot console sniffer: CH340 -> board UART0, 115200 8N1.
$sp = New-Object System.IO.Ports.SerialPort
$sp.PortName = 'COM5'
$sp.BaudRate = $Baud
$sp.Parity = 'None'
$sp.DataBits = 8
$sp.StopBits = 'One'
$sp.ReadTimeout = 300
$sp.DtrEnable = [bool]$Reset
$sp.RtsEnable = [bool]$Reset
$sp.Open()
$sw = [Diagnostics.Stopwatch]::StartNew()
$text = New-Object Text.StringBuilder
while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    try {
        $chunk = $sp.ReadExisting()
        if ($chunk) { [void]$text.Append($chunk) }
    } catch {}
    Start-Sleep -Milliseconds 60
}
$sp.Close()
$raw = $text.ToString()
Write-Output ("captured bytes: " + $raw.Length)
$raw -split "`r?`n" | Where-Object { $_ -ne '' } | Select-Object -First 90

param([Parameter(Mandatory = $true)][string]$Port, [int]$Seconds = 10, [int]$Baud = 115200, [switch]$Reset)
# One-shot console sniffer: CH340 -> board UART0, 115200 8N1.
# The port has no default on purpose: a hard-coded COMx is one machine's wiring, and
# sniffing the wrong port just returns silence that looks like a dead board.
$sp = New-Object System.IO.Ports.SerialPort
$sp.PortName = $Port
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

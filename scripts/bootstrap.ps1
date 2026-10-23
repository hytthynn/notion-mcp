param([ValidateSet('install','setup','start','stop')][string]$Action = 'install', [switch]$Preview)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = $OutputEncoding; [Console]::InputEncoding = $OutputEncoding } catch {}

. (Join-Path $PSScriptRoot 'console.ps1')
Write-BridgeHeader $Action $Root
if ($Preview) { Write-BridgeFooter 0; exit 0 }

function Find-Python {
    foreach ($name in @('py', 'python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $prefix = @()
        if ($name -eq 'py') { $prefix = @('-3') }
        # Do not activate Microsoft Store execution aliases.
        if ($name -ne 'py' -and $cmd.Source -match '\\Microsoft\\WindowsApps\\') { continue }
        try {
            $output = & $cmd.Source @prefix -c "import sys; print(sys.executable) if sys.version_info >= (3,10) else sys.exit(1)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $output) {
                return [string]($output | Select-Object -Last 1)
            }
        } catch { continue }
    }
    # An installation may succeed before py/Python appears in this process's PATH.
    foreach ($pattern in @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe")) {
        foreach ($file in (Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            try {
                & $file.FullName -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) { return $file.FullName }
            } catch { continue }
        }
    }
    return $null
}

try {
    $prefix = @()
    if ($Action -eq 'install') {
        # Full version/dependency inspection and installation belong here only.
        $Python = Find-Python
        if (-not $Python) {
            Write-Host '[НЕ НАЙДЕНО] Требуется Python 3.10 или новее.' -ForegroundColor Yellow
            $answer = Read-Host 'Установить Python 3.13 через winget? [да/НЕТ]'
            if ($answer -notin @('y','yes','д','да')) { Write-BridgeFooter 1; exit 1 }
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if (-not $winget) {
                Write-Host 'Установите Python с https://www.python.org/downloads/windows/ и повторите попытку.' -ForegroundColor Red
                Write-BridgeFooter 1
                exit 1
            }
            & $winget.Source install --id Python.Python.3.13 --exact --source winget
            $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ';' + $env:Path
            $Python = Find-Python
            if (-not $Python) { Write-Host 'Python недоступен. Откройте окно заново.'; Write-BridgeFooter 1; exit 1 }
        }
    } else {
        # This only reads the saved installer result; it never executes version probes.
        $receipt = $null
        $receiptPath = Join-Path $Root 'config\dependencies.checked.json'
        try { $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
        $valid = $receipt -and $receipt.schema -eq 1 -and $receipt.ok -eq $true -and $receipt.checked_at -and $receipt.python_binary
        $Python = $null
        if ($valid -and (Test-Path -LiteralPath $receipt.python_binary -PathType Leaf)) {
            $Python = [string]$receipt.python_binary
            Write-Host ('    ✓ Все зависимости установлены · проверены ' + $receipt.checked_at) -ForegroundColor Green
            Write-Host ''
        } elseif ($Action -eq 'stop') {
            # Stopping must remain possible even when the installation record is missing.
            Write-Host '    Нет сохранённой проверки; остановка всё равно доступна.' -ForegroundColor Yellow
            foreach ($name in @('py','python')) {
                $cmd = Get-Command $name -ErrorAction SilentlyContinue
                if ($cmd -and -not ($name -ne 'py' -and $cmd.Source -match '\\Microsoft\\WindowsApps\\')) {
                    $Python = $cmd.Source
                    if ($name -eq 'py') { $prefix = @('-3') }
                    break
                }
            }
        } else {
            Write-Host '    Сначала запустите install_dependencies.bat.' -ForegroundColor Red
            Write-BridgeFooter 1
            exit 1
        }
        if (-not $Python) { Write-Host 'Python не найден; запустите install_dependencies.bat.' -ForegroundColor Red; Write-BridgeFooter 1; exit 1 }
    }
    & $Python @prefix (Join-Path $PSScriptRoot 'manage.py') $Action
    $result = $LASTEXITCODE
    Write-BridgeFooter $result
    exit $result
} catch {
    Write-Host ('Статус: ОШИБКА — ' + $_.Exception.Message) -ForegroundColor Red
    Write-BridgeFooter 1
    exit 1
}

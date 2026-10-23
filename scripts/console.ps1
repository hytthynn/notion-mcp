# Единое оформление для всех BAT-сценариев. Без изменения размеров окна и реестра.
function Write-BridgeHeader([string]$Action, [string]$Project) {
    $labels = @{ install='ПРОВЕРКА И УСТАНОВКА'; setup='НАСТРОЙКА ПОДКЛЮЧЕНИЯ'; start='ЗАПУСК MCP-СЕРВЕРА'; stop='ОСТАНОВКА MCP-СЕРВЕРА' }
    $accent = 'Cyan'
    Write-Host ''
    Write-Host '  ╔══════════════════════════════════════════════════════════════════════╗' -ForegroundColor DarkCyan
    Write-Host '  ║                         NOTION · MCP МОСТ                            ║' -ForegroundColor $accent
    Write-Host '  ╚══════════════════════════════════════════════════════════════════════╝' -ForegroundColor DarkCyan
    Write-Host ('    ' + $labels[$Action]) -ForegroundColor White
    Write-Host ('    Папка: ' + $Project) -ForegroundColor DarkGray
    Write-Host '  ────────────────────────────────────────────────────────────────────────' -ForegroundColor DarkCyan
    Write-Host ''
}

function Write-BridgeFooter([int]$Code) {
    Write-Host ''
    Write-Host '  ────────────────────────────────────────────────────────────────────────' -ForegroundColor DarkCyan
    if ($Code -eq 0) {
        Write-Host '    ✓ ГОТОВО  ·  Операция завершена без ошибок' -ForegroundColor Green
    } elseif ($Code -eq 130) {
        Write-Host '    ■ ОТМЕНЕНО  ·  Выполнение прервано пользователем' -ForegroundColor Yellow
    } else {
        Write-Host ('    ! ОШИБКА  ·  Код завершения: ' + $Code) -ForegroundColor Red
        Write-Host '    Подробности — в сообщениях выше и в папке runtime.' -ForegroundColor DarkGray
    }
    Write-Host '  ────────────────────────────────────────────────────────────────────────' -ForegroundColor DarkCyan
    Write-Host ''
}

# Inicializador compatível com Windows PowerShell 5.1 e PowerShell 7.
param([switch]$Verificar)
$ErrorActionPreference = 'Stop'
$raizHarpia = $PSScriptRoot
$backendHarpia = Join-Path $raizHarpia 'backend'
$pythonHarpia = Join-Path $backendHarpia '.venv\Scripts\python.exe'

try {
    if (-not (Test-Path -LiteralPath $pythonHarpia)) {
        if ($Verificar) { throw 'Ambiente Python ainda nao preparado. Execute Iniciar-HarpIA.bat.' }
        Write-Host 'Preparando o ambiente Python...'
        $pythonBaseHarpia = Get-Command py -ErrorAction SilentlyContinue
        if ($pythonBaseHarpia) {
            & $pythonBaseHarpia.Source -3 -m venv (Join-Path $backendHarpia '.venv')
        } else {
            $pythonBaseHarpia = Get-Command python -ErrorAction SilentlyContinue
            if (-not $pythonBaseHarpia) { throw 'Python nao encontrado. Instale Python 3.11 ou posterior e execute novamente.' }
            & $pythonBaseHarpia.Source -m venv (Join-Path $backendHarpia '.venv')
        }
        if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel criar o ambiente Python.' }
    }
    & $pythonHarpia -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'O ambiente precisa de Python 3.11 ou posterior.' }
    & $pythonHarpia -c 'import fastapi, uvicorn, ollama, httpx, pydantic'
    if ($LASTEXITCODE -ne 0) {
        if ($Verificar) { throw 'Dependencias ausentes. Execute Iniciar-HarpIA.bat para instalar.' }
        Write-Host 'Instalando as dependencias da HarpIA...'
        & $pythonHarpia -m pip install -r (Join-Path $backendHarpia 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Falha na instalacao das dependencias. Confira sua conexao.' }
    }
    if ($Verificar) {
        & $pythonHarpia (Join-Path $backendHarpia 'iniciar.py') --verificar
    } else {
        & $pythonHarpia (Join-Path $backendHarpia 'iniciar.py')
    }
    exit $LASTEXITCODE
} catch {
    Write-Host ('Erro: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

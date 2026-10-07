@echo off
rem bin/oadm.cmd —— oadm CLI 启动壳（docs/ops/04 §5：Windows 语义对齐，内核同为 uv run）
rem 用法：bin\oadm.cmd <command> [options]；等价 uv run python -m services.cli.oadm ...
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
uv run python -m services.cli.oadm %*
endlocal & exit /b %ERRORLEVEL%

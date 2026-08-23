@echo off
rem luaudit hook launcher for Windows (v1.0.0)
rem Harnesses on Windows run hooks via cmd; codex 0.147 executes plugin hooks
rem as `cmd /C ""<command_windows>""` and pre-expands ${CLAUDE_PLUGIN_ROOT}.
rem The extensionless/${CLAUDE_PLUGIN_ROOT}/forward-slash and %~dp0%/pushd
rem forms break under that nested quoting (path gets prefixed with the caller
rem CWD or the leading quote swallowed). The robust form: use the plugin-root
rem env var codex sets (%CLAUDE_PLUGIN_ROOT% is stable and absolute), build the
rem engine path from it, never from %~dp0 of this script.
setlocal
if not defined CLAUDE_PLUGIN_ROOT (
  if defined PLUGIN_ROOT set "CLAUDE_PLUGIN_ROOT=%PLUGIN_ROOT%"
)
if not defined CLAUDE_PLUGIN_ROOT (
  echo luaudit: CLAUDE_PLUGIN_ROOT is not set, cannot locate engine 1>&2
  exit /b 9
)
set "ENGINE=%CLAUDE_PLUGIN_ROOT%\scripts\luaudit_hook.py"
if not exist "%ENGINE%" (
  echo luaudit: engine not found at "%ENGINE%" 1>&2
  exit /b 8
)

rem Forward any launcher arguments (e.g. "stop-hook") to the engine.
rem Prefer known real installs (harness hook envs may only expose the broken
rem WindowsApps python alias on PATH).
if exist "C:\Program Files\Python312\python.exe" (
  "C:\Program Files\Python312\python.exe" "%ENGINE%" %*
  exit /b %errorlevel%
)
if exist "C:\Python312\python.exe" (
  "C:\Python312\python.exe" "%ENGINE%" %*
  exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel% equ 0 (
  python "%ENGINE%" %*
  exit /b %errorlevel%
)
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 "%ENGINE%" %*
  exit /b %errorlevel%
)

rem No python on PATH: fall back to git-bash, which ships with the harnesses.
if exist "C:\Program Files\Git\bin\bash.exe" (
  "C:\Program Files\Git\bin\bash.exe" "%CLAUDE_PLUGIN_ROOT%\scripts\luaudit-hook.sh" %*
  exit /b %errorlevel%
)
echo luaudit: python not found on Windows 1>&2
exit /b 1

@echo off
rem luaudit hook launcher for Windows (v1.0.0)
rem Harnesses on Windows run hooks via cmd; codex 0.147 executes plugin hooks
rem as `cmd /C ""<command_windows>""` and pre-expands ${CLAUDE_PLUGIN_ROOT}.
rem The extensionless/${CLAUDE_PLUGIN_ROOT}/forward-slash and %~dp0%/pushd
rem forms break under that nested quoting (path gets prefixed with the caller
rem CWD or the leading quote swallowed). The robust form: use the plugin-root
rem env var codex sets (%CLAUDE_PLUGIN_ROOT% is stable and absolute), build the
rem engine path from it, never from %~dp0 of this script.
setlocal EnableDelayedExpansion
rem Engine resolution order: harness-provided plugin root vars first, then
rem this script's own directory (%~dp0). The %~dp0 fallback makes the
rem launcher work under any harness that manages to execute it, including
rem user-level hook configs and direct calls where no plugin env exists.
if not defined CLAUDE_PLUGIN_ROOT (
  if defined PLUGIN_ROOT set "CLAUDE_PLUGIN_ROOT=%PLUGIN_ROOT%"
)
if not defined CLAUDE_PLUGIN_ROOT (
  set "CLAUDE_PLUGIN_ROOT=%~dp0.."
)
set "ENGINE=%CLAUDE_PLUGIN_ROOT%\scripts\luaudit_hook.py"
if not exist "%ENGINE%" (
  echo luaudit: engine not found at "%ENGINE%" 1>&2
  exit /b 8
)

rem Forward any launcher arguments (e.g. "stop-hook") to the engine.
rem Prefer known real installs (harness hook envs may only expose the broken
rem WindowsApps python alias on PATH). Any Python3xx install location is
rem accepted, not just 3.12.
for %%V in (313 312 311 310) do (
  if exist "C:\Program Files\Python%%V\python.exe" (
    "C:\Program Files\Python%%V\python.exe" "%ENGINE%" %*
    exit /b !errorlevel!
  )
  if exist "C:\Python%%V\python.exe" (
    "C:\Python%%V\python.exe" "%ENGINE%" %*
    exit /b !errorlevel!
  )
)

rem py launcher knows about every registered interpreter and never resolves to
rem a Store stub; prefer it over raw `where python` for that reason.
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 "%ENGINE%" %*
  exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel% equ 0 (
  python "%ENGINE%" %*
  exit /b %errorlevel%
)

rem No python on PATH: fall back to git-bash, which ships with the harnesses.
if exist "C:\Program Files\Git\bin\bash.exe" (
  "C:\Program Files\Git\bin\bash.exe" "%CLAUDE_PLUGIN_ROOT%\scripts\luaudit-hook.sh" %*
  exit /b %errorlevel%
)
echo luaudit: python not found on Windows 1>&2
exit /b 1

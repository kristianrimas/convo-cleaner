@echo off
rem pushd so the tests can import convo_cleaner from this folder
pushd "%~dp0"
python -m unittest -v test_convo_cleaner
if errorlevel 1 (
    echo.
    echo Tests failed - not starting ConvoCleaner.
    popd
    pause
    exit /b 1
)
popd
python "%~dp0convo_cleaner.py"
pause

@echo off
REM Power Trading Backend Runner Script for Windows

setlocal enabledelayedexpansion

REM Colors aren't available in standard batch, using echo messages instead
echo.
echo ========================================
echo Power Trading Backend Startup
echo ========================================
echo.

REM Check if venv exists
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

REM Activate venv
call venv\Scripts\activate.bat

REM Install dependencies if needed
echo Installing/updating dependencies...
pip install -r requirements.txt

REM Check if .env exists
if not exist ".env" (
    echo Creating .env file from template...
    copy .env.example .env
    echo.
    echo IMPORTANT: Please update .env with your database credentials
    echo.
)

REM Run the application
echo.
echo Starting Power Trading Backend...
echo API Documentation will be available at http://localhost:8000/docs
echo.
python -m app.main

pause

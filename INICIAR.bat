@echo off
echo.
echo  CrossList Pro — A instalar e arrancar...
echo.

pip install -r requirements.txt --quiet
playwright install chromium --quiet

echo.
echo  A iniciar servidor...
python app.py
pause

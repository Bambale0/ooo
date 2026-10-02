@echo off
REM Скрипт для добавления SSH ключа на production сервер (Windows)
REM Использовать в cmd.exe или Git Bash

echo ====================================
echo Настройка SSH ключа для production
echo ====================================
echo.

REM Показываем публичный ключ
echo Ваш публичный ключ:
echo ----------------------------------------
type %USERPROFILE%\.ssh\neironych_prod_ed25519.pub
echo ----------------------------------------
echo.

echo ИНСТРУКЦИЯ:
echo.
echo 1. Подключитесь к серверу вручную:
echo    ssh root@5.35.124.201
echo    Пароль: 25896311
echo.
echo 2. Выполните на сервере:
echo    mkdir -p ~/.ssh
echo    chmod 700 ~/.ssh
echo    nano ~/.ssh/authorized_keys
echo.
echo 3. Добавьте в конец файла строку выше (публичный ключ)
echo.
echo 4. Сохраните (Ctrl+O, Enter, Ctrl+X) и выполните:
echo    chmod 600 ~/.ssh/authorized_keys
echo.
echo 5. Отключитесь (exit) и проверьте:
echo    ssh root@5.35.124.201
echo    (должно подключиться БЕЗ пароля)
echo.
pause

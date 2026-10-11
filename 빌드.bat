@echo off
cd /d "%~dp0"
echo [1/3] ffmpeg.exe 준비
if not exist ffmpeg.exe (
  for /f "delims=" %%p in ('python -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"') do copy /y "%%p" ffmpeg.exe >nul
)
if not exist ffmpeg.exe (
  echo ffmpeg.exe 를 찾지 못했습니다. pip install imageio-ffmpeg 를 실행하거나 ffmpeg.exe 를 이 폴더에 넣어 주세요.
  pause
  exit /b 1
)
echo [2/3] 실행파일 만들기 (1~3분)
pyinstaller --noconfirm --clean --onefile --windowed --name "동영상변환기" --add-binary "ffmpeg.exe;." --collect-all tkinterdnd2 --exclude-module imageio_ffmpeg video_converter.py
if errorlevel 1 (
  echo 빌드 실패
  pause
  exit /b 1
)
echo [3/3] 완료: dist\동영상변환기.exe
pause

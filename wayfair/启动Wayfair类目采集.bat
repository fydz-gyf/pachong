@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Wayfair Scraper

set "PROJECT_DIR=%~dp0"
set "IMAGE_TOOL_DIR=%~dp0"

cls
echo ============================================================
echo                Wayfair Category Product Scraper
echo ============================================================
echo.
echo Project directory:
echo %PROJECT_DIR%
echo.

set "HTTP_RUNNER=%PROJECT_DIR%\run_http.ps1"
if not exist "%HTTP_RUNNER%" (
    set "HTTP_RUNNER=%PROJECT_DIR%\run_wayfair_http.ps1"
)

if not exist "%HTTP_RUNNER%" (
    echo [ERROR] HTTP scraper runner was not found:
    echo %PROJECT_DIR%\run_http.ps1
    echo.
    echo Confirm the Wayfair HTTP scraper files are installed.
    pause
    exit /b 1
)

:choose_mode
echo Select scraper mode:
echo   1 = Product list
echo   2 = Reviews from existing product list
echo.
set "RUN_MODE="
set /p "RUN_MODE=Select mode 1 or 2 (default 1): "
if not defined RUN_MODE set "RUN_MODE=1"
if "%RUN_MODE%"=="1" goto :list_mode
if "%RUN_MODE%"=="2" goto :review_mode
echo.
echo [ERROR] Enter 1 or 2.
echo.
goto :choose_mode

:list_mode
set "CATEGORY_URL="
set /p "CATEGORY_URL=Paste category or search URL: "
if not defined CATEGORY_URL (
    echo.
    echo [ERROR] Category URL cannot be empty.
    pause
    exit /b 1
)
set "CATEGORY_URL=%CATEGORY_URL:"=%"

echo.
echo Example page ranges:
echo   1-2   = scrape pages 1 through 2
echo   1-50  = scrape pages 1 through 50
echo   20-30 = scrape pages 20 through 30
echo.

set "PAGE_RANGE=1-50"
set /p "PAGE_RANGE=Page range (default 1-50): "
if not defined PAGE_RANGE set "PAGE_RANGE=1-50"

set "START_PAGE="
set "END_PAGE="
for /f "tokens=1,2 delims=- " %%A in ("%PAGE_RANGE%") do (
    set "START_PAGE=%%A"
    set "END_PAGE=%%B"
)

if not defined START_PAGE set "START_PAGE=1"
if not defined END_PAGE set "END_PAGE=%START_PAGE%"

for /f %%I in ('powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"

set "RESULT_NAME="
set /p "RESULT_NAME=Result name (Enter for automatic name): "
if not defined RESULT_NAME set "RESULT_NAME=wayfair_%STAMP%"

set "DEFAULT_OUTPUT=%PROJECT_DIR%\results\%RESULT_NAME%"
set "OUTPUT_DIR="
set /p "OUTPUT_DIR=Output directory (Enter for %DEFAULT_OUTPUT%): "
if not defined OUTPUT_DIR set "OUTPUT_DIR=%DEFAULT_OUTPUT%"
set "OUTPUT_DIR=%OUTPUT_DIR:"=%"

set "NEW_TASK=Y"
set /p "NEW_TASK=Start as a new task and clear old checkpoints? [Y/n]: "
if not defined NEW_TASK set "NEW_TASK=Y"

set "KEEP_HTML=N"
set /p "KEEP_HTML=Save raw HTML for each page? [y/N]: "
if not defined KEEP_HTML set "KEEP_HTML=N"

set "ENABLE_CATEGORY=N"
echo.
echo Enable sub-category sheets? [y/N]
echo   When enabled, discover sub-categories under this URL, one sheet per category.
echo   Products not matched by a sub-category are written to Other.
set /p "ENABLE_CATEGORY=Enable sub-categories? "
if not defined ENABLE_CATEGORY set "ENABLE_CATEGORY=N"

set "CATEGORY_PAGES=1"
if /I "%ENABLE_CATEGORY%"=="Y" (
    set /p "CATEGORY_PAGES=Pages per sub-category (default 1): "
)
if /I "%ENABLE_CATEGORY%"=="YES" (
    set /p "CATEGORY_PAGES=Pages per sub-category (default 1): "
)
if not defined CATEGORY_PAGES set "CATEGORY_PAGES=1"

set "EMBED_IMAGES=Y"
set /p "EMBED_IMAGES=Download and embed product images after scraping? [Y/n]: "
if not defined EMBED_IMAGES set "EMBED_IMAGES=Y"

set "DATA_SHEET=UniqueProducts"
set "CHUNK_SIZE=300"
set "MAX_WORKERS=6"

if /I "%EMBED_IMAGES%"=="Y" goto :ask_image_options
if /I "%EMBED_IMAGES%"=="YES" goto :ask_image_options
goto :show_summary

:ask_image_options
echo.
echo Image Excel data range:
echo   1 = UniqueProducts      deduplicate by SKU, recommended
echo   2 = ListingOccurrences keep repeated products and ad slots
set "SHEET_CHOICE=1"
set /p "SHEET_CHOICE=Choose (default 1): "
if "%SHEET_CHOICE%"=="2" set "DATA_SHEET=ListingOccurrences"

set /p "CHUNK_SIZE=Maximum products per image workbook (default 300): "
if not defined CHUNK_SIZE set "CHUNK_SIZE=300"

set /p "MAX_WORKERS=Image download worker count (default 6): "
if not defined MAX_WORKERS set "MAX_WORKERS=6"

:show_summary
cls
echo ============================================================
echo                    Task parameters
echo ============================================================
echo.
echo Category URL:
echo %CATEGORY_URL%
echo.
echo Pages: %START_PAGE% through %END_PAGE%
echo Output directory: %OUTPUT_DIR%
echo New task / clear checkpoint: %NEW_TASK%
echo Save raw HTML: %KEEP_HTML%
echo Download and embed images: %EMBED_IMAGES%
echo Sub-categories (separate sheets): %ENABLE_CATEGORY%
if /I "%ENABLE_CATEGORY%"=="Y" echo Pages per sub-category: %CATEGORY_PAGES%
if /I "%ENABLE_CATEGORY%"=="YES" echo Pages per sub-category: %CATEGORY_PAGES%

if /I "%EMBED_IMAGES%"=="Y" (
    echo Image data sheet: %DATA_SHEET%
    echo Products per workbook: %CHUNK_SIZE%
    echo Image download workers: %MAX_WORKERS%
)
if /I "%EMBED_IMAGES%"=="YES" (
    echo Image data sheet: %DATA_SHEET%
    echo Products per workbook: %CHUNK_SIZE%
    echo Image download workers: %MAX_WORKERS%
)

echo.
set "CONFIRM=Y"
set /p "CONFIRM=Confirm start? [Y/n]: "
if not defined CONFIRM set "CONFIRM=Y"

if /I "%CONFIRM%"=="N" (
    echo Cancelled.
    pause
    exit /b 0
)
if /I "%CONFIRM%"=="NO" (
    echo Cancelled.
    pause
    exit /b 0
)

if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

set "FRESH_ARG="
set "HTML_ARG="
set "CATEGORY_ARG="
set "CATEGORY_PAGES_ARG="

if /I "%NEW_TASK%"=="Y" set "FRESH_ARG=-Fresh"
if /I "%NEW_TASK%"=="YES" set "FRESH_ARG=-Fresh"
if /I "%KEEP_HTML%"=="Y" set "HTML_ARG=-KeepHtml"
if /I "%KEEP_HTML%"=="YES" set "HTML_ARG=-KeepHtml"
if /I "%ENABLE_CATEGORY%"=="Y" set "CATEGORY_ARG=-Categorize"
if /I "%ENABLE_CATEGORY%"=="YES" set "CATEGORY_ARG=-Categorize"
if defined CATEGORY_ARG set "CATEGORY_PAGES_ARG=-CategoryPages %CATEGORY_PAGES%"

set "EMBED_CATEGORIZED="
if defined CATEGORY_ARG set "EMBED_CATEGORIZED=--categorized"

echo.
echo ============================================================
echo                       Start scraping
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
  -File "%HTTP_RUNNER%" ^
  -CategoryUrl "%CATEGORY_URL%" ^
  -StartPage %START_PAGE% ^
  -EndPage %END_PAGE% ^
  -OutputDir "%OUTPUT_DIR%" ^
  %FRESH_ARG% %HTML_ARG% %CATEGORY_ARG% %CATEGORY_PAGES_ARG%

if errorlevel 1 goto :scrape_failed

echo.
echo [OK] Product scraping completed.
if defined CATEGORY_ARG (
    echo Category workbook (one sheet per sub-category):
    echo %OUTPUT_DIR%\wayfair_categorized_by_category.xlsx
    echo.
)
echo Excel:
echo %OUTPUT_DIR%\wayfair_products.xlsx
echo.

if /I "%EMBED_IMAGES%"=="Y" goto :embed_images
if /I "%EMBED_IMAGES%"=="YES" goto :embed_images
goto :completed

:embed_images
set "EMBED_RUNNER=%PROJECT_DIR%\run_embed_images.ps1"
if not exist "%EMBED_RUNNER%" (
    echo Image embedder not found; attempting automatic installation...
    set "IMAGE_INSTALLER=%IMAGE_TOOL_DIR%\install_image_embedder.ps1"
    if not exist "%IMAGE_INSTALLER%" (
        echo.
        echo [ERROR] Image embedder installer was not found:
        echo %IMAGE_INSTALLER%
        echo.
        echo Product data was scraped successfully, but images were not embedded.
        goto :completed
    )
    powershell.exe -NoProfile -ExecutionPolicy Bypass ^
      -File "%IMAGE_INSTALLER%" ^
      -ProjectDir "%PROJECT_DIR%"
    if errorlevel 1 (
        echo.
        echo [ERROR] Image embedder installation failed.
        echo Product data was scraped successfully.
        goto :completed
    )
)

set "INPUT_XLSX=%OUTPUT_DIR%\wayfair_products.xlsx"
if defined EMBED_CATEGORIZED set "INPUT_XLSX=%OUTPUT_DIR%\wayfair_categorized_by_category.xlsx"

if not exist "%INPUT_XLSX%" (
    echo.
    echo [ERROR] Scraped workbook was not found:
    echo %INPUT_XLSX%
    goto :completed
)

echo.
echo ============================================================
echo                   Start image download and embedding
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
  -File "%EMBED_RUNNER%" ^
  -ProjectDir "%PROJECT_DIR%" ^
  -InputFile "%INPUT_XLSX%" ^
  -Sheet "%DATA_SHEET%" ^
  -ChunkSize %CHUNK_SIZE% ^
  -MaxWorkers %MAX_WORKERS% ^
  %EMBED_CATEGORIZED%

if errorlevel 1 (
    echo.
    echo [ERROR] Image download or embedding failed.
    echo Product data is still available at:
    echo %INPUT_XLSX%
    goto :completed
)

echo.
if defined EMBED_CATEGORIZED (
    echo [OK] Categorized workbooks with images were generated:
    echo %OUTPUT_DIR%\with_images_categorized
) else (
    echo [OK] Workbook with images was generated:
    echo %OUTPUT_DIR%\with_images
)
echo.

:completed
echo ============================================================
echo                         All done
echo ============================================================
echo.
echo Base output directory:
echo %OUTPUT_DIR%
echo.
echo Main files:
echo %OUTPUT_DIR%\wayfair_products.xlsx
echo %OUTPUT_DIR%\wayfair_unique_products.csv
echo %OUTPUT_DIR%\wayfair_listing_occurrences.csv
if defined CATEGORY_ARG (
    echo Category workbook (one sheet per sub-category):
    echo %OUTPUT_DIR%\wayfair_categorized_by_category.xlsx
)
echo.
if exist "%OUTPUT_DIR%\with_images" (
    echo Workbook with images:
    echo %OUTPUT_DIR%\with_images
    echo.
)
if exist "%OUTPUT_DIR%\with_images_categorized" (
    echo Categorized workbooks with images:
    echo %OUTPUT_DIR%\with_images_categorized
    echo.
)
set "OPEN_FOLDER=Y"
set /p "OPEN_FOLDER=Open the output folder? [Y/n]: "
if not defined OPEN_FOLDER set "OPEN_FOLDER=Y"
if /I "%OPEN_FOLDER%"=="Y" start "" "%OUTPUT_DIR%"
if /I "%OPEN_FOLDER%"=="YES" start "" "%OUTPUT_DIR%"
pause
exit /b 0

:scrape_failed
echo.
echo ============================================================
echo                         Scraping failed
echo ============================================================
echo.
echo Check these files in the output directory:
echo   failed_page_*.html
echo   unexpected_page_*.html
echo   checkpoint_http.json
echo.
echo Output directory:
echo %OUTPUT_DIR%
echo.
pause
exit /b 1

:review_mode
cls
echo ============================================================
echo                    Review scraping mode
echo ============================================================
echo.
echo This mode does not fetch category pages again; it reads the existing result directory:
echo   wayfair_unique_products.jsonl
echo.
set "OUTPUT_DIR="
set /p "OUTPUT_DIR=Enter an existing result directory: "
if not defined OUTPUT_DIR (
    echo.
    echo [ERROR] Result directory cannot be empty.
    pause
    exit /b 1
)
set "OUTPUT_DIR=%OUTPUT_DIR:"=%"

if not exist "%OUTPUT_DIR%\wayfair_unique_products.jsonl" (
    echo.
    echo [ERROR] Product list was not found:
    echo %OUTPUT_DIR%\wayfair_unique_products.jsonl
    echo Run mode 1 first to create the product list.
    pause
    exit /b 1
)

set "COOKIE_HEADER_FILE="
set /p "COOKIE_HEADER_FILE=Enter Cookie file path or directory containing cookies.txt: "
if not defined COOKIE_HEADER_FILE (
    echo.
    echo [ERROR] Cookie file path cannot be empty.
    pause
    exit /b 1
)
set "COOKIE_HEADER_FILE=%COOKIE_HEADER_FILE:"=%"
if exist "%COOKIE_HEADER_FILE%\cookies.txt" (
    set "COOKIE_HEADER_FILE=%COOKIE_HEADER_FILE%\cookies.txt"
)
if exist "%COOKIE_HEADER_FILE%" (
    echo.
) else (
    echo.
    echo [ERROR] Cookie file was not found:
    echo %COOKIE_HEADER_FILE%
    pause
    exit /b 1
)

set "NEW_REVIEWS=Y"
set /p "NEW_REVIEWS=Scrape all reviews again and clear old review results? [Y/n]: "
if not defined NEW_REVIEWS set "NEW_REVIEWS=Y"

set "REVIEW_HEADLESS=N"
set /p "REVIEW_HEADLESS=Use headless Chrome? Default N (N is recommended for verification): "
if not defined REVIEW_HEADLESS set "REVIEW_HEADLESS=N"

set "FRESH_ARG="
set "HEADLESS_ARG="
if /I "%NEW_REVIEWS%"=="Y" set "FRESH_ARG=-Fresh"
if /I "%NEW_REVIEWS%"=="YES" set "FRESH_ARG=-Fresh"
if /I "%REVIEW_HEADLESS%"=="Y" set "HEADLESS_ARG=-ReviewHeadless"
if /I "%REVIEW_HEADLESS%"=="YES" set "HEADLESS_ARG=-ReviewHeadless"

cls
echo ============================================================
echo                    Review task parameters
echo ============================================================
echo.
echo Product list directory:
echo %OUTPUT_DIR%
echo Cookie file:
echo %COOKIE_HEADER_FILE%
echo Clear old review results: %NEW_REVIEWS%
echo Headless Chrome: %REVIEW_HEADLESS%
echo.
set "CONFIRM=Y"
set /p "CONFIRM=Confirm start review scraping? [Y/n]: "
if not defined CONFIRM set "CONFIRM=Y"
if /I "%CONFIRM%"=="N" (
    echo Cancelled.
    pause
    exit /b 0
)
if /I "%CONFIRM%"=="NO" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo ============================================================
echo                       Start review scraping
echo ============================================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass ^
  -File "%HTTP_RUNNER%" ^
  -OutputDir "%OUTPUT_DIR%" ^
  -ScrapeReviews ^
  -ReviewsOnly ^
  -ReviewBrowser ^
  -CookieHeaderFile "%COOKIE_HEADER_FILE%" ^
  %FRESH_ARG% %HEADLESS_ARG%

if errorlevel 1 goto :review_scrape_failed

echo.
echo [OK] Review scraping completed.
echo Product Excel:
echo %OUTPUT_DIR%\wayfair_products.xlsx
echo Reviews Excel:
echo %OUTPUT_DIR%\wayfair_reviews.xlsx
echo Reviews CSV:
echo %OUTPUT_DIR%\wayfair_reviews.csv
echo Review status:
echo %OUTPUT_DIR%\wayfair_review_status.csv
echo.
set "OPEN_FOLDER=Y"
set /p "OPEN_FOLDER=Open the output folder? [Y/n]: "
if not defined OPEN_FOLDER set "OPEN_FOLDER=Y"
if /I "%OPEN_FOLDER%"=="Y" start "" "%OUTPUT_DIR%"
if /I "%OPEN_FOLDER%"=="YES" start "" "%OUTPUT_DIR%"
pause
exit /b 0

:review_scrape_failed
echo.
echo ============================================================
echo                       Review scraping failed
echo ============================================================
echo.
echo Check:
echo   %OUTPUT_DIR%\wayfair_review_status.csv
echo   Did Chrome show a verification page?
echo   Are the Cookies still valid?
echo.
pause
exit /b 1

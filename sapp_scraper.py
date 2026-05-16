import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import openpyxl
from dotenv import load_dotenv
from pymongo import UpdateOne
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.firefox import GeckoDriverManager

BASE_URL = "https://trading.sappmtp.com"
LOGIN_URL = f"{BASE_URL}/account/login?returnUrl=%2F"
INBOX_URL = f"{BASE_URL}/mdd/message-inbox"
DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
MESSAGE_SUBJECT_TEMPLATE = "MTP - DAM - Constrained Area Results for {delivery_date}"
DATA_SOURCE = "SAPP_MTP_DAM_CONSTRAINED_AREA_RESULTS"
MAX_INBOX_PAGES_TO_SEARCH = 25
INBOX_GRID_READY_TIMEOUT = 10
INBOX_PAGE_CHANGE_TIMEOUT = 10
MESSAGE_CLICK_TIMEOUT = 3
NEXT_PAGE_SELECTOR = (
    "button.k-pager-nav[title='Go to the next page'], "
    "button.k-pager-nav[aria-label='Go to the next page']"
)
INBOX_GRID_SELECTOR = ".k-grid, [role='grid']"
INBOX_LOADING_SELECTOR = ".k-loading-mask, .k-i-loading, .k-loading-image"

# stress test for all possible scenarios:


def build_message_subject(delivery_date: date) -> str:
    return MESSAGE_SUBJECT_TEMPLATE.format(delivery_date=delivery_date.strftime("%Y/%m/%d"))


def parse_delivery_date_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def parse_hour(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        hour = int(value)
        return hour if 1 <= hour <= 24 else None

    text = str(value).strip()
    interval_match = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", text)
    if interval_match:
        start_hour = int(interval_match.group(1))
        return start_hour + 1 if 0 <= start_hour <= 23 else None

    match = re.search(r"\d+", text)
    if not match:
        return None
    hour = int(match.group(0))
    return hour if 1 <= hour <= 24 else None


def to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def delivery_timestamp(delivery_date: date, hour: int) -> datetime:
    return datetime.combine(delivery_date, datetime.min.time()) + timedelta(hours=hour - 1)


def load_config():
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)

    username = os.getenv("SAPP_USERNAME")
    password = os.getenv("SAPP_PASSWORD")
    if not username or not password:
        raise ValueError("Please set SAPP_USERNAME and SAPP_PASSWORD as environment variables.")

    print("[1/7] ✅ Loaded credentials from .env")
    return username, password


def create_driver(download_dir: Path):
    download_dir.mkdir(parents=True, exist_ok=True)
    print(f"[2/7] 🔧 Creating Firefox driver and setting download folder to: {download_dir}")

    options = Options()
    options.add_argument("-headless")
    options.add_argument("--width=1366")
    options.add_argument("--height=900")
    options.headless = True
    options.set_preference("browser.download.folderList", 2)
    options.set_preference("browser.download.dir", str(download_dir.resolve()))
    options.set_preference("browser.download.useDownloadDir", True)
    options.set_preference("browser.helperApps.neverAsk.saveToDisk", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/octet-stream")
    options.set_preference("browser.helperApps.alwaysAsk.force", False)
    options.set_preference("browser.download.manager.showWhenStarting", False)
    options.set_preference("pdfjs.disabled", True)
    options.set_preference("browser.download.manager.showWhenStarting", False)
    options.set_preference("browser.download.manager.focusWhenStarting", False)
    options.set_preference("browser.shell.checkDefaultBrowser", False)

    os.environ["MOZ_HEADLESS"] = "1"
    service = Service(executable_path=GeckoDriverManager().install())
    driver = webdriver.Firefox(service=service, options=options)
    driver.set_window_size(1366, 900)
    driver.implicitly_wait(0)
    return driver


def login(driver, username: str, password: str):
    print("[3/7] 🔐 Navigating to login page")
    driver.get(LOGIN_URL)
    # TODO: update selectors according to the actual login page fields
    username_selector = "input[id='login-input-user-name-or-email-address']"
    password_selector = "input[id='password']"
    submit_selector = "button[type='submit']"

    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, username_selector)))

    driver.find_element(By.CSS_SELECTOR, username_selector).send_keys(username)
    driver.find_element(By.CSS_SELECTOR, password_selector).send_keys(password)
    driver.find_element(By.CSS_SELECTOR, submit_selector).click()

    print("[3/7] ⏳ Submitted login form, waiting for redirect")
    time.sleep(2)  # brief pause to allow login processing to start
    try:
        WebDriverWait(driver, 20).until(lambda d: BASE_URL in d.current_url)
        print("[3/7] ✅ Login appears successful")
    except TimeoutException:
        print("⚠️ Warning: login may not have completed successfully. Check selectors or credentials.")


def navigate_to_inbox(driver):
    print("[4/7] 📥 Navigating to inbox")
    driver.get(INBOX_URL)
    wait_for_inbox_grid_to_settle(driver, timeout=INBOX_GRID_READY_TIMEOUT)
    print("[4/7] ✅ Inbox page loaded")


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in parts) + ")"


def wait_for_inbox_grid_to_settle(driver, timeout: int = INBOX_GRID_READY_TIMEOUT):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        WebDriverWait(driver, timeout).until(
            EC.invisibility_of_element_located(
                (By.CSS_SELECTOR, INBOX_LOADING_SELECTOR)
            )
        )
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                """
                const grid = document.querySelector(arguments[0]);
                if (!grid) return false;
                const loading = document.querySelector(arguments[1]);
                if (loading && loading.offsetParent !== null) return false;
                return Boolean(
                    grid.querySelector("tbody tr") ||
                    /no records|no data|empty/i.test(grid.innerText)
                );
                """,
                INBOX_GRID_SELECTOR,
                INBOX_LOADING_SELECTOR,
            )
        )
    except TimeoutException:
        pass


def inbox_grid_signature(driver) -> str:
    try:
        return driver.execute_script(
            """
            const selectedPage = document.querySelector(
                ".k-pager-numbers .k-selected, .k-pager-numbers .k-state-selected, [aria-current='page']"
            )?.textContent?.trim() || "";
            const grid = document.querySelector(arguments[0]) || document.body;
            return selectedPage + "|" + grid.innerText.slice(0, 2000);
            """,
            INBOX_GRID_SELECTOR,
        )
    except WebDriverException:
        return ""


def find_message_on_current_page(driver, target_subject: str):
    try:
        return driver.execute_script(
            """
            const target = arguments[0];
            return Array.from(document.querySelectorAll("span[title]")).find((element) => {
                if (element.getAttribute("title") !== target) return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }) || null;
            """,
            target_subject,
        )
    except WebDriverException:
        return None


def click_message(driver, target_subject: str):
    subject_xpath = f"//span[@title={xpath_literal(target_subject)}]"
    try:
        message_span = WebDriverWait(driver, MESSAGE_CLICK_TIMEOUT).until(
            EC.element_to_be_clickable((By.XPATH, subject_xpath))
        )
        try:
            message_span.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", message_span)
    except TimeoutException:
        message_span = find_message_on_current_page(driver, target_subject)
        if message_span is None:
            raise
        driver.execute_script("arguments[0].click();", message_span)


def find_enabled_next_page_button(driver):
    for button in driver.find_elements(By.CSS_SELECTOR, NEXT_PAGE_SELECTOR):
        try:
            class_name = button.get_attribute("class") or ""
            if (
                button.is_displayed()
                and button.is_enabled()
                and button.get_attribute("aria-disabled") != "true"
                and button.get_attribute("disabled") is None
                and "k-disabled" not in class_name
            ):
                return button
        except StaleElementReferenceException:
            continue
    return None


def click_next_inbox_page(driver, page_number: int, timeout: int = INBOX_PAGE_CHANGE_TIMEOUT):
    old_signature = inbox_grid_signature(driver)

    def click_next(driver):
        button = find_enabled_next_page_button(driver)
        if button is None:
            return False
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
        try:
            button.click()
        except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
            button = find_enabled_next_page_button(driver)
            if button is None:
                return False
            driver.execute_script("arguments[0].click();", button)
        return True

    if not click_next(driver):
        raise RuntimeError(f"Could not click the inbox next-page button from page {page_number}.")

    try:
        WebDriverWait(driver, timeout).until(
            lambda d: inbox_grid_signature(d) != old_signature
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Timed out waiting for inbox page {page_number + 1} to load after clicking next."
        ) from exc

    wait_for_inbox_grid_to_settle(driver)


def find_message_and_open(driver, delivery_date: date):
    target_subject = build_message_subject(delivery_date)
    print(f"[5/7] Looking for message with subject: {target_subject}")

    for page_number in range(1, MAX_INBOX_PAGES_TO_SEARCH + 1):
        wait_for_inbox_grid_to_settle(driver, timeout=INBOX_GRID_READY_TIMEOUT)
        message_span = find_message_on_current_page(driver, target_subject)
        if message_span is not None:
            click_message(driver, target_subject)
            print(f"[5/7] Opened target message on inbox page {page_number}")
            return

        next_page_button = find_enabled_next_page_button(driver)
        if next_page_button is None:
            break

        print(f"[5/7] Message not on page {page_number}; moving to next inbox page")
        click_next_inbox_page(driver, page_number)

    raise RuntimeError(
        f"Target message not found after searching up to {MAX_INBOX_PAGES_TO_SEARCH} inbox pages: "
        f"{target_subject}"
    )

def wait_for_new_download_file(download_dir: Path, existing_names: set, timeout: int = 30) -> Path:
    print("[6/7] ⏳ Waiting for the downloaded Excel file to appear")
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = [p for p in download_dir.glob("*.xlsx") if p.name not in existing_names]
        if files:
            latest = max(files, key=lambda p: p.stat().st_mtime)
            if latest.stat().st_size > 0:
                print(f"[6/7] 📥 Download detected: {latest.name}")
                return latest
        time.sleep(1)
    raise RuntimeError("Timed out waiting for the downloaded Excel file.")


def find_latest_download_file(download_dir: Path) -> Path:
    files = list(download_dir.glob("*.xlsx"))
    if not files:
        raise RuntimeError("No Excel files found in the download folder.")
    return max(files, key=lambda p: p.stat().st_mtime)


def extract_data_from_file(file_path: Path):
    print(f"[7/7] 📄 Extracting data from downloaded file: {file_path.name}")
    workbook = openpyxl.load_workbook(file_path, data_only=True)
    sheet = workbook.active

    delivery_date = None
    for row in sheet.iter_rows(min_row=1, max_row=50, values_only=True):
        if not row:
            continue
        for idx, value in enumerate(row):
            if isinstance(value, str) and value.strip() == "Delivery Date:":
                candidate = row[idx + 1] if idx + 1 < len(row) else None
                if isinstance(candidate, datetime):
                    delivery_date = candidate.date()
                elif isinstance(candidate, str):
                    try:
                        delivery_date = datetime.fromisoformat(candidate).date()
                    except ValueError:
                        continue
                break
        if delivery_date is not None:
            break

    if delivery_date is None:
        raise RuntimeError("Could not find the delivery date in the downloaded file.")

    header_row_index = None
    hour_col = purchase_col = sales_col = price_col = None
    for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        if not row:
            continue
        if (
            "Hour" in row
            and "Area Purchase (MW)" in row
            and "Area Sales (MW)" in row
            and "Area Price (USD/MWh)" in row
        ):
            header_row_index = row_idx
            hour_col = row.index("Hour")
            purchase_col = row.index("Area Purchase (MW)")
            sales_col = row.index("Area Sales (MW)")
            price_col = row.index("Area Price (USD/MWh)")
            break

    if header_row_index is None:
        raise RuntimeError("Could not find the hourly data header row in the downloaded file.")

    extracted_rows = []
    for row in sheet.iter_rows(min_row=header_row_index + 1, values_only=True):
        if not row or row[hour_col] is None:
            continue
        hour_text = str(row[hour_col]).strip()
        hour_value = hour_text.split("-")[0] if "-" in hour_text else hour_text
        area_purchase = row[purchase_col] if purchase_col < len(row) else None
        area_sales = row[sales_col] if sales_col < len(row) else None
        area_price = row[price_col] if price_col < len(row) else None
        extracted_rows.append([delivery_date.isoformat(), hour_value, area_purchase, area_sales, area_price])

    if not extracted_rows:
        raise RuntimeError("No hourly rows were extracted from the downloaded file.")

    output_wb = openpyxl.Workbook()
    output_ws = output_wb.active
    output_ws.title = "Extracted Data"
    output_ws.append(["Date", "Hour", "Area Purchase (MW)", "Area Sales (MW)", "Area Price (USD/MWh)"])
    for row in extracted_rows:
        output_ws.append(row)

    output_path = file_path.with_name(f"extracted_{file_path.stem}.xlsx")
    output_wb.save(output_path)
    print(f"💾 Saved extracted data to: {output_path}")
    return output_path


def extract_results_from_file(file_path: Path) -> list[dict]:
    print(f"[7/7] Extracting SAPP rows from downloaded file: {file_path.name}")
    workbook = openpyxl.load_workbook(file_path, data_only=True)
    sheet = workbook.active

    delivery_date = None
    for row in sheet.iter_rows(min_row=1, max_row=50, values_only=True):
        if not row:
            continue
        for idx, value in enumerate(row):
            if isinstance(value, str) and value.strip() == "Delivery Date:":
                candidate = row[idx + 1] if idx + 1 < len(row) else None
                delivery_date = parse_delivery_date_value(candidate)
                break
        if delivery_date is not None:
            break

    if delivery_date is None:
        raise RuntimeError("Could not find the delivery date in the downloaded file.")

    header_row_index = None
    hour_col = purchase_col = sales_col = price_col = None
    for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        if not row:
            continue
        if (
            "Hour" in row
            and "Area Purchase (MW)" in row
            and "Area Sales (MW)" in row
            and "Area Price (USD/MWh)" in row
        ):
            header_row_index = row_idx
            hour_col = row.index("Hour")
            purchase_col = row.index("Area Purchase (MW)")
            sales_col = row.index("Area Sales (MW)")
            price_col = row.index("Area Price (USD/MWh)")
            break

    if header_row_index is None:
        raise RuntimeError("Could not find the hourly data header row in the downloaded file.")

    records = []
    for row in sheet.iter_rows(min_row=header_row_index + 1, values_only=True):
        if not row or hour_col >= len(row) or row[hour_col] is None:
            continue

        hour = parse_hour(row[hour_col])
        if hour is None:
            continue

        area_purchase = row[purchase_col] if purchase_col < len(row) else None
        area_sales = row[sales_col] if sales_col < len(row) else None
        area_price = row[price_col] if price_col < len(row) else None
        records.append(
            {
                "timestamp": delivery_timestamp(delivery_date, hour),
                "delivery_date": delivery_date.isoformat(),
                "hour": hour,
                "hour_label": str(row[hour_col]).strip(),
                "area_purchase_mw": to_float(area_purchase),
                "area_sales_mw": to_float(area_sales),
                "area_price_usd_per_mwh": to_float(area_price),
                "metadata": {
                    "data_source": DATA_SOURCE,
                    "source_file": file_path.name,
                },
                "source_file": file_path.name,
            }
        )

    if not records:
        raise RuntimeError("No hourly rows were extracted from the downloaded file.")

    extracted_hours = {record["hour"] for record in records}
    expected_hours = set(range(1, 25))
    if extracted_hours != expected_hours:
        missing_hours = sorted(expected_hours - extracted_hours)
        extra_hours = sorted(extracted_hours - expected_hours)
        raise RuntimeError(
            "Expected 24 hourly rows for the delivery day, "
            f"but extracted {len(records)}. "
            f"Missing hours: {missing_hours or 'none'}. "
            f"Unexpected hours: {extra_hours or 'none'}."
        )

    return records


def store_results_in_database(records: list[dict]) -> dict:
    if not records:
        raise RuntimeError("No SAPP records were provided for database import.")

    from app.db.database import get_db

    db = get_db()
    collection = db["sapp_constrained_area_results"]
    now = datetime.now(timezone.utc)

    operations = []
    for record in records:
        operations.append(
            UpdateOne(
                {
                    "delivery_date": record["delivery_date"],
                    "hour": record["hour"],
                },
                {
                    "$set": {
                        **record,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                },
                upsert=True,
            )
        )

    result = collection.bulk_write(operations, ordered=False)
    return {
        "delivery_date": records[0]["delivery_date"],
        "imported": result.upserted_count,
        "updated": result.matched_count,
        "source_file": records[0].get("source_file", ""),
    }


def download_attachment(driver, download_dir: Path):
    # Click the attachment button for the Excel file using the data-cy/title attribute.
    attachment_xpath = "//button[contains(@data-cy, '.xlsx') or contains(@title, '.xlsx')]"

    attachment_button = WebDriverWait(driver, 20).until(
        EC.element_to_be_clickable((By.XPATH, attachment_xpath))
    )

    existing_files = {p.name for p in download_dir.glob("*.xlsx")}
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
        attachment_button,
    )
    time.sleep(0.5)
    try:
        attachment_button.click()
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        attachment_button = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, attachment_xpath))
        )
        driver.execute_script("arguments[0].click();", attachment_button)

    downloaded_file = wait_for_new_download_file(download_dir, existing_files, timeout=60)
    print(f"📥 Downloaded file: {downloaded_file.name}")
    return downloaded_file


def run_scraper(delivery_date: Optional[date] = None) -> dict:
    delivery_date = delivery_date or datetime.now().date()
    print(f"Starting SAPP scraper for {delivery_date.isoformat()}")
    username, password = load_config()
    with tempfile.TemporaryDirectory(prefix="sapp-download-") as temp_download_dir:
        download_dir = Path(temp_download_dir)
        driver = create_driver(download_dir)
        try:
            login(driver, username, password)
            navigate_to_inbox(driver)
            find_message_and_open(driver, delivery_date)
            downloaded_file = download_attachment(driver, download_dir)
            records = extract_results_from_file(downloaded_file)
            result = store_results_in_database(records)
            print(f"SAPP scraper completed successfully: {result}")
            return result
        finally:
            driver.quit()


def main():
    return run_scraper()


if __name__ == "__main__":
    main()

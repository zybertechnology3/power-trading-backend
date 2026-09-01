import argparse
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.firefox import GeckoDriverManager

BASE_URL = "https://trading.sappmtp.com"
LOGIN_URL = f"{BASE_URL}/account/login?returnUrl=%2F"
ATC_URL = f"{BASE_URL}/tsam/view-atc-X-bm"

RESULTS_COLLECTION = "sapp_bm_atc_results"
DATA_SOURCE = "TSAM_BM_ATC_RESULTS"

MONGO_SERVER_SELECTION_TIMEOUT_MS = 30000
MONGO_CONNECT_TIMEOUT_MS = 30000
MONGO_SOCKET_TIMEOUT_MS = 30000
MONGO_PING_RETRIES = 3
MONGO_PING_RETRY_DELAY_SECONDS = 5
BULK_WRITE_CHUNK_SIZE = 500
SUMMARY_ROW_LABELS = {"min", "max", "avg", "tot", "total"}


def debug_log(enabled: bool, message: str) -> None:
    if enabled:
        print(f"🔎 {message}")


def wait_for_page_settle(driver, timeout: int = 20, extra_delay: float = 0.75) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    WebDriverWait(driver, timeout).until(
        lambda d: not d.execute_script(
            """
            const loading = document.querySelector(
                ".k-loading-mask, .k-i-loading, .k-loading-image, "
                + ".spinner-border, .ngx-spinner-overlay"
            );
            return Boolean(loading && loading.offsetParent !== null);
            """
        )
    )
    time.sleep(extra_delay)


def _default_headless_mode() -> bool:
    configured = os.getenv("SAPP_HEADLESS")
    if configured is None:
        configured = os.getenv("HEADLESS")
    if configured is None or not configured.strip():
        return True

    normalized = configured.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    print(f"[config] Invalid SAPP_HEADLESS={configured!r}; defaulting to headless=True")
    return True


def load_config() -> tuple[str, str, str, str]:
    env_path = Path(__file__).resolve().parent / ".env"
    print(f"[config] Loading configuration from {env_path}")
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print("[config] .env file found and loaded")
    else:
        print("[config] .env file not found next to script; using process environment")

    username = os.getenv("SAPP_USERNAME")
    password = os.getenv("SAPP_PASSWORD")
    mongodb_url = os.getenv("MONGODB_URL")
    database_name = os.getenv("DATABASE_NAME")
    if not username or not password:
        raise ValueError("Set SAPP_USERNAME and SAPP_PASSWORD in .env.")
    if not mongodb_url or not database_name:
        raise ValueError("Set MONGODB_URL and DATABASE_NAME in .env.")

    print(
        "[config] Credentials and database settings are present: "
        f"SAPP_USERNAME={username}, DATABASE_NAME={database_name}"
    )
    return username, password, mongodb_url, database_name


def create_driver(headless: Optional[bool] = None):
    configured_headless = os.getenv("SAPP_HEADLESS")
    if configured_headless is None:
        configured_headless = os.getenv("HEADLESS")
    headless_mode = (
        _default_headless_mode()
        if configured_headless is not None
        else (True if headless is None else headless)
    )
    print(f"[driver] Creating Firefox driver with headless={headless_mode}")
    options = Options()
    options.add_argument("--width=1366")
    options.add_argument("--height=900")
    if headless_mode:
        options.add_argument("-headless")
        options.headless = True
        os.environ["MOZ_HEADLESS"] = "1"
    else:
        os.environ.pop("MOZ_HEADLESS", None)

    os.environ["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"
    options.set_preference("browser.shell.checkDefaultBrowser", False)

    driver_path = GeckoDriverManager().install()
    print(f"[driver] Using geckodriver at {driver_path}")
    service = Service(executable_path=driver_path)
    driver = webdriver.Firefox(service=service, options=options)
    driver.set_window_size(1366, 900)
    driver.implicitly_wait(0)
    print("[driver] Firefox started")
    return driver


def login(driver, username: str, password: str, timeout: int = 20) -> None:
    print("[1/4] Opening login page")
    driver.get(LOGIN_URL)
    wait_for_page_settle(driver, timeout=timeout)

    username_selector = "input[id='login-input-user-name-or-email-address']"
    password_selector = "input[id='password']"
    submit_selector = "button[type='submit']"

    WebDriverWait(driver, timeout).until(
        lambda d: d.find_element("css selector", username_selector)
    )
    driver.find_element("css selector", username_selector).send_keys(username)
    driver.find_element("css selector", password_selector).send_keys(password)
    driver.find_element("css selector", submit_selector).click()

    WebDriverWait(driver, timeout).until(lambda d: BASE_URL in d.current_url)
    wait_for_page_settle(driver, timeout=timeout)
    print("[1/4] Login completed")


def open_atc_page(
    driver,
    username: str,
    password: str,
    timeout: int = 20,
    debug: bool = False,
) -> None:
    login_page_detected = driver.current_url.startswith(LOGIN_URL) or bool(
        driver.execute_script(
            """
            return Boolean(
                document.querySelector("input[id='login-input-user-name-or-email-address']")
                || document.querySelector("input[id='password']")
            );
            """
        )
    )

    if login_page_detected:
        print("[2/4] Session expired; logging in again")
        login(driver, username, password, timeout=timeout)

    print(f"[2/4] Opening {ATC_URL}")
    debug_log(debug, f"Opening ATC page: {ATC_URL}")
    driver.get(ATC_URL)
    wait_for_page_settle(driver, timeout=timeout, extra_delay=1.25)
    debug_log(debug, f"ATC page loaded: current_url={driver.current_url}")

    if driver.current_url.startswith(LOGIN_URL) or bool(
        driver.execute_script(
            """
            return Boolean(
                document.querySelector("input[id='login-input-user-name-or-email-address']")
                || document.querySelector("input[id='password']")
            );
            """
        )
    ):
        print("[2/4] Redirected back to login; authenticating again")
        login(driver, username, password, timeout=timeout)
        driver.get(ATC_URL)
        wait_for_page_settle(driver, timeout=timeout, extra_delay=1.25)
        debug_log(debug, f"ATC page reloaded: current_url={driver.current_url}")


def format_delivery_day(value: date) -> str:
    return value.strftime("%Y/%m/%d")


def find_field_input(driver, label: str):
    return driver.execute_script(
        r"""
        const labelText = arguments[0].toLowerCase();
        const normalize = (value) => (value || "")
            .replace(/\*/g, "")
            .replace(/\s+/g, " ")
            .trim()
            .toLowerCase();
        const isVisible = (element) => Boolean(
            element
            && element.getClientRects
            && element.getClientRects().length > 0
            && element.offsetParent !== null
        );
        const labels = Array.from(document.querySelectorAll("label, .form-label, div, span"))
            .filter((element) => normalize(element.textContent) === labelText);

        for (const label of labels) {
            let node = label;
            for (let depth = 0; node && depth < 6; depth += 1) {
                const input = node.querySelector(
                    "input.k-input-inner:not([type='hidden']), input:not([type='hidden'])"
                );
                if (isVisible(input)) return input;
                node = node.parentElement;
            }
        }
        return null;
        """,
        label,
    )


def read_input_value(driver, input_element) -> str:
    return driver.execute_script(
        """
        const input = arguments[0];
        return (input.value || "").trim();
        """,
        input_element,
    )


def select_all_input_text(driver, input_element) -> None:
    driver.execute_script(
        """
        const input = arguments[0];
        input.focus();
        if (typeof input.select === "function") {
            input.select();
        }
        if (typeof input.setSelectionRange === "function") {
            input.setSelectionRange(0, input.value.length);
        }
        """,
        input_element,
    )


def set_input_by_label(driver, label: str, value: str, timeout: int = 20) -> None:
    print(f"[2/4] Setting {label}: {value}")
    wait_for_page_settle(driver, timeout=timeout)
    input_element = WebDriverWait(driver, timeout).until(
        lambda d: find_field_input(d, label)
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", input_element)

    for attempt in range(1, 4):
        print(f"  ↳ attempt {attempt}: typing")
        input_element.click()
        time.sleep(0.15)
        select_all_input_text(driver, input_element)
        for character in value:
            input_element.send_keys(character)
            time.sleep(0.08)
        input_element.send_keys(Keys.TAB)
        time.sleep(0.25)

        actual_value = read_input_value(driver, input_element)
        if actual_value == value:
            print(f"  ✓ {label} set")
            return

        wait_for_page_settle(driver, timeout=timeout, extra_delay=0.2)
        input_element = WebDriverWait(driver, timeout).until(
            lambda d: find_field_input(d, label)
        )

    raise RuntimeError(f"Failed to set {label} to '{value}'. Field kept a different value.")


def is_schedule_panel_open(driver) -> bool:
    return bool(
        driver.execute_script(
            """
            const isVisible = (element) => Boolean(
                element
                && element.getClientRects
                && element.getClientRects().length > 0
                && element.offsetParent !== null
            );
            return Boolean(
                isVisible(document.querySelector("input[id='datepicker-2']"))
                || isVisible(document.querySelector("input.k-input-inner[aria-haspopup='grid']"))
            );
            """
        )
    )


def click_select_schedule(driver, timeout: int = 20) -> None:
    def find_button(driver):
        return driver.execute_script(
            r"""
            const buttons = Array.from(document.querySelectorAll(
                "button[title='Select a Schedule'], button[data-cy='Select a Schedule'], button"
            ));
            return buttons.find((button) => {
                const rect = button.getBoundingClientRect();
                const text = (button.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
                const title = (button.getAttribute("title") || "").trim().toLowerCase();
                const dataCy = (button.getAttribute("data-cy") || "").trim().toLowerCase();
                return rect.width > 0
                    && rect.height > 0
                    && !button.disabled
                    && (title === "select a schedule"
                        || dataCy === "select a schedule"
                        || text === "select a schedule");
            }) || null;
            """
        )

    button = WebDriverWait(driver, timeout).until(find_button)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    time.sleep(0.15)
    try:
        button.click()
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        button = WebDriverWait(driver, 5).until(find_button)
        driver.execute_script("arguments[0].click();", button)

    WebDriverWait(driver, timeout).until(lambda d: is_schedule_panel_open(d))
    wait_for_page_settle(driver, timeout=timeout, extra_delay=0.55)


def click_search(driver, timeout: int = 20, debug: bool = False) -> dict:
    print("[3/4] Searching")
    wait_for_page_settle(driver, timeout=timeout)
    old_url = driver.current_url
    old_signature = driver.execute_script(
        "return document.body ? document.body.innerText.slice(0, 2000) : '';"
    )

    def find_button(driver):
        return driver.execute_script(
            r"""
            const buttons = Array.from(document.querySelectorAll(
                "button[title='Search'], button[data-cy='Search'], button"
            ));
            return buttons.find((button) => {
                const rect = button.getBoundingClientRect();
                const text = (button.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
                const title = (button.getAttribute("title") || "").trim().toLowerCase();
                const dataCy = (button.getAttribute("data-cy") || "").trim().toLowerCase();
                return rect.width > 0
                    && rect.height > 0
                    && !button.disabled
                    && (title === "search" || dataCy === "search" || text === "search");
            }) || null;
            """
        )

    button = WebDriverWait(driver, timeout).until(find_button)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    time.sleep(0.15)
    if debug:
        button_details = driver.execute_script(
            """
            const button = arguments[0];
            return {
                text: (button.textContent || "").replace(/\\s+/g, " ").trim(),
                title: button.getAttribute("title"),
                dataCy: button.getAttribute("data-cy"),
                className: button.className,
            };
            """,
            button,
        )
        debug_log(debug, f"Search button details: {button_details}")
    try:
        button.click()
        click_method = "selenium_click"
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        button = WebDriverWait(driver, 5).until(find_button)
        driver.execute_script("arguments[0].click();", button)
        click_method = "javascript_click"

    WebDriverWait(driver, timeout).until(
        lambda d: (
            d.current_url != old_url
            or d.execute_script(
                "return document.body ? document.body.innerText.slice(0, 2000) : '';"
            )
            != old_signature
            or d.execute_script(
                """
                const loading = document.querySelector(
                    ".k-loading-mask, .k-i-loading, .k-loading-image, .spinner-border, .ngx-spinner-overlay"
                );
                return Boolean(loading && loading.offsetParent !== null);
                """
            )
        )
    )
    wait_for_page_settle(driver, timeout=timeout, extra_delay=1.25)
    print("  ✓ search complete")
    debug_log(
        debug,
        f"Search completed via {click_method}; redirected={driver.current_url != old_url}; current_url={driver.current_url}",
    )
    return {
        "search_click_method": click_method,
        "search_redirected": driver.current_url != old_url,
    }


def hour_from_product_label(label: str) -> Optional[int]:
    match = re.match(r"^\s*(\d{2})-(\d{2})\s*$", label or "")
    if not match:
        return None
    return int(match.group(1)) + 1


def parse_number(value: str) -> Optional[float]:
    text = (value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def extract_hourly_table(driver, timeout: int = 20, debug: bool = False) -> dict:
    print("[4/4] Extracting hourly table")
    wait_for_page_settle(driver, timeout=timeout, extra_delay=1.5)

    def table_exists(driver):
        return driver.execute_script(
            """
            const table = document.querySelector(".ht_master table.htCore");
            if (!table) return null;
            const headers = Array.from(table.querySelectorAll("thead th"))
                .map((cell) => (cell.textContent || "").trim())
                .filter(Boolean);
            const rows = Array.from(table.querySelectorAll("tbody tr"));
            return headers.length >= 2 && rows.length > 0 ? table : null;
            """
        )

    WebDriverWait(driver, timeout).until(table_exists)
    result = driver.execute_script(
        r"""
        const table = document.querySelector(".ht_master table.htCore");
        const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
        const parseNumber = (value) => {
            const text = normalize(value).replace(/,/g, "");
            if (!text) return null;
            const parsed = Number(text);
            return Number.isFinite(parsed) ? parsed : null;
        };
        const hourFromProduct = (product) => {
            const match = normalize(product).match(/^(\d{2})-(\d{2})$/);
            if (!match) return null;
            return Number(match[1]) + 1;
        };
        const summaryNames = new Set(["min", "max", "avg", "tot", "total"]);

        const headers = Array.from(table.querySelectorAll("thead th"))
            .map((cell) => normalize(cell.textContent));
        const columnHeaders = headers.slice(1);
        const rows = Array.from(table.querySelectorAll("tbody tr"))
            .map((row) => Array.from(row.querySelectorAll("td, th"))
                .map((cell) => normalize(cell.textContent)))
            .filter((cells) => cells.length > 1)
            .map((cells) => ({
                product: cells[0],
                values: cells.slice(1)
            }))
            .filter((row) => row.product && !summaryNames.has(row.product.toLowerCase()));

        const records = [];
        rows.forEach((row) => {
            const columnValues = {};
            columnHeaders.forEach((header, index) => {
                columnValues[header] = parseNumber(row.values[index]);
            });
            records.push({
                product: row.product,
                hour_label: row.product,
                hour: hourFromProduct(row.product),
                column_values: columnValues,
            });
        });

        return {
            columns: columnHeaders,
            rows,
            records,
            row_count: records.length,
        };
        """
    )

    if debug:
        debug_log(debug, f"Table columns: {result['columns']}")
        debug_log(debug, f"Row count: {result['row_count']}")

    if result["row_count"] != 24:
        raise RuntimeError(
            f"Expected 24 hourly rows, got {result['row_count']}. "
            f"Columns={result['columns']}"
        )

    print(
        f"[4/4] Table has {len(result['columns'])} value columns and {result['row_count']} hourly rows"
    )
    return result


def build_delivery_dates(
    delivery_date: Optional[date],
    start_date: Optional[date],
    end_date: Optional[date],
) -> list[date]:
    if delivery_date:
        if start_date or end_date:
            raise ValueError("Use either delivery_date or start_date/end_date, not both")
        return [delivery_date]

    if start_date is None and end_date is None:
        today = datetime.now().date()
        return [today]

    normalized_start_date = start_date or end_date
    normalized_end_date = end_date or start_date
    if normalized_start_date is None or normalized_end_date is None:
        raise ValueError("Provide delivery_date or both start_date and end_date")
    if normalized_end_date < normalized_start_date:
        raise ValueError("end_date must be greater than or equal to start_date")

    delivery_dates = []
    current_date = normalized_start_date
    while current_date <= normalized_end_date:
        delivery_dates.append(current_date)
        current_date += timedelta(days=1)
    return delivery_dates


def delivery_timestamp(delivery_date_value: str, hour: int) -> datetime:
    parsed_date = date.fromisoformat(delivery_date_value)
    return datetime.combine(parsed_date, datetime.min.time()) + timedelta(hours=hour - 1)


def open_mongo_connection(mongodb_url: str, database_name: str):
    print(f"[db] Preflighting MongoDB connection for database {database_name}")
    client = MongoClient(
        mongodb_url,
        serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
        connectTimeoutMS=MONGO_CONNECT_TIMEOUT_MS,
        socketTimeoutMS=MONGO_SOCKET_TIMEOUT_MS,
        retryWrites=True,
        maxPoolSize=1,
    )

    last_error: Exception | None = None
    for attempt in range(1, MONGO_PING_RETRIES + 1):
        try:
            print(
                f"[db] Pinging MongoDB for database {database_name} "
                f"(attempt {attempt}/{MONGO_PING_RETRIES})"
            )
            client.admin.command("ping")
            print(f"[db] MongoDB preflight passed for database {database_name}")
            return client, client[database_name]
        except Exception as exc:
            last_error = exc
            print(f"[db] MongoDB ping attempt {attempt} failed: {exc}")
            if attempt < MONGO_PING_RETRIES:
                time.sleep(MONGO_PING_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Failed to connect to MongoDB database {database_name} after "
        f"{MONGO_PING_RETRIES} attempts"
    ) from last_error


def close_mongo_connection(client: MongoClient) -> None:
    client.close()
    print("[db] MongoDB connection closed")


def upsert_hourly_records(
    collection,
    records: list[dict],
    delivery_day: str,
    area: str,
    now: datetime,
) -> dict:
    if not records:
        return {"imported": 0, "updated": 0}

    operations = []
    for record in records:
        hour = record["hour"]
        row_payload = {
            "timestamp": delivery_timestamp(delivery_day, hour),
            "delivery_date": delivery_day,
            "hour": hour,
            "hour_label": record["hour_label"],
            "product": record["product"],
            "area": area,
            "market": "BM",
            "column_values": record["column_values"],
            **record["column_values"],
            "metadata": {
                "data_source": DATA_SOURCE,
                "source_page": ATC_URL,
                "area": area,
                "market": "BM",
                "column_headers": list(record["column_values"].keys()),
            },
            "source_file": None,
            "updated_at": now,
        }

        operations.append(
            UpdateOne(
                {
                    "delivery_date": delivery_day,
                    "hour": hour,
                    "area": area,
                },
                {
                    "$set": row_payload,
                    "$setOnInsert": {
                        "created_at": now,
                    },
                },
                upsert=True,
            )
        )

    imported = 0
    updated = 0
    for index in range(0, len(operations), BULK_WRITE_CHUNK_SIZE):
        batch = operations[index : index + BULK_WRITE_CHUNK_SIZE]
        result = collection.bulk_write(batch, ordered=False)
        imported += result.upserted_count
        updated += result.matched_count

    return {"imported": imported, "updated": updated}


def store_results(
    db,
    records: list[dict],
    delivery_day: str,
    area: str,
) -> dict:
    now = datetime.now(timezone.utc)
    collection = db[RESULTS_COLLECTION]
    print(
        f"[db] Preparing {len(records)} hourly rows for {collection.full_name}"
    )
    result = upsert_hourly_records(collection, records, delivery_day, area, now)
    print(f"[db] Storage summary: {result}")
    return result


def scrape_and_store_delivery_day(
    driver,
    db,
    delivery_day: date,
    area: str,
    timeout: int,
    debug: bool,
    reopen_schedule: bool,
) -> dict:
    if reopen_schedule:
        click_select_schedule(driver, timeout=timeout)

    set_input_by_label(driver, "Delivery Day", format_delivery_day(delivery_day), timeout=timeout)
    if area and area != "All Areas":
        set_input_by_label(driver, "Area", area, timeout=timeout)
    click_search(driver, timeout=timeout, debug=debug)
    table = extract_hourly_table(driver, timeout=timeout, debug=debug)
    storage_result = store_results(db, table["records"], delivery_day.isoformat(), area)
    return {
        "delivery_date": delivery_day.isoformat(),
        "columns": table["columns"],
        "row_count": table["row_count"],
        "storage": storage_result,
    }


def run(
    delivery_date: Optional[date],
    start_date: Optional[date],
    end_date: Optional[date],
    area: str,
    timeout: int,
    headless: Optional[bool],
    observe_seconds: int,
    debug: bool = False,
) -> dict:
    run_started_at = time.perf_counter()
    username, password, mongodb_url, database_name = load_config()
    delivery_dates = build_delivery_dates(delivery_date, start_date, end_date)
    normalized_start_date = delivery_dates[0]
    normalized_end_date = delivery_dates[-1]
    print(
        f"[run] Starting BM ATC scraper for start_date={normalized_start_date.isoformat()}, "
        f"end_date={normalized_end_date.isoformat()}, area={area}, timeout={timeout}, "
        f"headless={headless}, observe_seconds={observe_seconds}, "
        f"debug={debug}"
    )

    driver = None
    mongo_client = None
    db = None
    run_succeeded = False
    try:
        mongo_client, db = open_mongo_connection(mongodb_url, database_name)
        driver = create_driver(headless=headless)
        login(driver, username, password, timeout=timeout)
        open_atc_page(driver, username, password, timeout=timeout, debug=debug)

        day_results = []
        for index, delivery_day in enumerate(delivery_dates, start=1):
            print(
                f"[run] Executing day {index}/{len(delivery_dates)}: {delivery_day.isoformat()}"
            )
            try:
                day_result = scrape_and_store_delivery_day(
                    driver=driver,
                    db=db,
                    delivery_day=delivery_day,
                    area=area,
                    timeout=timeout,
                    debug=debug,
                    reopen_schedule=index > 1,
                )
                day_results.append(day_result)
                print(
                    f"[run] Saved {day_result['row_count']} rows for {day_result['delivery_date']}"
                )
            except Exception as exc:
                print(
                    f"[run] Day {delivery_day.isoformat()} failed during scrape/store: {exc}"
                )
                raise

        elapsed_seconds = time.perf_counter() - run_started_at
        result = {
            "start_date": normalized_start_date.isoformat(),
            "end_date": normalized_end_date.isoformat(),
            "area": area,
            "elapsed_seconds": round(elapsed_seconds, 2),
            "day_count": len(day_results),
            "row_count_total": sum(day["row_count"] for day in day_results),
            "days": day_results,
        }
        print(
            f"[done] start_date={normalized_start_date.isoformat()} end_date={normalized_end_date.isoformat()} "
            f"area={area} days={len(day_results)} rows={result['row_count_total']} "
            f"elapsed={elapsed_seconds:.2f}s"
        )
        run_succeeded = True
        return result
    finally:
        if not run_succeeded and observe_seconds > 0:
            print(f"🛑 keeping browser open for {observe_seconds}s")
            time.sleep(observe_seconds)
        if driver is not None:
            driver.quit()
            print("[driver] Firefox closed")
        if mongo_client is not None:
            close_mongo_connection(mongo_client)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone BM ATC scraper for delivery day and area selection."
    )
    parser.add_argument(
        "--delivery-date",
        help="Single delivery date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--start-date",
        help="Start delivery date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end-date",
        help="End delivery date in YYYY-MM-DD format, inclusive.",
    )
    parser.add_argument(
        "--area",
        default="All Areas",
        help="Area to search for, e.g. All Areas.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="Selenium wait timeout in seconds.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=None,
        help="Run Firefox headless. Default is visible.",
    )
    parser.add_argument(
        "--observe-seconds",
        type=int,
        default=30,
        help="Seconds to keep the browser open before closing on failure.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print live debug traces for page discovery and table extraction.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    has_range = bool(args.start_date or args.end_date)
    delivery_date = (
        date.fromisoformat(args.delivery_date)
        if args.delivery_date
        else (None if has_range else datetime.now().date())
    )
    start_date = date.fromisoformat(args.start_date) if args.start_date else None
    end_date = date.fromisoformat(args.end_date) if args.end_date else None
    result = run(
        delivery_date=delivery_date,
        start_date=start_date,
        end_date=end_date,
        area=args.area,
        timeout=args.timeout,
        headless=args.headless,
        observe_seconds=args.observe_seconds,
        debug=args.debug,
    )
    print(
        f"[done] day_count={result['day_count']} rows={result['row_count_total']} "
        f"elapsed={result['elapsed_seconds']}s"
    )


if __name__ == "__main__":
    main()

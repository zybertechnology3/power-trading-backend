import argparse
import os
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import ServerSelectionTimeoutError
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
AREA_RESULTS_URL = f"{BASE_URL}/amt/prices-and-turnover-X-dam"

CONSTRAINED_COLLECTION = "sapp_constrained_area_results"
UNCONSTRAINED_COLLECTION = "sapp_unconstrained_area_results"

CONSTRAINED_DATA_SOURCE = "SAPP_AMT_DAM_CONSTRAINED_PRICE_RESULTS"
UNCONSTRAINED_DATA_SOURCE = "SAPP_AMT_DAM_UNCONSTRAINED_PRICE_RESULTS"


def wait_for_page_settle(driver, timeout: int = 20, extra_delay: float = 1.5) -> None:
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


def create_driver(headless: bool = False):
    print(f"[driver] Creating Firefox driver with headless={headless}")
    options = Options()
    options.add_argument("--width=1366")
    options.add_argument("--height=900")
    if headless:
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
    print("[1/7] Opening login page")
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
    print("[1/7] Login completed")


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
        const labels = Array.from(document.querySelectorAll("label, .form-label, div, span"))
            .filter((element) => normalize(element.textContent) === labelText);

        for (const label of labels) {
            let node = label;
            for (let depth = 0; node && depth < 6; depth += 1) {
                const input = node.querySelector(
                    "input.k-input-inner:not([type='hidden']), input:not([type='hidden'])"
                );
                if (input) return input;
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


def set_input_by_label(driver, label: str, value: str, timeout: int = 20) -> None:
    print(f"[2/7] Setting {label}: {value}")
    wait_for_page_settle(driver, timeout=timeout)
    input_element = WebDriverWait(driver, timeout).until(
        lambda d: find_field_input(d, label)
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", input_element)
    for attempt in range(1, 4):
        print(f"[2/7] {label} attempt {attempt}: writing '{value}'")
        input_element.click()
        time.sleep(0.5)
        input_element.send_keys(Keys.CONTROL, "a")
        selected_value = read_input_value(driver, input_element)
        print(f"[2/7] {label} attempt {attempt}: selected existing value '{selected_value}'")

        for character in value:
            input_element.send_keys(character)
            time.sleep(0.12)

        input_element.send_keys(Keys.TAB)
        time.sleep(1.0)

        actual_value = read_input_value(driver, input_element)
        print(f"[2/7] {label} attempt {attempt}: field now shows '{actual_value}'")
        if actual_value == value:
            return

        wait_for_page_settle(driver, timeout=timeout, extra_delay=0.5)
        input_element = WebDriverWait(driver, timeout).until(
            lambda d: find_field_input(d, label)
        )

    raise RuntimeError(
        f"Failed to set {label} to '{value}'. Field kept a different value."
    )


def select_category(driver, value: str, timeout: int = 20) -> None:
    print(f"[2/7] Selecting Category: {value}")
    wait_for_page_settle(driver, timeout=timeout)
    input_element = WebDriverWait(driver, timeout).until(
        lambda d: find_field_input(d, "Category")
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", input_element)
    input_element.click()
    time.sleep(0.5)
    input_element.send_keys(Keys.CONTROL, "a")
    input_element.send_keys(value)

    def visible_option(driver):
        return driver.execute_script(
            r"""
            const target = arguments[0].toLowerCase();
            const options = Array.from(document.querySelectorAll(
                ".k-list-item, .k-item, [role='option']"
            ));
            return options.find((option) => {
                const rect = option.getBoundingClientRect();
                const text = (option.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
                return rect.width > 0 && rect.height > 0 && text === target;
            }) || null;
            """,
            value,
        )

    try:
        option = WebDriverWait(driver, 5).until(visible_option)
        driver.execute_script("arguments[0].click();", option)
    except TimeoutException:
        input_element.send_keys(Keys.ENTER)
        input_element.send_keys(Keys.TAB)
    time.sleep(1.0)


def find_constrained_toggle(driver):
    return driver.execute_script(
        r"""
        const normalize = (value) => (value || "")
            .replace(/\*/g, "")
            .replace(/\s+/g, " ")
            .trim()
            .toLowerCase();
        const labels = Array.from(document.querySelectorAll("label, .form-label, div, span"))
            .filter((element) => normalize(element.textContent) === "constrained per area result");
        for (const label of labels) {
            let node = label;
            for (let depth = 0; node && depth < 6; depth += 1) {
                const toggle = node.querySelector(
                    ".k-switch, [role='switch'], input[type='checkbox'], .k-switch-track"
                );
                if (toggle) return toggle.closest(".k-switch") || toggle;
                node = node.parentElement;
            }
        }
        return null;
        """
    )


def get_toggle_state(driver, toggle) -> Optional[bool]:
    return driver.execute_script(
        """
        const toggle = arguments[0];
        const input = toggle.matches("input[type='checkbox']")
            ? toggle
            : toggle.querySelector("input[type='checkbox']");
        if (input) return Boolean(input.checked);

        const ariaChecked = toggle.getAttribute("aria-checked")
            || (toggle.querySelector("[aria-checked]") || {}).getAttribute?.("aria-checked");
        if (ariaChecked === "true") return true;
        if (ariaChecked === "false") return false;

        const className = toggle.getAttribute("class") || "";
        if (/k-switch-on|k-selected|k-checked/.test(className)) return true;
        if (/k-switch-off/.test(className)) return false;
        return null;
        """,
        toggle,
    )


def set_constrained_toggle(driver, enabled: bool, timeout: int = 20) -> None:
    print(f"[3/7] Setting constrained toggle to {enabled}")
    wait_for_page_settle(driver, timeout=timeout)
    toggle = WebDriverWait(driver, timeout).until(find_constrained_toggle)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", toggle)
    time.sleep(0.5)
    before_state = get_toggle_state(driver, toggle)
    should_click = before_state is not enabled
    if before_state is None and enabled is False:
        should_click = False
    if should_click:
        driver.execute_script("arguments[0].click();", toggle)
        time.sleep(2)
        wait_for_page_settle(driver, timeout=timeout, extra_delay=0.75)
    after_state = get_toggle_state(driver, toggle)
    print(f"[3/7] Toggle state before={before_state}, after={after_state}")


def click_search(driver, timeout: int = 20) -> dict:
    print("[4/7] Clicking Search")
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
    button_info = driver.execute_script(
        r"""
        const button = arguments[0];
        return {
            text: (button.textContent || "").replace(/\s+/g, " ").trim(),
            title: button.getAttribute("title"),
            dataCy: button.getAttribute("data-cy"),
            className: button.getAttribute("class")
        };
        """,
        button,
    )
    print(f"[4/7] Search button details: {button_info}")
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    time.sleep(0.5)
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
    wait_for_page_settle(driver, timeout=timeout, extra_delay=2.0)
    new_url = driver.current_url
    content_changed = (
        driver.execute_script("return document.body ? document.body.innerText.slice(0, 2000) : '';")
        != old_signature
    )
    print(
        f"[4/7] Search dispatched via {click_method}; redirected={new_url != old_url}; "
        f"content_changed={content_changed}"
    )
    return {
        "search_click_method": click_method,
        "search_redirected": new_url != old_url,
        "search_content_changed": content_changed,
    }


def click_select_schedule(driver, timeout: int = 20) -> None:
    print("[5/7] Reopening schedule panel")
    wait_for_page_settle(driver, timeout=timeout)

    def find_button(driver):
        return driver.execute_script(
            r"""
            const buttons = Array.from(document.querySelectorAll(
                "button[data-cy='Select a Schedule'], button[title='Select a Schedule'], button"
            ));
            return buttons.find((button) => {
                const rect = button.getBoundingClientRect();
                const text = (button.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
                const title = (button.getAttribute("title") || "").trim().toLowerCase();
                const dataCy = (button.getAttribute("data-cy") || "").trim().toLowerCase();
                return rect.width > 0
                    && rect.height > 0
                    && !button.disabled
                    && (
                        title === "select a schedule"
                        || dataCy === "select a schedule"
                        || text === "select a schedule"
                    );
            }) || null;
            """
        )

    button = WebDriverWait(driver, timeout).until(find_button)
    button_info = driver.execute_script(
        r"""
        const button = arguments[0];
        return {
            text: (button.textContent || "").replace(/\s+/g, " ").trim(),
            title: button.getAttribute("title"),
            dataCy: button.getAttribute("data-cy"),
            className: button.getAttribute("class")
        };
        """,
        button,
    )
    print(f"[5/7] Select a Schedule button details: {button_info}")
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    time.sleep(0.5)
    driver.execute_script("arguments[0].click();", button)
    WebDriverWait(driver, timeout).until(lambda d: find_field_input(d, "Delivery Day"))
    wait_for_page_settle(driver, timeout=timeout, extra_delay=1.5)


def extract_hourly_table(driver, dataset: str, timeout: int = 20) -> dict:
    print(f"[6/7] Extracting {dataset} hourly table")
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
        const dataset = arguments[0];
        const table = document.querySelector(".ht_master table.htCore");
        const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
        const parseNumber = (value) => {
            const text = normalize(value).replace(/,/g, "");
            if (!text) return null;
            const parsed = Number(text);
            return Number.isFinite(parsed) ? parsed : null;
        };
        const normalizeDate = (value) => normalize(value).replace(/\//g, "-");
        const hourFromProduct = (product) => {
            const match = normalize(product).match(/^(\d{2})-(\d{2})$/);
            if (!match) return null;
            return Number(match[1]) + 1;
        };
        const summaryNames = new Set(["min", "max", "avg", "nett"]);

        const headers = Array.from(table.querySelectorAll("thead th"))
            .map((cell) => normalize(cell.textContent));
        const dateHeaders = headers.slice(1).map((label) => ({
            label,
            date: normalizeDate(label)
        }));
        const rows = Array.from(table.querySelectorAll("tbody tr"))
            .map((row) => Array.from(row.querySelectorAll("td, th"))
                .map((cell) => normalize(cell.textContent)))
            .filter((cells) => cells.length > 1)
            .map((cells) => ({
                product: cells[0],
                values: cells.slice(1)
            }))
            .filter((row) => row.product && !summaryNames.has(row.product.toLowerCase()));

        const hourlyRecords = [];
        rows.forEach((row) => {
            dateHeaders.forEach((header, index) => {
                hourlyRecords.push({
                    dataset,
                    delivery_date: header.date,
                    product: row.product,
                    hour: hourFromProduct(row.product),
                    hour_label: row.product,
                    value: parseNumber(row.values[index])
                });
            });
        });

        const returnedDates = dateHeaders
            .map((header) => header.date)
            .filter(Boolean);

        return {
            dataset,
            columns: dateHeaders,
            returned_dates: returnedDates,
            hourly_records: hourlyRecords,
            record_count: hourlyRecords.length
        };
        """,
        dataset,
    )
    print(
        f"[6/7] {dataset} table has {len(result['columns'])} date columns and "
        f"{result['record_count']} hourly cells"
    )
    if result["columns"]:
        print(
            f"[6/7] {dataset} returned dates: "
            + ", ".join(column["date"] for column in result["columns"])
        )
    return result


def delivery_timestamp(delivery_date_value: str, hour: int) -> datetime:
    parsed_date = date.fromisoformat(delivery_date_value)
    return datetime.combine(parsed_date, datetime.min.time()) + timedelta(hours=hour - 1)


def enrich_hourly_records(
    dataset: str,
    records: list[dict],
    search_delivery_date: date,
    returned_dates: list[str],
) -> list[dict]:
    window_start_date = date.fromisoformat(returned_dates[0]) if returned_dates else None
    window_end_date = date.fromisoformat(returned_dates[-1]) if returned_dates else None
    enriched_records = []

    for record in records:
        delivery_day = date.fromisoformat(record["delivery_date"])
        enriched_record = {
            **record,
            "search_delivery_date": search_delivery_date.isoformat(),
            "window_start_date": window_start_date.isoformat()
            if window_start_date
            else None,
            "window_end_date": window_end_date.isoformat() if window_end_date else None,
            "window_offset_days": (delivery_day - search_delivery_date).days,
            "category": "Price in USD",
            "dataset": dataset,
        }
        enriched_records.append(enriched_record)

    print(
        f"[6/7] Enriched {len(enriched_records)} {dataset} records with "
        f"search window {window_start_date} to {window_end_date}"
    )
    return enriched_records


def build_search_chunks(start_date: date, end_date: date) -> list[dict]:
    if end_date < start_date:
        raise ValueError("end_date must be greater than or equal to start_date")

    chunks = []
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=6), end_date)
        chunks.append(
            {
                "chunk_start": chunk_start,
                "chunk_end": chunk_end,
                "search_date": chunk_end,
            }
        )
        chunk_start = chunk_end + timedelta(days=1)

    return chunks


def filter_existing_records(collection, records: list[dict]) -> tuple[list[dict], int]:
    if not records:
        return [], 0

    deduped_records = {}
    for record in records:
        deduped_records[(record["delivery_date"], record["hour"])] = record

    unique_records = list(deduped_records.values())
    delivery_dates = sorted({record["delivery_date"] for record in unique_records})
    existing_keys = set()

    for existing_record in collection.find(
        {"delivery_date": {"$in": delivery_dates}},
        {"delivery_date": 1, "hour": 1},
    ):
        existing_keys.add((existing_record.get("delivery_date"), existing_record.get("hour")))

    new_records = [
        record
        for record in unique_records
        if (record["delivery_date"], record["hour"]) not in existing_keys
    ]
    skipped_existing = len(unique_records) - len(new_records)
    if skipped_existing:
        print(
            f"[db] Skipping {skipped_existing} existing records already present in "
            f"{collection.name}"
        )
    return new_records, skipped_existing


def upsert_hourly_records(
    collection,
    dataset: str,
    records: list[dict],
    now: datetime,
) -> dict:
    records_to_write, skipped_existing = filter_existing_records(collection, records)
    print(
        f"[db] Preparing {len(records_to_write)} {dataset} hourly records for collection "
        f"{collection.name}"
    )
    if dataset == "constrained":
        data_source = CONSTRAINED_DATA_SOURCE
        field_name = "area_price_usd_per_mwh"
    else:
        data_source = UNCONSTRAINED_DATA_SOURCE
        field_name = "price_usd_per_mwh"

    operations = []
    for record in records_to_write:
        operations.append(
            UpdateOne(
                {
                    "delivery_date": record["delivery_date"],
                    "hour": record["hour"],
                },
                {
                    "$set": {
                        "timestamp": delivery_timestamp(record["delivery_date"], record["hour"]),
                        "delivery_date": record["delivery_date"],
                        "hour": record["hour"],
                        "hour_label": record["hour_label"],
                        "product": record["product"],
                        field_name: record["value"],
                        "search_delivery_date": record["search_delivery_date"],
                        "window_start_date": record["window_start_date"],
                        "window_end_date": record["window_end_date"],
                        "window_offset_days": record["window_offset_days"],
                        "category": record["category"],
                        "metadata": {
                            "data_source": data_source,
                            "source_page": AREA_RESULTS_URL,
                            "category": record["category"],
                            "search_delivery_date": record["search_delivery_date"],
                            "window_start_date": record["window_start_date"],
                            "window_end_date": record["window_end_date"],
                            "window_offset_days": record["window_offset_days"],
                        },
                        "source_file": None,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                },
                upsert=True,
            )
        )

    if not operations:
        return {"imported": 0, "updated": 0, "skipped_existing": skipped_existing}

    result = collection.bulk_write(operations, ordered=False)
    summary = {
        "imported": result.upserted_count,
        "updated": result.matched_count,
        "skipped_existing": skipped_existing,
    }
    print(
        f"[db] Upserted {dataset} records into {collection.name}: "
        f"imported={summary['imported']}, updated={summary['updated']}, "
        f"skipped_existing={summary['skipped_existing']}"
    )
    return summary


def store_results(
    mongodb_url: str,
    database_name: str,
    unconstrained_records: list[dict],
    constrained_records: list[dict],
) -> dict:
    print(f"[db] Connecting to MongoDB at {mongodb_url}")
    client = MongoClient(
        mongodb_url,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=10000,
        socketTimeoutMS=None,
        retryWrites=True,
        maxPoolSize=50,
    )
    try:
        db = client[database_name]
        client.admin.command("ping")
        print(f"[db] Connected to database {database_name}")
        now = datetime.now(timezone.utc)
        unconstrained_result = upsert_hourly_records(
            db[UNCONSTRAINED_COLLECTION],
            "unconstrained",
            unconstrained_records,
            now,
        )
        constrained_result = upsert_hourly_records(
            db[CONSTRAINED_COLLECTION],
            "constrained",
            constrained_records,
            now,
        )
        summary = {
            "database": database_name,
            "unconstrained": unconstrained_result,
            "constrained": constrained_result,
        }
        print(f"[db] Storage summary: {summary}")
        return summary
    except ServerSelectionTimeoutError:
        raise RuntimeError(f"Failed to connect to MongoDB at {mongodb_url}")
    finally:
        client.close()
        print("[db] MongoDB connection closed")


def scrape_area_results_for_search_date(
    driver,
    search_date: date,
    timeout: int,
    reopen_schedule: bool,
) -> dict:
    formatted_date = format_delivery_day(search_date)
    if reopen_schedule:
        click_select_schedule(driver, timeout=timeout)

    set_input_by_label(driver, "Delivery Day", formatted_date, timeout=timeout)
    select_category(driver, "Price in USD", timeout=timeout)

    set_constrained_toggle(driver, False, timeout=timeout)
    click_search(driver, timeout=timeout)
    unconstrained_table = extract_hourly_table(driver, "unconstrained", timeout=timeout)
    unconstrained_records = enrich_hourly_records(
        "unconstrained",
        unconstrained_table["hourly_records"],
        search_date,
        unconstrained_table["returned_dates"],
    )

    click_select_schedule(driver, timeout=timeout)
    set_input_by_label(driver, "Delivery Day", formatted_date, timeout=timeout)
    select_category(driver, "Price in USD", timeout=timeout)
    set_constrained_toggle(driver, True, timeout=timeout)
    click_search(driver, timeout=timeout)
    constrained_table = extract_hourly_table(driver, "constrained", timeout=timeout)
    constrained_records = enrich_hourly_records(
        "constrained",
        constrained_table["hourly_records"],
        search_date,
        constrained_table["returned_dates"],
    )

    return {
        "search_date": search_date.isoformat(),
        "formatted_search_date": formatted_date,
        "returned_dates": {
            "unconstrained": unconstrained_table["columns"],
            "constrained": constrained_table["columns"],
        },
        "unconstrained_records": unconstrained_records,
        "constrained_records": constrained_records,
    }


def run(
    start_date: date,
    end_date: date,
    timeout: int,
    headless: bool,
    observe_seconds: int,
) -> dict:
    username, password, mongodb_url, database_name = load_config()
    chunks = build_search_chunks(start_date, end_date)
    print(
        f"[run] Starting area results scraper for start_date={start_date.isoformat()}, "
        f"end_date={end_date.isoformat()}, chunks={len(chunks)}, timeout={timeout}, "
        f"headless={headless}, observe_seconds={observe_seconds}"
    )
    for index, chunk in enumerate(chunks, start=1):
        print(
            f"[run] Chunk {index}/{len(chunks)}: "
            f"{chunk['chunk_start'].isoformat()} -> {chunk['chunk_end'].isoformat()} "
            f"(search {chunk['search_date'].isoformat()})"
        )

    driver = create_driver(headless=headless)
    try:
        login(driver, username, password, timeout=timeout)
        print(f"[2/7] Opening {AREA_RESULTS_URL}")
        driver.get(AREA_RESULTS_URL)
        wait_for_page_settle(driver, timeout=timeout, extra_delay=3.0)

        chunk_results = []
        total_unconstrained_records = 0
        total_constrained_records = 0
        total_imported_unconstrained = 0
        total_imported_constrained = 0
        total_skipped_unconstrained = 0
        total_skipped_constrained = 0

        for index, chunk in enumerate(chunks, start=1):
            print(
                f"[run] Executing chunk {index}/{len(chunks)} with search date "
                f"{chunk['search_date'].isoformat()}"
            )
            chunk_result = scrape_area_results_for_search_date(
                driver=driver,
                search_date=chunk["search_date"],
                timeout=timeout,
                reopen_schedule=index > 1,
            )
            storage_result = store_results(
                mongodb_url,
                database_name,
                chunk_result["unconstrained_records"],
                chunk_result["constrained_records"],
            )
            chunk_result["storage"] = storage_result
            chunk_results.append(chunk_result)

            total_unconstrained_records += len(chunk_result["unconstrained_records"])
            total_constrained_records += len(chunk_result["constrained_records"])
            total_imported_unconstrained += storage_result["unconstrained"]["imported"]
            total_imported_constrained += storage_result["constrained"]["imported"]
            total_skipped_unconstrained += storage_result["unconstrained"]["skipped_existing"]
            total_skipped_constrained += storage_result["constrained"]["skipped_existing"]

        result = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "chunks": [
                {
                    "chunk_start": chunk["chunk_start"].isoformat(),
                    "chunk_end": chunk["chunk_end"].isoformat(),
                    "search_date": chunk["search_date"].isoformat(),
                }
                for chunk in chunks
            ],
            "searched_chunk_count": len(chunks),
            "fetched_record_counts": {
                "unconstrained": total_unconstrained_records,
                "constrained": total_constrained_records,
            },
            "stored_record_counts": {
                "unconstrained_imported": total_imported_unconstrained,
                "constrained_imported": total_imported_constrained,
                "unconstrained_skipped_existing": total_skipped_unconstrained,
                "constrained_skipped_existing": total_skipped_constrained,
            },
            "results": chunk_results,
        }
        print(
            "[7/7] Range storage summary: "
            f"unconstrained imported={total_imported_unconstrained}, "
            f"constrained imported={total_imported_constrained}, "
            f"unconstrained skipped={total_skipped_unconstrained}, "
            f"constrained skipped={total_skipped_constrained}"
        )
        print(f"[run] Final result summary: {result}")
        return result
    finally:
        if observe_seconds > 0:
            print(f"Keeping browser open for {observe_seconds} seconds")
            time.sleep(observe_seconds)
        driver.quit()
        print("[driver] Firefox closed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone SAPP area results test scraper for constrained and unconstrained prices."
    )
    parser.add_argument(
        "--delivery-date",
        help="Legacy single search date in YYYY-MM-DD format. Searches one 7-day window ending on this date.",
    )
    parser.add_argument(
        "--start-date",
        help="Start delivery date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end-date",
        help="End delivery date in YYYY-MM-DD format. If omitted with --start-date, a 7-day chunk is assumed.",
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
        help="Run Firefox headless. Default is visible.",
    )
    parser.add_argument(
        "--observe-seconds",
        type=int,
        default=30,
        help="Seconds to keep the browser open before closing.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.start_date:
        start_date = date.fromisoformat(args.start_date)
        end_date = (
            date.fromisoformat(args.end_date)
            if args.end_date
            else start_date + timedelta(days=6)
        )
    else:
        delivery_date_value = (
            date.fromisoformat(args.delivery_date)
            if args.delivery_date
            else datetime.now().date()
        )
        start_date = delivery_date_value
        end_date = delivery_date_value

    result = run(
        start_date=start_date,
        end_date=end_date,
        timeout=args.timeout,
        headless=args.headless,
        observe_seconds=args.observe_seconds,
    )
    print(result)


if __name__ == "__main__":
    main()

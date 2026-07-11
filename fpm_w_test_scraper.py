import argparse
import time
from datetime import date, datetime, time as dt_time, timedelta
from typing import Optional

from app.core.time_of_use import get_time_of_use_period
from area_results_test_scraper import (
    click_search,
    create_driver,
    find_field_input,
    load_config,
    login,
    wait_for_page_settle,
    set_constrained_toggle,
    set_input_by_label,
)
from sapp_scraper import (
    SappExtractionJob,
    delivery_timestamp,
    hour_label_from_hour,
    store_records_in_database,
)
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = "https://trading.sappmtp.com"
LOGIN_URL = f"{BASE_URL}/account/login?returnUrl=%2F"
FPM_RESULTS_URL = f"{BASE_URL}/amt/prices-and-turnover-X-fpm"

MARKET_KEY = "fpm_w"
MARKET_LABEL = "FPM-W"
AREA_VALUE = "ZAML"
CATEGORY_VALUE = "Price in USD"
CURRENCY_VALUE = "USD"

CONSTRAINED_COLLECTION = "sapp_fpm_w_constrained_area_results"
UNCONSTRAINED_COLLECTION = "sapp_fpm_w_unconstrained_area_results"

CONSTRAINED_DATA_SOURCE = "SAPP_AMT_FPM_W_CONSTRAINED_PRICE_RESULTS"
UNCONSTRAINED_DATA_SOURCE = "SAPP_AMT_FPM_W_UNCONSTRAINED_PRICE_RESULTS"

MONGO_SERVER_SELECTION_TIMEOUT_MS = 30000
MONGO_CONNECT_TIMEOUT_MS = 30000
MONGO_SOCKET_TIMEOUT_MS = 30000
MONGO_PING_RETRIES = 3
MONGO_PING_RETRY_DELAY_SECONDS = 5


def normalize_text(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def format_delivery_week(value: date) -> str:
    return value.strftime("%Y/%m/%d")


def week_label_to_date(value: str) -> str:
    return value.replace("/", "-")


def parse_week(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip().replace("/", "-")
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def parse_number(value) -> Optional[float]:
    if value is None:
        return None
    text = normalize_text(value).replace(",", "")
    if not text:
        return None
    try:
        parsed = float(text)
        return parsed if parsed == parsed else None
    except ValueError:
        return None


def select_combo_by_label(driver, label: str, value: str, timeout: int = 20) -> None:
    print(f"[2/7] Selecting {label}: {value}")
    wait_for_page_settle(driver, timeout=timeout)
    input_element = WebDriverWait(driver, timeout).until(
        lambda d: find_field_input(d, label)
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


def is_visible_field_input(driver, label: str) -> bool:
    return bool(
        driver.execute_script(
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
                    if (input) {
                        const rect = input.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 && input.offsetParent !== null) {
                            return true;
                        }
                    }
                    node = node.parentElement;
                }
            }
            return false;
            """,
            label,
        )
    )


def ensure_schedule_panel_open(driver, timeout: int = 20) -> None:
    if is_visible_field_input(driver, "Delivery Week"):
        return

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
    WebDriverWait(driver, timeout).until(lambda d: is_visible_field_input(d, "Delivery Week"))
    wait_for_page_settle(driver, timeout=timeout, extra_delay=1.5)


def week_range_contains(start_week: date, end_week: date) -> list[str]:
    weeks = []
    current = start_week
    while current <= end_week:
        weeks.append(current.isoformat())
        current += timedelta(weeks=1)
    return weeks


def normalize_requested_range(start_date: date, end_date: date) -> tuple[date, date]:
    normalized_start = normalize_week_start(start_date)
    normalized_end = normalize_week_start(end_date)
    current_week_start = normalize_week_start(datetime.now().date())

    if normalized_end > current_week_start:
        print(
            f"[run] Requested end_date {end_date.isoformat()} is in the future; "
            f"using current week start {current_week_start.isoformat()} instead"
        )
        normalized_end = current_week_start

    if normalized_end < normalized_start:
        raise ValueError(
            "Requested range starts after the last scrapeable week."
        )

    return normalized_start, normalized_end


def build_search_chunks(start_date: date, end_date: date) -> list[dict]:
    if end_date < start_date:
        raise ValueError("end_date must be greater than or equal to start_date")

    chunks = []
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(weeks=6), end_date)
        chunks.append(
            {
                "chunk_start": chunk_start,
                "chunk_end": chunk_end,
                "search_date": chunk_end,
            }
        )
        chunk_start = chunk_end + timedelta(weeks=1)

    return chunks


def expected_chunk_weeks(chunk_start: date, chunk_end: date) -> list[str]:
    return week_range_contains(chunk_start, chunk_end)


def validate_returned_weeks_for_chunk(
    dataset: str,
    returned_weeks: list[str],
    chunk_start: date,
    chunk_end: date,
) -> None:
    expected_weeks = expected_chunk_weeks(chunk_start, chunk_end)
    if not returned_weeks:
        raise RuntimeError(
            f"{dataset} search for chunk {chunk_start.isoformat()} to "
            f"{chunk_end.isoformat()} returned no weeks."
        )

    returned_week_set = set(returned_weeks)
    expected_week_set = set(expected_weeks)
    missing_weeks = sorted(expected_week_set - returned_week_set)
    unexpected_weeks = sorted(returned_week_set - expected_week_set)

    if missing_weeks:
        raise RuntimeError(
            f"{dataset} search missed expected weeks for chunk "
            f"{chunk_start.isoformat()} to {chunk_end.isoformat()}. "
            f"Returned={returned_weeks}, missing={missing_weeks or 'none'}, "
            f"unexpected={unexpected_weeks or 'none'}."
        )

    print(
        f"[6/7] {dataset} returned weeks cover requested chunk "
        f"{chunk_start.isoformat()} to {chunk_end.isoformat()}"
    )
    if unexpected_weeks:
        print(
            f"[6/7] {dataset} search also returned extra weeks outside the chunk: "
            f"{unexpected_weeks}"
        )


def find_weekly_table(driver, dataset: str, timeout: int = 20) -> dict:
    print(f"[6/7] Extracting {dataset} weekly table")
    wait_timeout = timeout

    from selenium.webdriver.support.ui import WebDriverWait

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

    WebDriverWait(driver, wait_timeout).until(table_exists)
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
        const summaryNames = new Set(["min", "max", "avg", "nett", "net"]);

        const headers = Array.from(table.querySelectorAll("thead th"))
            .map((cell) => normalize(cell.textContent));
        const weekHeaders = headers.slice(1).map((label) => ({
            label,
            week: normalizeDate(label)
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

        const weeklyRecords = [];
        rows.forEach((row) => {
            weekHeaders.forEach((header, index) => {
                weeklyRecords.push({
                    dataset,
                    delivery_week: header.week,
                    product_label: row.product,
                    product: row.product,
                    value: parseNumber(row.values[index])
                });
            });
        });

        const returnedWeeks = weekHeaders
            .map((header) => header.week)
            .filter(Boolean);

        return {
            dataset,
            columns: weekHeaders,
            returned_weeks: returnedWeeks,
            weekly_records: weeklyRecords,
            record_count: weeklyRecords.length
        };
        """,
        dataset,
    )
    print(
        f"[6/7] {dataset} table has {len(result['columns'])} week columns and "
        f"{result['record_count']} weekly cells"
    )
    if result["columns"]:
        print(
            f"[6/7] {dataset} returned weeks: "
            + ", ".join(column["week"] for column in result["columns"])
        )
    return result


def normalize_product(value) -> Optional[str]:
    normalized = normalize_text(value)
    aliases = {
        "off-peak": "off_peak",
        "off peak": "off_peak",
        "off_peak": "off_peak",
        "peak": "peak",
        "standard": "standard",
    }
    return aliases.get(normalized)


def enrich_weekly_records(
    dataset: str,
    records: list[dict],
    search_delivery_week: date,
    returned_weeks: list[str],
) -> list[dict]:
    window_start_week = parse_week(returned_weeks[0]) if returned_weeks else None
    window_end_week = parse_week(returned_weeks[-1]) if returned_weeks else None
    product_prices: dict[str, dict[str, dict[str, object]]] = {}

    for record in records:
        delivery_week = parse_week(record["delivery_week"])
        if delivery_week is None:
            continue

        product = normalize_product(record["product"])
        if product is None or record["value"] is None:
            continue

        product_prices.setdefault(delivery_week.isoformat(), {})[product] = {
            "value": record["value"],
            "product_label": record["product_label"],
        }

    hourly_records = []
    for week_iso in returned_weeks:
        week_start = parse_week(week_iso)
        if week_start is None:
            continue

        week_products = product_prices.get(week_iso, {})
        if not week_products:
            continue

        period_end = week_start + timedelta(days=6)
        for offset in range(7):
            delivery_day = week_start + timedelta(days=offset)
            for hour in range(1, 25):
                product = get_time_of_use_period(delivery_day, hour)
                price_row = week_products.get(product)
                if price_row is None:
                    continue

                record = {
                    "timestamp": delivery_timestamp(delivery_day, hour),
                    "market": MARKET_KEY,
                    "delivery_date": delivery_day.isoformat(),
                    "source_delivery_date": search_delivery_week.isoformat(),
                    "period_start_date": week_start.isoformat(),
                    "period_end_date": period_end.isoformat(),
                    "hour": hour,
                    "hour_label": hour_label_from_hour(hour),
                    "product": product,
                    "product_label": price_row["product_label"],
                    "category": CATEGORY_VALUE,
                    "currency": CURRENCY_VALUE,
                    "area": AREA_VALUE,
                    "dataset": dataset,
                    "metadata": {
                        "data_source": (
                            CONSTRAINED_DATA_SOURCE
                            if dataset == "constrained"
                            else UNCONSTRAINED_DATA_SOURCE
                        ),
                        "source_file": None,
                        "market": MARKET_KEY,
                        "area": AREA_VALUE,
                        "currency": CURRENCY_VALUE,
                        "category": CATEGORY_VALUE,
                        "source_delivery_date": search_delivery_week.isoformat(),
                        "period_start_date": week_start.isoformat(),
                        "period_end_date": period_end.isoformat(),
                    },
                    "source_file": None,
                }
                if dataset == "constrained":
                    record["area_price_usd_per_mwh"] = price_row["value"]
                else:
                    record["price_usd_per_mwh"] = price_row["value"]
                hourly_records.append(record)

    print(
        f"[6/7] Expanded {len(hourly_records)} {dataset} hourly records with "
        f"search window {window_start_week} to {window_end_week}"
    )
    return hourly_records


def preflight_mongo_connection(mongodb_url: str, database_name: str) -> None:
    print(f"[db] Preflighting MongoDB connection for database {database_name}")
    from pymongo import MongoClient

    client = MongoClient(
        mongodb_url,
        serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
        connectTimeoutMS=MONGO_CONNECT_TIMEOUT_MS,
        socketTimeoutMS=MONGO_SOCKET_TIMEOUT_MS,
        retryWrites=True,
        maxPoolSize=1,
    )
    try:
        client.admin.command("ping")
        print(f"[db] MongoDB preflight passed for database {database_name}")
    finally:
        client.close()
        print("[db] MongoDB connection closed")


def scrape_weekly_results_for_search_week(
    driver,
    search_week: date,
    chunk_start: date,
    chunk_end: date,
    timeout: int,
) -> dict:
    formatted_week = format_delivery_week(search_week)
    ensure_schedule_panel_open(driver, timeout=timeout)

    select_combo_by_label(driver, "Market", MARKET_LABEL, timeout=timeout)
    set_input_by_label(driver, "Delivery Week", formatted_week, timeout=timeout)
    select_combo_by_label(driver, "Category", CATEGORY_VALUE, timeout=timeout)
    select_combo_by_label(driver, "Currency", CURRENCY_VALUE, timeout=timeout)
    select_combo_by_label(driver, "Area", AREA_VALUE, timeout=timeout)

    set_constrained_toggle(driver, True, timeout=timeout)
    click_search(driver, timeout=timeout)
    constrained_table = find_weekly_table(driver, "constrained", timeout=timeout)
    validate_returned_weeks_for_chunk(
        "constrained",
        constrained_table["returned_weeks"],
        chunk_start,
        chunk_end,
    )
    constrained_records = enrich_weekly_records(
        "constrained",
        constrained_table["weekly_records"],
        search_week,
        constrained_table["returned_weeks"],
    )

    ensure_schedule_panel_open(driver, timeout=timeout)
    set_constrained_toggle(driver, False, timeout=timeout)
    click_search(driver, timeout=timeout)
    unconstrained_table = find_weekly_table(driver, "unconstrained", timeout=timeout)
    validate_returned_weeks_for_chunk(
        "unconstrained",
        unconstrained_table["returned_weeks"],
        chunk_start,
        chunk_end,
    )
    unconstrained_records = enrich_weekly_records(
        "unconstrained",
        unconstrained_table["weekly_records"],
        search_week,
        unconstrained_table["returned_weeks"],
    )

    return {
        "search_week": search_week.isoformat(),
        "formatted_search_week": formatted_week,
        "returned_weeks": {
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
    start_date, end_date = normalize_requested_range(start_date, end_date)
    chunks = build_search_chunks(start_date, end_date)
    print(
        f"[run] Starting FPM-W weekly scraper for start_date={start_date.isoformat()}, "
        f"end_date={end_date.isoformat()}, chunks={len(chunks)}, timeout={timeout}, "
        f"headless={headless}, observe_seconds={observe_seconds}"
    )
    for index, chunk in enumerate(chunks, start=1):
        print(
            f"[run] Chunk {index}/{len(chunks)}: "
            f"{chunk['chunk_start'].isoformat()} -> {chunk['chunk_end'].isoformat()} "
            f"(search {chunk['search_date'].isoformat()})"
        )

    preflight_mongo_connection(mongodb_url, database_name)

    driver = None
    try:
        driver = create_driver(headless=headless)
        login(driver, username, password, timeout=timeout)
        print(f"[2/7] Opening {FPM_RESULTS_URL}")
        driver.get(FPM_RESULTS_URL)
        wait_for_page_settle(driver, timeout=timeout, extra_delay=3.0)

        chunk_results = []
        total_unconstrained_records = 0
        total_constrained_records = 0
        total_imported_unconstrained = 0
        total_imported_constrained = 0

        constrained_job = SappExtractionJob(
            name="fpm_w_constrained_area_results",
            subject_template="MTP - FPM-W - Constrained Area Results for {delivery_date}",
            data_source=CONSTRAINED_DATA_SOURCE,
            collection_name=CONSTRAINED_COLLECTION,
            unique_key_fields=("market", "delivery_date", "hour"),
            extractor=lambda _path, _job: [],
        )
        unconstrained_job = SappExtractionJob(
            name="fpm_w_unconstrained_area_results",
            subject_template="MTP - FPM-W - Unconstrained Area Results for {delivery_date}",
            data_source=UNCONSTRAINED_DATA_SOURCE,
            collection_name=UNCONSTRAINED_COLLECTION,
            unique_key_fields=("market", "delivery_date", "hour"),
            extractor=lambda _path, _job: [],
        )

        for index, chunk in enumerate(chunks, start=1):
            print(
                f"[run] Executing chunk {index}/{len(chunks)} with search week "
                f"{chunk['search_date'].isoformat()}"
            )
            chunk_result = scrape_weekly_results_for_search_week(
                driver=driver,
                search_week=chunk["search_date"],
                chunk_start=chunk["chunk_start"],
                chunk_end=chunk["chunk_end"],
                timeout=timeout,
            )
            storage_constrained = store_records_in_database(
                chunk_result["constrained_records"],
                constrained_job,
            )
            storage_unconstrained = store_records_in_database(
                chunk_result["unconstrained_records"],
                unconstrained_job,
            )
            chunk_result["storage"] = {
                "constrained": storage_constrained,
                "unconstrained": storage_unconstrained,
            }
            chunk_results.append(chunk_result)

            total_unconstrained_records += len(chunk_result["unconstrained_records"])
            total_constrained_records += len(chunk_result["constrained_records"])
            total_imported_unconstrained += storage_unconstrained["imported"]
            total_imported_constrained += storage_constrained["imported"]

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
            },
            "results": chunk_results,
        }
        print(
            "[7/7] Range storage summary: "
            f"unconstrained imported={total_imported_unconstrained}, "
            f"constrained imported={total_imported_constrained}"
        )
        print(f"[run] Final result summary: {result}")
        return result
    finally:
        if observe_seconds > 0:
            print(f"Keeping browser open for {observe_seconds} seconds")
            from time import sleep

            sleep(observe_seconds)
        if driver is not None:
            driver.quit()
            print("[driver] Firefox closed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone SAPP FPM-W weekly area results scraper."
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="Start week in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        help="End week in YYYY-MM-DD format.",
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
        help="Run Firefox headless.",
    )
    parser.add_argument(
        "--observe-seconds",
        type=int,
        default=30,
        help="Keep the browser open this many seconds before exiting.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
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

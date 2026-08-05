import argparse
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
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
MONGO_SERVER_SELECTION_TIMEOUT_MS = 30000
MONGO_CONNECT_TIMEOUT_MS = 30000
MONGO_SOCKET_TIMEOUT_MS = 30000
MONGO_PING_RETRIES = 3
MONGO_PING_RETRY_DELAY_SECONDS = 5
BULK_WRITE_CHUNK_SIZE = 500
SCRAPE_SCOPE_CHOICES = ("prices", "volumes", "all")
SCRAPE_VARIANTS = {
    "prices": (
        {
            "category": "Price in USD",
            "fields": {
                "constrained": "area_price_usd_per_mwh",
                "unconstrained": "price_usd_per_mwh",
            },
        },
    ),
    "volumes": (
        {
            "category": "Total Purchase Volume",
            "fields": {
                "constrained": "area_purchase_mw",
                "unconstrained": "total_purchase_volume_mw",
            },
        },
        {
            "category": "Total Sale Volume",
            "fields": {
                "constrained": "area_sales_mw",
                "unconstrained": "total_sales_volume_mw",
            },
        },
    ),
    "all": (
        {
            "category": "Price in USD",
            "fields": {
                "constrained": "area_price_usd_per_mwh",
                "unconstrained": "price_usd_per_mwh",
            },
        },
        {
            "category": "Total Purchase Volume",
            "fields": {
                "constrained": "area_purchase_mw",
                "unconstrained": "total_purchase_volume_mw",
            },
        },
        {
            "category": "Total Sale Volume",
            "fields": {
                "constrained": "area_sales_mw",
                "unconstrained": "total_sales_volume_mw",
            },
        },
    ),
}


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


def open_area_results_page(
    driver,
    username: str,
    password: str,
    timeout: int = 20,
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
        print("[2/7] Session expired on turnover page; logging in again")
        login(driver, username, password, timeout=timeout)

    if AREA_RESULTS_URL not in driver.current_url:
        driver.get(AREA_RESULTS_URL)
    wait_for_page_settle(driver, timeout=timeout, extra_delay=1.5)

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
        print("[2/7] Redirected back to login; authenticating again")
        login(driver, username, password, timeout=timeout)
        driver.get(AREA_RESULTS_URL)
        wait_for_page_settle(driver, timeout=timeout, extra_delay=1.5)


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


def parse_month_year_from_text(text: str) -> Optional[tuple[int, int]]:
    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    match = re.search(
        r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})",
        text.lower(),
    )
    if not match:
        return None
    return months[match.group(1)], int(match.group(2))


def find_calendar_nav_button(driver, calendar, direction: str):
    return driver.execute_script(
        r"""
        const calendar = arguments[0];
        const direction = arguments[1];
        const isVisible = (element) => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && element.offsetParent !== null;
        };
        const buttons = Array.from(calendar.querySelectorAll("button")).filter(isVisible);
        const predicate = direction === "next"
            ? (button) => {
                const title = (button.getAttribute("title") || "").trim().toLowerCase();
                const aria = (button.getAttribute("aria-label") || "").trim().toLowerCase();
                const cls = (button.getAttribute("class") || "").toLowerCase();
                return title.includes("next") || aria.includes("next") || cls.includes("next");
            }
            : (button) => {
                const title = (button.getAttribute("title") || "").trim().toLowerCase();
                const aria = (button.getAttribute("aria-label") || "").trim().toLowerCase();
                const cls = (button.getAttribute("class") || "").toLowerCase();
                return title.includes("previous") || title.includes("prev")
                    || aria.includes("previous") || aria.includes("prev")
                    || cls.includes("prev");
            };
        return buttons.find(predicate) || null;
        """,
        calendar,
        direction,
    )


def click_calendar_day(driver, calendar, target_date: date):
    return driver.execute_script(
        r"""
        const calendar = arguments[0];
        const dayText = String(arguments[1]);
        const targetMonth = Number(arguments[2]);
        const targetYear = Number(arguments[3]);
        const isVisible = (element) => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && element.offsetParent !== null;
        };
        const sameMonth = (button) => {
            const cls = (button.getAttribute("class") || "").toLowerCase();
            const aria = (button.getAttribute("aria-label") || "").toLowerCase();
            const title = (button.getAttribute("title") || "").toLowerCase();
            if (cls.includes("other-month") || cls.includes("adjacent")) return false;
            if (aria.includes("other month") || title.includes("other month")) return false;
            if (aria.includes(String(targetYear)) || title.includes(String(targetYear))) return true;
            return true;
        };
        const buttons = Array.from(calendar.querySelectorAll("button")).filter(isVisible);
        const candidates = buttons.filter((button) => {
            const text = (button.textContent || "").replace(/\s+/g, " ").trim();
            return text === dayText && sameMonth(button);
        });
        return candidates[0] || null;
        """,
        calendar,
        target_date.day,
        target_date.month,
        target_date.year,
    )


def set_date_via_calendar(driver, label: str, value: str, timeout: int = 20) -> None:
    print(f"🗓️ [2/7] Calendar fallback for {label}: {value}")
    target_date = date.fromisoformat(value.replace("/", "-"))
    wait_for_page_settle(driver, timeout=timeout)

    def find_calendar_toggle(driver):
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

            const isVisible = (element) => {
                if (!element) return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            for (const label of labels) {
                let node = label;
                for (let depth = 0; node && depth < 6; depth += 1) {
                    const toggles = Array.from(node.querySelectorAll("button")).filter(isVisible);
                    const toggle = toggles.find((button) => {
                        const title = (button.getAttribute("title") || "").trim().toLowerCase();
                        const aria = (button.getAttribute("aria-label") || "").trim().toLowerCase();
                        return title === "toggle calendar" || aria === "toggle calendar";
                    });
                    if (toggle) return toggle;
                    node = node.parentElement;
                }
            }
            return null;
            """,
            label,
        )

    def find_visible_calendar_popup(driver):
        return driver.execute_script(
            r"""
            const calendars = Array.from(document.querySelectorAll(
                ".k-calendar, .k-daterangepicker .k-popup, .k-animation-container"
            ));
            const isVisible = (element) => {
                if (!element) return false;
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && element.offsetParent !== null;
            };
            return calendars.find(isVisible) || null;
            """
        )

    toggle = WebDriverWait(driver, timeout).until(find_calendar_toggle)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", toggle)
    time.sleep(0.12)
    driver.execute_script("arguments[0].click();", toggle)

    calendar = WebDriverWait(driver, timeout).until(find_visible_calendar_popup)

    for _ in range(24):
        popup_text = driver.execute_script(
            """
            const popup = arguments[0];
            return (popup.innerText || popup.textContent || "").replace(/\\s+/g, " ").trim();
            """,
            calendar,
        )
        current_month_year = parse_month_year_from_text(popup_text)
        if current_month_year == (target_date.month, target_date.year):
            break

        nav_direction = None
        if current_month_year is not None:
            current_month, current_year = current_month_year
            current_serial = current_year * 12 + current_month
            target_serial = target_date.year * 12 + target_date.month
            nav_direction = "next" if target_serial > current_serial else "prev"

        if nav_direction is None:
            break

        nav_button = find_calendar_nav_button(driver, calendar, nav_direction)
        if nav_button is None:
            break

        driver.execute_script("arguments[0].click();", nav_button)
        time.sleep(0.15)
        calendar = WebDriverWait(driver, timeout).until(find_visible_calendar_popup)

    day_button = WebDriverWait(driver, timeout).until(
        lambda d: click_calendar_day(d, calendar, target_date)
    )
    driver.execute_script("arguments[0].click();", day_button)
    time.sleep(0.3)


def set_input_by_label(driver, label: str, value: str, timeout: int = 20) -> None:
    print(f"📝 [2/7] {label}: {value}")
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
            time.sleep(0.12)

        input_element.send_keys(Keys.TAB)
        time.sleep(0.25)

        actual_value = read_input_value(driver, input_element)
        if actual_value == value:
            print(f"  ✓ {label} set")
            return

        if label == "Delivery Day":
            try:
                set_date_via_calendar(driver, label, value, timeout=timeout)
                actual_value = read_input_value(driver, input_element)
                if actual_value == value:
                    print(f"  ✓ {label} set via calendar")
                    return
            except Exception as exc:
                print(f"  ⚠️ {label} calendar fallback failed: {exc}")

        wait_for_page_settle(driver, timeout=timeout, extra_delay=0.2)
        input_element = WebDriverWait(driver, timeout).until(
            lambda d: find_field_input(d, label)
        )

    raise RuntimeError(
        f"Failed to set {label} to '{value}'. Field kept a different value."
    )


def select_category(driver, value: str, timeout: int = 20) -> None:
    print(f"📝 [2/7] Category: {value}")
    wait_for_page_settle(driver, timeout=timeout)
    input_element = WebDriverWait(driver, timeout).until(
        lambda d: find_field_input(d, "Category")
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", input_element)
    input_element.click()
    time.sleep(0.15)
    select_all_input_text(driver, input_element)
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
    time.sleep(0.25)


def find_constrained_toggle(driver):
    return driver.execute_script(
        r"""
        const isVisible = (element) => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };
        const normalize = (value) => (value || "")
            .replace(/\*/g, "")
            .replace(/\s+/g, " ")
            .trim()
            .toLowerCase();
        const directMatches = Array.from(document.querySelectorAll(
            "kendo-switch[data-cy*='constrained'], [role='switch'][data-cy*='constrained'], .k-switch[data-cy*='constrained']"
        )).filter(isVisible);
        if (directMatches.length) return directMatches[0];

        const labels = Array.from(document.querySelectorAll("label, .form-label, div, span"))
            .filter((element) =>
                isVisible(element)
                && normalize(element.textContent) === "constrained per area result"
            );
        for (const label of labels) {
            let node = label;
            for (let depth = 0; node && depth < 6; depth += 1) {
                const toggles = Array.from(node.querySelectorAll(
                    "kendo-switch, .k-switch, [role='switch'], input[type='checkbox'], .k-switch-track"
                )).filter(isVisible);
                if (toggles.length) {
                    const toggle = toggles[0];
                    return toggle.closest("kendo-switch, .k-switch, [role='switch']") || toggle;
                }
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
    print(f"🔁 [3/7] Toggle -> {'unconstrained' if enabled else 'constrained'}")
    wait_for_page_settle(driver, timeout=timeout)
    toggle = WebDriverWait(driver, timeout).until(find_constrained_toggle)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", toggle)
    time.sleep(0.15)
    before_state = get_toggle_state(driver, toggle)
    should_click = before_state is not enabled
    if before_state is None and enabled is False:
        should_click = False
    if should_click:
        print("  ↳ flipping switch")
        try:
            driver.execute_script(
                """
                const toggle = arguments[0];
                const track = toggle.querySelector?.(".k-switch-track");
                (track || toggle).click();
                """,
                toggle,
            )
        except WebDriverException:
            toggle = WebDriverWait(driver, timeout).until(find_constrained_toggle)
            toggle.click()

        try:
            WebDriverWait(driver, timeout).until(
                lambda d: (
                    lambda refreshed: refreshed is not None
                    and get_toggle_state(d, refreshed) is enabled
                )(find_constrained_toggle(d))
            )
        except TimeoutException:
            print("  ↳ click did not stick; trying keyboard toggle")
            toggle = WebDriverWait(driver, timeout).until(find_constrained_toggle)
            toggle.send_keys(Keys.SPACE)
            WebDriverWait(driver, timeout).until(
                lambda d: (
                    lambda refreshed: refreshed is not None
                    and get_toggle_state(d, refreshed) is enabled
                )(find_constrained_toggle(d))
            )

        time.sleep(0.25)
        wait_for_page_settle(driver, timeout=timeout, extra_delay=0.35)

    toggle = WebDriverWait(driver, timeout).until(find_constrained_toggle)
    after_state = get_toggle_state(driver, toggle)
    print(f"  ✓ toggle state now {after_state}")
    if after_state is not enabled:
        raise RuntimeError(
            f"Failed to set area result toggle to {enabled}. Final state was {after_state}."
        )


def click_search(driver, timeout: int = 20) -> dict:
    print("🔎 [4/7] Searching")
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
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    time.sleep(0.15)
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
    wait_for_page_settle(driver, timeout=timeout, extra_delay=0.9)
    new_url = driver.current_url
    content_changed = (
        driver.execute_script("return document.body ? document.body.innerText.slice(0, 2000) : '';")
        != old_signature
    )
    print("  ✓ search complete")
    return {
        "search_click_method": click_method,
        "search_redirected": new_url != old_url,
        "search_content_changed": content_changed,
    }


def click_select_schedule(driver, timeout: int = 20) -> None:
    print("📅 [5/7] Opening schedule panel")
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
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    time.sleep(0.15)
    driver.execute_script("arguments[0].click();", button)
    WebDriverWait(driver, timeout).until(lambda d: find_field_input(d, "Delivery Day"))
    wait_for_page_settle(driver, timeout=timeout, extra_delay=0.55)


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
    category_label: str,
    value_field: str,
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
            "category": category_label,
            "value_field": value_field,
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


def expected_chunk_dates(chunk_start: date, chunk_end: date) -> list[str]:
    dates = []
    current_date = chunk_start
    while current_date <= chunk_end:
        dates.append(current_date.isoformat())
        current_date += timedelta(days=1)
    return dates


def validate_returned_dates_for_chunk(
    dataset: str,
    returned_dates: list[str],
    chunk_start: date,
    chunk_end: date,
) -> None:
    expected_dates = expected_chunk_dates(chunk_start, chunk_end)
    if not returned_dates:
        raise RuntimeError(
            f"{dataset} search for chunk {chunk_start.isoformat()} to "
            f"{chunk_end.isoformat()} returned no dates."
        )

    returned_date_set = set(returned_dates)
    expected_date_set = set(expected_dates)
    unexpected_dates = sorted(returned_date_set - expected_date_set)
    missing_dates = sorted(expected_date_set - returned_date_set)

    if missing_dates:
        raise RuntimeError(
            f"{dataset} search missed expected dates for chunk "
            f"{chunk_start.isoformat()} to {chunk_end.isoformat()}. "
            f"Returned={returned_dates}, missing={missing_dates or 'none'}, "
            f"unexpected={unexpected_dates or 'none'}."
        )

    print(
        f"[6/7] {dataset} returned dates cover requested chunk "
        f"{chunk_start.isoformat()} to {chunk_end.isoformat()}"
    )
    if unexpected_dates:
        print(
            f"[6/7] {dataset} search also returned extra dates outside the chunk: "
            f"{unexpected_dates}"
        )


def normalize_requested_range(start_date: date, end_date: date) -> tuple[date, date]:
    today = datetime.now().date()
    normalized_end_date = end_date
    if end_date > today:
        normalized_end_date = today
        print(
            f"[run] Requested end_date {end_date.isoformat()} is in the future; "
            f"using {normalized_end_date.isoformat()} instead"
        )

    if normalized_end_date < start_date:
        raise ValueError(
            "Requested range starts in the future. No scrapeable delivery dates remain "
            "after clamping end_date to today."
        )

    return start_date, normalized_end_date


def filter_existing_records(collection, records: list[dict]) -> tuple[list[dict], int]:
    if not records:
        return [], 0

    deduped_records = {}
    for record in records:
        deduped_records[(record["delivery_date"], record["hour"])] = record

    unique_records = list(deduped_records.values())
    if len(unique_records) != len(records):
        print(
            f"[db] Deduplicated {len(records) - len(unique_records)} duplicate rows before write "
            f"for {collection.full_name}"
        )

    print(
        f"[db] Preparing overwrite/upsert for {len(unique_records)} rows in "
        f"{collection.full_name}"
    )
    return unique_records, 0


def summarize_delivery_date_coverage(
    chunk_results: list[dict],
    start_date: date,
    end_date: date,
) -> dict:
    coverage_by_date: dict[str, dict] = {}
    current_date = start_date
    while current_date <= end_date:
        coverage_by_date[current_date.isoformat()] = {
            "delivery_date": current_date.isoformat(),
            "constrained_count": 0,
            "unconstrained_count": 0,
            "search_dates": set(),
        }
        current_date += timedelta(days=1)

    for chunk_result in chunk_results:
        search_date = chunk_result["search_date"]
        for record in chunk_result["constrained_records"]:
            delivery_date_value = record["delivery_date"]
            if delivery_date_value in coverage_by_date:
                coverage_by_date[delivery_date_value]["constrained_count"] += 1
                coverage_by_date[delivery_date_value]["search_dates"].add(search_date)

        for record in chunk_result["unconstrained_records"]:
            delivery_date_value = record["delivery_date"]
            if delivery_date_value in coverage_by_date:
                coverage_by_date[delivery_date_value]["unconstrained_count"] += 1
                coverage_by_date[delivery_date_value]["search_dates"].add(search_date)

    both = []
    constrained_only = []
    unconstrained_only = []
    no_data = []
    per_date = []

    for delivery_date_value in sorted(coverage_by_date.keys()):
        summary = coverage_by_date[delivery_date_value]
        has_constrained = summary["constrained_count"] > 0
        has_unconstrained = summary["unconstrained_count"] > 0

        if has_constrained and has_unconstrained:
            status = "both"
            both.append(delivery_date_value)
        elif has_constrained:
            status = "constrained_only"
            constrained_only.append(delivery_date_value)
        elif has_unconstrained:
            status = "unconstrained_only"
            unconstrained_only.append(delivery_date_value)
        else:
            status = "no_data"
            no_data.append(delivery_date_value)

        per_date.append(
            {
                "delivery_date": delivery_date_value,
                "status": status,
                "constrained_count": summary["constrained_count"],
                "unconstrained_count": summary["unconstrained_count"],
                "search_dates": sorted(summary["search_dates"]),
            }
        )

    return {
        "successful_both_dates": both,
        "constrained_only_dates": constrained_only,
        "unconstrained_only_dates": unconstrained_only,
        "no_data_dates": no_data,
        "counts": {
            "successful_both_dates": len(both),
            "constrained_only_dates": len(constrained_only),
            "unconstrained_only_dates": len(unconstrained_only),
            "no_data_dates": len(no_data),
        },
        "per_date": per_date,
    }


def upsert_hourly_records(
    collection,
    dataset: str,
    records: list[dict],
    now: datetime,
) -> dict:
    records_to_write, skipped_existing = filter_existing_records(collection, records)
    print(
        f"[db] Preparing {len(records_to_write)} {dataset} hourly records for collection "
        f"{collection.full_name}"
    )

    operations = []
    for record in records_to_write:
        field_name = record["value_field"]
        data_source = (
            CONSTRAINED_DATA_SOURCE if dataset == "constrained"
            else UNCONSTRAINED_DATA_SOURCE
        )
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
                            "value_field": field_name,
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

    imported = 0
    updated = 0
    for index in range(0, len(operations), BULK_WRITE_CHUNK_SIZE):
        batch = operations[index : index + BULK_WRITE_CHUNK_SIZE]
        result = collection.bulk_write(batch, ordered=False)
        imported += result.upserted_count
        updated += result.matched_count

    _strictly_verify_written_records(collection, data_source, records_to_write)
    summary = {
        "imported": imported,
        "updated": updated,
        "skipped_existing": skipped_existing,
    }
    return summary


def _strictly_verify_written_records(
    collection,
    data_source: str,
    expected_records: list[dict],
) -> None:
    if not expected_records:
        return

    expected_keys = {
        (record["delivery_date"], record["hour"]) for record in expected_records
    }
    delivery_dates = sorted({record["delivery_date"] for record in expected_records})
    stored_keys = {
        (doc.get("delivery_date"), doc.get("hour"))
        for doc in collection.find(
            {
                "delivery_date": {"$in": delivery_dates},
                "metadata.data_source": data_source,
            },
            {"_id": 0, "delivery_date": 1, "hour": 1},
        )
    }

    missing_keys = sorted(expected_keys - stored_keys)
    if missing_keys:
        raise RuntimeError(
            f"Strict write verification failed for {collection.full_name}: "
            f"missing {len(missing_keys)} rows. First missing keys: {missing_keys[:5]}"
        )

    print(
        f"[db] Strict write verification passed for {collection.full_name}: "
        f"{len(stored_keys)}/{len(expected_keys)} rows present"
    )


def _ping_mongo_with_retry(client: MongoClient, database_name: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, MONGO_PING_RETRIES + 1):
        try:
            print(
                f"[db] Pinging MongoDB for database {database_name} "
                f"(attempt {attempt}/{MONGO_PING_RETRIES})"
            )
            client.admin.command("ping")
            return
        except Exception as exc:
            last_error = exc
            print(f"[db] MongoDB ping attempt {attempt} failed: {exc}")
            if attempt < MONGO_PING_RETRIES:
                time.sleep(MONGO_PING_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Failed to connect to MongoDB database {database_name} after "
        f"{MONGO_PING_RETRIES} attempts"
    ) from last_error


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
    _ping_mongo_with_retry(client, database_name)
    print(f"[db] MongoDB preflight passed for database {database_name}")
    return client, client[database_name]


def close_mongo_connection(client: MongoClient) -> None:
    client.close()
    print("[db] MongoDB connection closed")


def store_results(
    db,
    unconstrained_records: list[dict],
    constrained_records: list[dict],
) -> dict:
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
        "database": db.name,
        "unconstrained": unconstrained_result,
        "constrained": constrained_result,
    }
    print(f"[db] Storage summary: {summary}")
    verify_storage_coverage(
        db[CONSTRAINED_COLLECTION],
        db[UNCONSTRAINED_COLLECTION],
        constrained_records,
        unconstrained_records,
    )
    return summary


def verify_storage_coverage(
    constrained_collection,
    unconstrained_collection,
    constrained_records: list[dict],
    unconstrained_records: list[dict],
) -> None:
    def summarize(collection, dataset: str, records: list[dict]) -> None:
        if not records:
            print(f"[db] {dataset} coverage: no records were prepared")
            return

        delivery_dates = sorted({record["delivery_date"] for record in records})
        expected_start = delivery_dates[0]
        expected_end = delivery_dates[-1]
        stored_dates = sorted(
            {
                doc["delivery_date"]
                for doc in collection.find(
                    {
                        "delivery_date": {
                            "$gte": expected_start,
                            "$lte": expected_end,
                        }
                    },
                    {"_id": 0, "delivery_date": 1},
                )
            }
        )
        missing_dates = [
            delivery_date
            for delivery_date in delivery_dates
            if delivery_date not in stored_dates
        ]
        print(
            f"[db] {dataset} coverage in Mongo: "
            f"expected={expected_start}..{expected_end}, "
            f"stored_dates={stored_dates or []}, "
            f"missing_dates={missing_dates or []}"
        )

    summarize(constrained_collection, "constrained", constrained_records)
    summarize(unconstrained_collection, "unconstrained", unconstrained_records)


def scrape_area_results_for_search_date(
    driver,
    search_date: date,
    chunk_start: date,
    chunk_end: date,
    category_label: str,
    category_field_map: dict[str, str],
    timeout: int,
    reopen_schedule: bool,
    configure_static_fields: bool,
) -> dict:
    formatted_date = format_delivery_day(search_date)
    if reopen_schedule:
        click_select_schedule(driver, timeout=timeout)

    select_category(driver, category_label, timeout=timeout)
    if configure_static_fields:
        set_input_by_label(driver, "Delivery Day", formatted_date, timeout=timeout)

    click_order = [
        ("constrained", False),
        ("unconstrained", True),
    ]

    tables: dict[str, dict] = {}
    records: dict[str, list[dict]] = {}

    for step_index, (dataset_name, toggle_state) in enumerate(click_order):
        if step_index > 0:
            click_select_schedule(driver, timeout=timeout)
        set_constrained_toggle(driver, toggle_state, timeout=timeout)
        click_search(driver, timeout=timeout)
        table = extract_hourly_table(driver, dataset_name, timeout=timeout)
        validate_returned_dates_for_chunk(
            dataset_name,
            table["returned_dates"],
            chunk_start,
            chunk_end,
        )
        tables[dataset_name] = table
        records[dataset_name] = enrich_hourly_records(
            dataset_name,
            table["hourly_records"],
            search_date,
            table["returned_dates"],
            category_label,
            category_field_map[dataset_name],
        )

    return {
        "search_date": search_date.isoformat(),
        "formatted_search_date": formatted_date,
        "category": category_label,
        "returned_dates": {
            "unconstrained": tables["unconstrained"]["columns"],
            "constrained": tables["constrained"]["columns"],
        },
        "unconstrained_records": records["unconstrained"],
        "constrained_records": records["constrained"],
    }


def run(
    start_date: date,
    end_date: date,
    timeout: int,
    headless: bool,
    observe_seconds: int,
    scrape_scope: str,
) -> dict:
    run_started_at = time.perf_counter()
    if scrape_scope not in SCRAPE_SCOPE_CHOICES:
        raise ValueError(
            f"Invalid scrape_scope '{scrape_scope}'. "
            f"Choose one of: {', '.join(SCRAPE_SCOPE_CHOICES)}"
        )
    username, password, mongodb_url, database_name = load_config()
    start_date, end_date = normalize_requested_range(start_date, end_date)
    chunks = build_search_chunks(start_date, end_date)
    print(
        f"[run] Starting area results scraper for start_date={start_date.isoformat()}, "
        f"end_date={end_date.isoformat()}, chunks={len(chunks)}, timeout={timeout}, "
        f"headless={headless}, observe_seconds={observe_seconds}, scope={scrape_scope}"
    )
    for index, chunk in enumerate(chunks, start=1):
        print(
            f"[run] Chunk {index}/{len(chunks)}: "
            f"{chunk['chunk_start'].isoformat()} -> {chunk['chunk_end'].isoformat()} "
            f"(search {chunk['search_date'].isoformat()})"
        )

    mongo_client, db = open_mongo_connection(mongodb_url, database_name)
    driver = None
    storage_executor = ThreadPoolExecutor(max_workers=1)
    run_succeeded = False
    try:
        driver = create_driver(headless=headless)
        login(driver, username, password, timeout=timeout)
        print(f"[2/7] Opening {AREA_RESULTS_URL}")
        open_area_results_page(driver, username, password, timeout=timeout)

        chunk_results = []
        storage_jobs = []
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
            for category_index, category_variant in enumerate(SCRAPE_VARIANTS[scrape_scope]):
                category_result = scrape_area_results_for_search_date(
                    driver=driver,
                    search_date=chunk["search_date"],
                    chunk_start=chunk["chunk_start"],
                    chunk_end=chunk["chunk_end"],
                    category_label=category_variant["category"],
                    category_field_map=category_variant["fields"],
                    timeout=timeout,
                    reopen_schedule=index > 1 or category_index > 0,
                    configure_static_fields=(category_index == 0),
                )
                storage_future = storage_executor.submit(
                    store_results,
                    db,
                    category_result["unconstrained_records"],
                    category_result["constrained_records"],
                )
                chunk_results.append(category_result)
                storage_jobs.append((category_result, storage_future))

        for chunk_result, storage_future in storage_jobs:
            storage_result = storage_future.result()
            chunk_result["storage"] = storage_result

            total_unconstrained_records += len(chunk_result["unconstrained_records"])
            total_constrained_records += len(chunk_result["constrained_records"])
            total_imported_unconstrained += storage_result["unconstrained"]["imported"]
            total_imported_constrained += storage_result["constrained"]["imported"]
            total_skipped_unconstrained += storage_result["unconstrained"]["skipped_existing"]
            total_skipped_constrained += storage_result["constrained"]["skipped_existing"]

        coverage_summary = summarize_delivery_date_coverage(
            chunk_results,
            start_date,
            end_date,
        )
        elapsed_seconds = time.perf_counter() - run_started_at
        result = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 2),
            "scrape_scope": scrape_scope,
            "categories_scraped": [variant["category"] for variant in SCRAPE_VARIANTS[scrape_scope]],
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
            "delivery_date_coverage": coverage_summary,
            "results": chunk_results,
        }
        print(
            "[7/7] Range storage summary: "
            f"unconstrained imported={total_imported_unconstrained}, "
            f"constrained imported={total_imported_constrained}, "
            f"unconstrained skipped={total_skipped_unconstrained}, "
            f"constrained skipped={total_skipped_constrained}, "
            f"elapsed={elapsed_seconds:.2f}s"
        )
        print(
            "[7/7] Delivery-date coverage: "
            f"both={coverage_summary['counts']['successful_both_dates']}, "
            f"constrained_only={coverage_summary['counts']['constrained_only_dates']}, "
            f"unconstrained_only={coverage_summary['counts']['unconstrained_only_dates']}, "
            f"no_data={coverage_summary['counts']['no_data_dates']}, "
            f"elapsed={elapsed_seconds:.2f}s"
        )
        run_succeeded = True
        return result
    finally:
        storage_executor.shutdown(wait=True)
        if not run_succeeded and observe_seconds > 0:
            print(f"Keeping browser open for {observe_seconds} seconds")
            time.sleep(observe_seconds)
        if driver is not None:
            driver.quit()
            print("[driver] Firefox closed")
        close_mongo_connection(mongo_client)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone SAPP area results test scraper for DAM prices and volumes."
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
    parser.add_argument(
        "--scrape-scope",
        choices=SCRAPE_SCOPE_CHOICES,
        default="prices",
        help=(
            "Choose which DAM categories to scrape: prices, volumes, or all "
            "(prices + volumes)."
        ),
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
        scrape_scope=args.scrape_scope,
    )


if __name__ == "__main__":
    main()

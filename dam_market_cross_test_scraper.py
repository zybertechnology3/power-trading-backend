import argparse
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.firefox import GeckoDriverManager

BASE_URL = "https://trading.sappmtp.com"
LOGIN_URL = f"{BASE_URL}/account/login?returnUrl=%2F"
MARKET_CROSS_URL = f"{BASE_URL}/amt/unconstrained-market-cross-X-dam"


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


def load_config() -> tuple[str, str]:
    env_path = Path(__file__).resolve().parent / ".env"
    print(f"[config] Loading configuration from {env_path}")
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print("[config] .env file found and loaded")
    else:
        print("[config] .env file not found next to script; using process environment")

    username = os.getenv("SAPP_USERNAME")
    password = os.getenv("SAPP_PASSWORD")
    if not username or not password:
        raise ValueError("Set SAPP_USERNAME and SAPP_PASSWORD in .env.")

    return username, password


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


def create_driver(headless: Optional[bool] = None):
    configured_headless = os.getenv("SAPP_HEADLESS")
    if configured_headless is None:
        configured_headless = os.getenv("HEADLESS")
    headless = (
        _default_headless_mode()
        if configured_headless is not None
        else (True if headless is None else headless)
    )
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


def open_market_cross_page(
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

    debug_log(debug, f"Opening market-cross page: {MARKET_CROSS_URL}")
    driver.get(MARKET_CROSS_URL)
    wait_for_page_settle(driver, timeout=timeout, extra_delay=1.25)
    debug_log(debug, f"Market-cross page loaded: current_url={driver.current_url}")

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
        debug_log(debug, "Re-opening market-cross page after login redirect")
        driver.get(MARKET_CROSS_URL)
        wait_for_page_settle(driver, timeout=timeout, extra_delay=1.25)
        debug_log(debug, f"Market-cross page reloaded: current_url={driver.current_url}")


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


def set_date_via_calendar(driver, label: str, value: str, timeout: int = 20) -> None:
    print(f"[2/4] Calendar fallback for {label}: {value}")
    # Keep this minimal for now; the direct input path is preferred.
    return None


def set_input_by_label(driver, label: str, value: str, timeout: int = 20) -> None:
    print(f"[2/4] {label}: {value}")
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
    debug_log(debug, "Search button discovered and scrolled into view")
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


def _scan_market_cross_context(driver):
    return driver.execute_script(
        r"""
        const normalize = (value) => (value || "").replace(/\s+/g, " ").trim().toLowerCase();

        const describeDocument = (doc) => {
            const highcharts = doc.defaultView && typeof doc.defaultView.Highcharts !== "undefined"
                ? doc.defaultView.Highcharts
                : null;
            const charts = highcharts && Array.isArray(highcharts.charts)
                ? highcharts.charts.filter(Boolean)
                : [];
            const legendNames = Array.from(doc.querySelectorAll("g.highcharts-legend-item text, g.highcharts-legend-item tspan"))
                .map((node) => normalize(node.textContent))
                .filter(Boolean);
            const chartTitles = charts
                .map((chart) => normalize(chart?.title?.textStr || chart?.title?.text || ""))
                .filter(Boolean);
            return {
                ready_state: doc.readyState,
                has_highcharts: Boolean(highcharts),
                highcharts_chart_count: charts.length,
                svg_root_count: doc.querySelectorAll("svg.highcharts-root").length,
                chart_titles: Array.from(new Set(chartTitles)),
                legend_names: Array.from(new Set(legendNames)),
                iframe_count: doc.querySelectorAll("iframe").length,
            };
        };

        const walk = (win, framePath, depth) => {
            if (depth > 8) {
                return {
                    found: false,
                    frame_path: null,
                    ...describeDocument(win.document),
                };
            }

            try {
                const doc = win.document;
                if (doc.querySelector("svg.highcharts-root")) {
                    return {
                        found: true,
                        frame_path: framePath,
                        ...describeDocument(doc),
                    };
                }

                const frames = Array.from(doc.querySelectorAll("iframe"));
                for (let index = 0; index < frames.length; index += 1) {
                    try {
                        const childWindow = frames[index].contentWindow;
                        if (!childWindow) continue;
                        const found = walk(childWindow, framePath.concat(index), depth + 1);
                        if (found && found.found) {
                            return found;
                        }
                    } catch (error) {
                        // Ignore cross-origin / detached frame errors and keep scanning.
                    }
                }

                return {
                    found: false,
                    frame_path: null,
                    ...describeDocument(doc),
                };
            } catch (error) {
                return {
                    found: false,
                    frame_path: null,
                    error: String(error),
                };
            }
        };

        return walk(window, [], 0);
        """
    )
    debug_log(debug, f"Chart-object extraction candidate: {result}")
    return result


def _switch_to_chart_context(driver, timeout: int = 20, debug: bool = False) -> dict:
    original_window = driver.current_window_handle
    original_handles = list(driver.window_handles)
    handles = list(reversed(original_handles))
    debug_log(debug, f"Scanning {len(original_handles)} window(s) for the chart")

    for handle in handles:
        try:
            driver.switch_to.window(handle)
            driver.switch_to.default_content()
            context = _scan_market_cross_context(driver)
            debug_log(
                debug,
                "Window scan "
                f"handle={handle} found={context.get('found')} "
                f"ready_state={context.get('ready_state')} "
                f"svg_roots={context.get('svg_root_count')} "
                f"charts={context.get('highcharts_chart_count')} "
                f"titles={context.get('chart_titles')} "
                f"legends={context.get('legend_names')} "
                f"frames={context.get('iframe_count')} "
                f"frame_path={context.get('frame_path')}",
            )
            if context and context.get("found"):
                for frame_index in context.get("frame_path") or []:
                    frames = driver.find_elements("tag name", "iframe")
                    if frame_index >= len(frames):
                        raise RuntimeError(
                            f"Frame path became invalid while switching to chart context: {context.get('frame_path')}"
                        )
                    driver.switch_to.frame(frames[frame_index])
                context["window_handle"] = handle
                context["window_count"] = len(original_handles)
                return context
        except Exception:
            continue

    driver.switch_to.window(original_window)
    driver.switch_to.default_content()
    raise RuntimeError(
        "Could not find the rendered Highcharts chart in any window or iframe context."
    )


def _collect_market_cross_diagnostics(driver, debug: bool = False) -> dict:
    try:
        diagnostics = driver.execute_script(
            r"""
            const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
            const highcharts = window.Highcharts || null;
            const charts = highcharts && Array.isArray(highcharts.charts)
                ? highcharts.charts.filter(Boolean)
                : [];
            return {
                current_url: window.location.href,
                ready_state: document.readyState,
                has_highcharts: Boolean(highcharts),
                highcharts_chart_count: charts.length,
                svg_root_count: document.querySelectorAll("svg.highcharts-root").length,
                chart_titles: charts.map((chart) => normalize(chart?.title?.textStr || chart?.title?.text || "")).filter(Boolean),
                legend_names: Array.from(document.querySelectorAll("g.highcharts-legend-item text, g.highcharts-legend-item tspan"))
                    .map((node) => normalize(node.textContent))
                    .filter(Boolean),
                iframe_count: document.querySelectorAll("iframe").length,
                tooltip_text: normalize(
                    document.querySelector("g.highcharts-tooltip")?.textContent
                    || document.querySelector(".highcharts-tooltip")?.textContent
                    || ""
                ),
            };
            """
        )
        debug_log(debug, f"Chart diagnostics: {diagnostics}")
        return diagnostics
    except Exception as exc:
        diagnostics = {
            "error": str(exc),
            "current_url": driver.current_url,
            "ready_state": None,
        }
        debug_log(debug, f"Chart diagnostics failed: {diagnostics}")
        return diagnostics


def _collect_svg_market_cross_debug(driver, debug: bool = False) -> dict:
    try:
        payload = driver.execute_script(
            r"""
            const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
            const svg = document.querySelector("svg.highcharts-root");
            if (!svg) {
                return {
                    found: false,
                    reason: "svg.highcharts-root not found",
                };
            }
            const seriesGroups = Array.from(svg.querySelectorAll("g.highcharts-series")).map((group, index) => {
                const points = Array.from(group.querySelectorAll("path.highcharts-point, circle.highcharts-point"));
                return {
                    index,
                    class_name: normalize(group.getAttribute("class")),
                    aria_label: normalize(group.getAttribute("aria-label")),
                    data_label: normalize(group.getAttribute("data-label")),
                    point_count: points.length,
                    point_classes: points.slice(0, 5).map((point) => normalize(point.getAttribute("class"))),
                    point_titles: points.slice(0, 5).map((point) => normalize(point.getAttribute("title"))),
                };
            });
            const legendItems = Array.from(svg.querySelectorAll("g.highcharts-legend-item")).map((item, index) => ({
                index,
                text: normalize(item.textContent),
                class_name: normalize(item.getAttribute("class")),
            }));
            return {
                found: true,
                svg_class_name: normalize(svg.getAttribute("class")),
                series_group_count: seriesGroups.length,
                legend_item_count: legendItems.length,
                series_groups: seriesGroups,
                legend_items: legendItems,
            };
            """
        )
        debug_log(debug, f"SVG series debug: {payload}")
        return payload
    except Exception as exc:
        payload = {
            "found": False,
            "error": str(exc),
        }
        debug_log(debug, f"SVG series debug failed: {payload}")
        return payload


def _extract_from_chart_object(driver, debug: bool = False) -> Optional[dict]:
    result = driver.execute_script(
        r"""
        const normalize = (value) => (value || "").replace(/\s+/g, " ").trim().toLowerCase();
        const highcharts = window.Highcharts || null;
        const charts = highcharts && Array.isArray(highcharts.charts)
            ? highcharts.charts.filter(Boolean)
            : [];

        const toPoint = (candidate) => {
            if (!candidate) return null;
            if (Number.isFinite(candidate.x) && Number.isFinite(candidate.y)) {
                return { x: candidate.x, y: candidate.y };
            }
            if (Array.isArray(candidate) && candidate.length >= 2) {
                const x = Number(candidate[0]);
                const y = Number(candidate[1]);
                if (Number.isFinite(x) && Number.isFinite(y)) {
                    return { x, y };
                }
            }
            if (typeof candidate === "object") {
                const x = Number(candidate.x ?? candidate[0]);
                const y = Number(candidate.y ?? candidate[1]);
                if (Number.isFinite(x) && Number.isFinite(y)) {
                    return { x, y };
                }
            }
            return null;
        };

        const getPoints = (series) => {
            if (!series) return [];
            const buckets = []
                .concat(series.points || [])
                .concat(series.data || [])
                .concat(series.userOptions?.data || [])
                .concat(series.options?.data || []);
            const points = [];
            const seen = new Set();
            for (const candidate of buckets) {
                const point = toPoint(candidate);
                if (!point) continue;
                const key = `${point.x}:${point.y}`;
                if (seen.has(key)) continue;
                seen.add(key);
                points.push(point);
            }
            return points;
        };

        const pickMaxX = (points) => {
            if (!points.length) return null;
            return points.slice().sort((a, b) => a.x - b.x)[points.length - 1];
        };

        const chart = charts.find((candidate) => {
            const title = normalize(candidate?.title?.textStr || candidate?.title?.text || "");
            const seriesNames = (candidate.series || []).map((series) => normalize(series.name));
            return title.includes("price/quantity curves")
                || seriesNames.some((name) => name === "buy")
                || seriesNames.some((name) => name === "sell")
                || seriesNames.some((name) => name === "market cross");
        }) || null;

        if (!chart) {
            return null;
        }

        const seriesByExactName = (name) => (chart.series || []).find(
            (series) => normalize(series.name) === name
        ) || null;
        const seriesByIncludes = (needle) => (chart.series || []).find(
            (series) => normalize(series.name).includes(needle)
        ) || null;

        const buySeries = seriesByExactName("buy") || seriesByIncludes("buy");
        const sellSeries = seriesByExactName("sell") || seriesByIncludes("sell");
        const marketCrossSeries = seriesByExactName("market cross")
            || seriesByIncludes("market cross")
            || seriesByIncludes("cross")
            || (chart.series || []).find((series) => series.type === "scatter")
            || null;

        const buyPoints = getPoints(buySeries);
        const sellPoints = getPoints(sellSeries);
        const marketCrossPoints = getPoints(marketCrossSeries);

        const zeroBuyPoints = buyPoints.filter((point) => Math.abs(point.y) < 1e-9);
        const chosenBuyPoint = pickMaxX(zeroBuyPoints.length ? zeroBuyPoints : buyPoints);
        const chosenSellPoint = pickMaxX(sellPoints);
        const chosenMarketCrossPoint = pickMaxX(marketCrossPoints);

        return {
            method: "chart-object",
            chart_title: chart?.title?.textStr || chart?.title?.text || null,
            chart_titles: charts.map((candidate) => candidate?.title?.textStr || candidate?.title?.text || null).filter(Boolean),
            series_names: (chart.series || []).map((series) => series.name).filter(Boolean),
            series_order: (chart.series || []).map((series, index) => ({
                index,
                name: series.name || null,
                type: series.type || null,
                visible: series.visible !== false,
                points_count: getPoints(series).length,
            })),
            buy_points_count: buyPoints.length,
            buy_zero_points_count: zeroBuyPoints.length,
            sell_points_count: sellPoints.length,
            market_cross_points_count: marketCrossPoints.length,
            buy: chosenBuyPoint ? {
                quantity_mw: chosenBuyPoint.x,
                buy_value: chosenBuyPoint.y,
            } : null,
            sell: chosenSellPoint ? {
                quantity_mw: chosenSellPoint.x,
                sell_value: chosenSellPoint.y,
            } : null,
            market_cross: chosenMarketCrossPoint ? {
                quantity_mw: chosenMarketCrossPoint.x,
                price_usd_per_mwh: chosenMarketCrossPoint.y,
            } : null,
        };
        """
    )


def _parse_tooltip_text(text: str) -> Optional[dict]:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return None
    match = re.search(
        r"(?P<quantity>-?[\d,.]+)\s+Buy:\s*(?P<value>-?[\d,.]+)",
        cleaned,
        re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "quantity_mw": float(match.group("quantity").replace(",", "")),
        "buy_value": float(match.group("value").replace(",", "")),
        "tooltip_text": cleaned,
    }


def _extract_buy_from_dom_tooltip(driver, timeout: int = 20, debug: bool = False) -> dict:
    legend_data = driver.execute_script(
        r"""
        const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
        const legendItems = Array.from(document.querySelectorAll("g.highcharts-legend-item"));
        const legend = legendItems.map((item, index) => {
            const text = normalize(item.textContent);
            const className = item.getAttribute("class") || "";
            const seriesMatch = className.match(/highcharts-series-(\d+)/);
            return {
                index,
                text,
                className,
                series_index: seriesMatch ? Number(seriesMatch[1]) : null,
            };
        });
        const buy = legend.find((item) => item.text.toLowerCase() === "buy") || null;
        return { legend, buy };
        """
    )

    buy_series_index = None
    if legend_data and legend_data.get("buy"):
        buy_series_index = legend_data["buy"].get("series_index")

    if buy_series_index is None:
        raise RuntimeError(
            f"Buy legend item not found for tooltip fallback. Legend items: "
            f"{legend_data.get('legend') if legend_data else []}"
        )
    debug_log(debug, f"Tooltip fallback using Buy series index: {buy_series_index}")
    if debug:
        debug_log(debug, f"Tooltip fallback legend data: {legend_data}")
        debug_log(debug, f"Tooltip fallback SVG series info: {_collect_svg_market_cross_debug(driver, debug=debug)}")

    point_selector = (
        f"g.highcharts-series.highcharts-series-{buy_series_index} path.highcharts-point, "
        f"g.highcharts-series.highcharts-series-{buy_series_index} circle.highcharts-point"
    )
    points = driver.find_elements("css selector", point_selector)
    search_scope = "buy-series"
    if not points:
        search_scope = "all-points"
        point_selector = "svg.highcharts-root path.highcharts-point, svg.highcharts-root circle.highcharts-point"
        points = driver.find_elements("css selector", point_selector)
    if not points:
        raise RuntimeError(
            f"No chart points found for tooltip fallback. Tried selectors: buy={point_selector!r}"
        )
    debug_log(
        debug,
        f"Tooltip fallback found {len(points)} point(s) using scope={search_scope} selector={point_selector}",
    )

    last_tooltip_text = ""
    for point_index, point in enumerate(reversed(points), start=1):
        try:
            ActionChains(driver).move_to_element(point).pause(0.05).perform()
        except WebDriverException:
            try:
                driver.execute_script(
                    r"""
                    const element = arguments[0];
                    const rect = element.getBoundingClientRect();
                    const clientX = rect.left + rect.width / 2;
                    const clientY = rect.top + rect.height / 2;
                    for (const type of ["mouseover", "mousemove", "mouseenter"]) {
                        element.dispatchEvent(new MouseEvent(type, {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                            clientX,
                            clientY,
                        }));
                    }
                    """,
                    point,
                )
            except WebDriverException:
                debug_log(debug, f"Tooltip hover attempt {point_index} failed to dispatch events")
                continue

        deadline = time.time() + 2.0
        while time.time() < deadline:
            tooltip_text = driver.execute_script(
                r"""
                const tooltip = document.querySelector("g.highcharts-tooltip");
                const container = document.querySelector(".highcharts-tooltip");
                return (tooltip && (tooltip.textContent || tooltip.innerText || ""))
                    || (container && (container.textContent || container.innerText || ""))
                    || "";
                """
            )
            last_tooltip_text = tooltip_text or last_tooltip_text
            debug_log(debug, f"Tooltip hover attempt {point_index}: text={tooltip_text!r}")
            parsed = _parse_tooltip_text(tooltip_text)
            if parsed and abs(parsed["buy_value"]) < 1e-9:
                parsed["method"] = "dom-tooltip"
                parsed["buy_series_index"] = buy_series_index
                parsed["search_scope"] = search_scope
                parsed["last_tooltip_text"] = parsed["tooltip_text"]
                debug_log(debug, f"Tooltip fallback matched zero-value Buy point: {parsed}")
                return parsed
            time.sleep(0.1)

    raise RuntimeError(
        f"Could not activate a Buy tooltip with zero value. Last tooltip text: {last_tooltip_text!r}"
    )


def extract_market_cross_values(driver, timeout: int = 20, debug: bool = False) -> dict:
    original_window = driver.current_window_handle
    original_handles = list(driver.window_handles)
    context_info = None

    try:
        context_info = _switch_to_chart_context(driver, timeout=timeout, debug=debug)
        debug_log(debug, f"Chart context selected: {context_info}")
    except Exception as exc:
        diagnostics = _collect_market_cross_diagnostics(driver, debug=debug)
        if len(original_handles) > 1:
            diagnostics["window_handles"] = original_handles
        else:
            diagnostics["window_handles"] = [original_window]
        raise RuntimeError(
            "Could not find the rendered Highcharts market-cross chart on the page. "
            f"Diagnostics={diagnostics}"
        ) from exc

    try:
        result = _extract_from_chart_object(driver, debug=debug)
        debug_log(debug, f"Chart-object raw result: {result}")
        if result and result.get("buy") and result.get("sell") and result.get("market_cross"):
            extracted = {
                "power_offered_on_dam_mw": result["sell"]["quantity_mw"],
                "power_requested_on_dam_mw": result["buy"]["quantity_mw"],
                "market_clearing_price_usd_per_mwh": round(
                    float(result["market_cross"]["price_usd_per_mwh"]), 2
                ),
                "extraction_method": result["method"],
                "chart_title": result["chart_title"],
                "chart_titles": result["chart_titles"],
                "series_names": result["series_names"],
                "buy_points_count": result["buy_points_count"],
                "buy_zero_points_count": result["buy_zero_points_count"],
                "sell_points_count": result["sell_points_count"],
                "market_cross_points_count": result["market_cross_points_count"],
            }
            print("[4/4] Extracted values")
            print(f"  Power offered on DAM: {extracted['power_offered_on_dam_mw']} MW")
            print(f"  Power requested on DAM: {extracted['power_requested_on_dam_mw']} MW")
            print(
                "  Market clearing price: "
                f"{extracted['market_clearing_price_usd_per_mwh']} USD/MWh"
            )
            return extracted

        debug_log(debug, "Chart-object path incomplete; falling back to Buy tooltip hover")
        buy_result = _extract_buy_from_dom_tooltip(driver, timeout=timeout, debug=debug)
        extracted = {
            "power_offered_on_dam_mw": None,
            "power_requested_on_dam_mw": buy_result["quantity_mw"],
            "market_clearing_price_usd_per_mwh": None,
            "extraction_method": buy_result["method"],
            "chart_title": None,
            "chart_titles": context_info.get("chart_titles") if context_info else [],
            "series_names": context_info.get("legend_names") if context_info else [],
            "buy_points_count": None,
            "buy_zero_points_count": None,
            "sell_points_count": None,
            "market_cross_points_count": None,
            "buy_value": buy_result["buy_value"],
            "last_tooltip_text": buy_result["last_tooltip_text"],
        }
        print("[4/4] Extracted values")
        print(f"  Power requested on DAM: {extracted['power_requested_on_dam_mw']} MW")
        print(f"  Buy value: {extracted['buy_value']}")
        return extracted
    except Exception as exc:
        diagnostics = _collect_market_cross_diagnostics(driver, debug=debug)
        diagnostics["svg_debug"] = _collect_svg_market_cross_debug(driver, debug=debug)
        diagnostics["window_handles"] = original_handles
        diagnostics["context_info"] = context_info
        raise RuntimeError(
            "Failed to extract market-cross values. "
            f"Diagnostics={diagnostics}"
        ) from exc
    finally:
        try:
            driver.switch_to.window(original_window)
            driver.switch_to.default_content()
        except Exception:
            pass


def run(
    delivery_date: date,
    product: str,
    timeout: int,
    headless: bool,
    observe_seconds: int,
    debug: bool = False,
) -> dict:
    username, password = load_config()
    print(
        f"[run] Starting DAM market-cross scraper for delivery_date={delivery_date.isoformat()}, "
        f"product={product}, timeout={timeout}, headless={headless}, observe_seconds={observe_seconds}, debug={debug}"
    )

    driver = None
    run_succeeded = False
    try:
        driver = create_driver(headless=headless)
        login(driver, username, password, timeout=timeout)
        print(f"[2/4] Opening {MARKET_CROSS_URL}")
        open_market_cross_page(driver, username, password, timeout=timeout, debug=debug)

        set_input_by_label(driver, "Delivery Day", format_delivery_day(delivery_date), timeout=timeout)
        set_input_by_label(driver, "Product", product, timeout=timeout)
        click_search(driver, timeout=timeout, debug=debug)
        extracted = extract_market_cross_values(driver, timeout=timeout, debug=debug)

        result = {
            "delivery_date": delivery_date.isoformat(),
            "product": product,
            **extracted,
            "page_url": driver.current_url,
        }
        print(f"[done] delivery_date={result['delivery_date']} product={result['product']}")
        run_succeeded = True
        return result
    finally:
        if run_succeeded:
            print("[hold] Keeping browser open for 60s so you can verify the values")
            time.sleep(60)
        elif observe_seconds > 0:
            print(f"🛑 keeping browser open for {observe_seconds}s")
            time.sleep(observe_seconds)
        if driver is not None:
            driver.quit()
            print("[driver] Firefox closed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone DAM market cross scraper for delivery day and product selection."
    )
    parser.add_argument(
        "--delivery-date",
        help="Delivery date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--product",
        default="00-01",
        help="Product period to search for, e.g. 00-01.",
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
        help="Seconds to keep the browser open before closing on failure.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print live debug traces for page discovery, chart selection, and tooltip extraction.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    delivery_date = (
        date.fromisoformat(args.delivery_date)
        if args.delivery_date
        else datetime.now().date()
    )
    result = run(
        delivery_date=delivery_date,
        product=args.product,
        timeout=args.timeout,
        headless=args.headless,
        observe_seconds=args.observe_seconds,
        debug=args.debug,
    )
    print(
        f"[done] power_offered={result['power_offered_on_dam_mw']} MW "
        f"power_requested={result['power_requested_on_dam_mw']} MW "
        f"market_clearing_price={result['market_clearing_price_usd_per_mwh']} USD/MWh"
    )


if __name__ == "__main__":
    main()

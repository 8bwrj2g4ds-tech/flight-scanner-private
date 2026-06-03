import re
import json
import os
import csv
import requests
import subprocess

from dotenv import load_dotenv

load_dotenv()

from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# =========================
# SETTINGS
# =========================

ORIGINS = ["MEX"]
DESTINATIONS = ["NRT", "CDG", "MAD", "AMS", "EDI", "DUB", "KEF"]

SCAN_FROM_DAYS = 180
SCAN_TO_DAYS = 210

MIN_TRIP_DAYS = 10
MAX_TRIP_DAYS = 12

PASSENGERS = 1

CABIN_CLASSES = ["economy", "business"]

ALLOWED_STOPS = ["Nonstop", "1 stop", "2 stops", "Unknown"]

MAX_PRICE_BY_CABIN = {
    "economy": 18000,
    "business": 70000
}

MIN_VALID_PRICE_MXN = 8000
FALLBACK_USD_TO_MXN = 20
HEADLESS_MODE = True

PRICE_HISTORY_FILE = "best_price_history.json"
CSV_FILE = "flight_results.csv"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# =========================
# HELPERS
# =========================

def generate_trips():
    trips = []

    today = datetime.today()
    start_date = today + timedelta(days=SCAN_FROM_DAYS)
    end_date = today + timedelta(days=SCAN_TO_DAYS)

    departure_date = start_date

    while departure_date <= end_date:
        for trip_length in range(MIN_TRIP_DAYS, MAX_TRIP_DAYS + 1):
            return_date = departure_date + timedelta(days=trip_length)

            if return_date <= end_date:
                trips.append({
                    "departure": departure_date.strftime("%Y-%m-%d"),
                    "return": return_date.strftime("%Y-%m-%d"),
                    "trip_length": trip_length
                })

        departure_date += timedelta(days=1)

    return trips


def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Skipping alert.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    response = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    })

    print("Telegram status:", response.status_code)
    print("Telegram response:", response.text)


def load_price_history():
    if not os.path.exists(PRICE_HISTORY_FILE):
        return {}

    with open(PRICE_HISTORY_FILE, "r") as file:
        return json.load(file)


def save_price_history(history):
    with open(PRICE_HISTORY_FILE, "w") as file:
        json.dump(history, file, indent=4)


def build_google_flights_url(origin, destination, departure_date, return_date, cabin_class):
    return (
        "https://www.google.com/travel/flights?"
        f"q=Flights%20from%20{origin}%20to%20{destination}%20"
        f"on%20{departure_date}%20returning%20{return_date}%20"
        f"{cabin_class}%20class%20{PASSENGERS}%20passenger"
    )


def get_usd_to_mxn_rate():
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        response = requests.get(url, timeout=10)
        data = response.json()

        rate = data["rates"]["MXN"]
        print(f"Live USD to MXN rate: {rate}")
        return rate

    except Exception as e:
        print("Could not fetch live FX rate. Using fallback.", e)
        return FALLBACK_USD_TO_MXN


def extract_flight_blocks(all_text):
    usd_to_mxn = get_usd_to_mxn_rate()

    lines = [line.strip() for line in all_text.splitlines() if line.strip()]
    blocks = []

    for i, line in enumerate(lines):
        price_match = re.search(r"(MX\$|\$)\s?([\d,]+)", line)

        if price_match:
            currency = price_match.group(1)
            price = int(price_match.group(2).replace(",", ""))

            if currency == "$":
                price = int(price * usd_to_mxn)

            if price < MIN_VALID_PRICE_MXN:
                continue

            nearby = lines[max(0, i - 12): i + 6]
            block_text = "\n".join(nearby)

            stops = "Unknown"
            if re.search(r"\b(nonstop|direct)\b", block_text, re.IGNORECASE):
                stops = "Nonstop"
            elif re.search(r"\b1\s+stop\b|\b1\s+layover\b|\b1\s+connection\b", block_text, re.IGNORECASE):
                stops = "1 stop"
            elif re.search(r"\b2\s+stops\b|\b2\s+layovers\b|\b2\s+connections\b", block_text, re.IGNORECASE):
                stops = "2 stops"

            duration = "Unknown"
            duration_match = re.search(r"(\d+ hr(?: \d+ min)?|\d+ min)", block_text)
            if duration_match:
                duration = duration_match.group(1)

            airline = "Unknown"
            possible_airlines = [
                "Aeromexico", "Air France", "KLM", "Lufthansa",
                "British Airways", "Iberia", "United", "American",
                "Delta", "ANA", "JAL", "Emirates", "Qatar",
                "Turkish Airlines", "Air Canada", "Air Europa",
                "Volaris", "Viva Aerobus", "Avianca", "WestJet", "Aer Lingus"
            ]

            for name in possible_airlines:
                if name in block_text:
                    airline = name
                    break

            blocks.append({
                "price": price,
                "airline": airline,
                "stops": stops,
                "duration": duration,
                "raw_block": block_text
            })

    return blocks


def telegram_deal_score(deal, historical_context=None):
    price = deal["lowest_price"]
    cabin = deal["cabin"].lower()
    stops = deal["stops"].lower()
    airline = deal["airline"]
    duration = str(deal["duration"])

    score = 100

    if historical_context:
        delta = historical_context["delta_vs_average"]

        if price <= historical_context["lowest_ever"] * 1.03:
            score += 35

        if delta <= -20:
            score += 30
        elif delta <= -10:
            score += 20
        elif delta <= 0:
            score += 10
        elif delta >= 20:
            score -= 25
        elif delta >= 10:
            score -= 15

        if historical_context["confidence"] == "High":
            score += 10
        elif historical_context["confidence"] == "Medium":
            score += 5
    else:
        if cabin == "business":
            if price <= 50000:
                score += 30
            elif price <= 60000:
                score += 20
            elif price <= 70000:
                score += 10
        else:
            if price <= 15000:
                score += 25
            elif price <= 18000:
                score += 15
            elif price <= 22000:
                score += 5

    if cabin == "business":
        score += 20
    else:
        score += 5

    if stops == "nonstop":
        score += 25
    elif "1 stop" in stops:
        score += 10
    elif "2 stops" in stops:
        score -= 10
    else:
        score -= 5

    premium_airlines = [
        "Air France", "KLM", "Lufthansa", "British Airways",
        "Iberia", "ANA", "JAL", "Emirates", "Qatar",
        "Turkish Airlines", "Air Canada", "Delta"
    ]

    solid_airlines = [
        "Aeromexico", "United", "American", "Air Europa", "Avianca"
    ]

    if airline in premium_airlines:
        score += 10
    elif airline in solid_airlines:
        score += 5

    if "hr" in duration:
        try:
            duration_hours = int(duration.split("hr")[0].strip())

            if duration_hours <= 12:
                score += 10
            elif duration_hours <= 16:
                score += 5
            elif duration_hours >= 22:
                score -= 10
        except Exception:
            pass

    return round(score)


def get_telegram_signal(score):
    if score >= 150:
        return "🔥 STRONG BUY"
    if score >= 130:
        return "✅ BUY"
    return None


def get_historical_context(deal):
    if not os.path.exists(CSV_FILE):
        return None

    try:
        prices = []

        with open(CSV_FILE, "r", encoding="utf-8") as file:
            reader = csv.DictReader(file)

            for row in reader:
                if (
                    row.get("origin") == deal["origin"]
                    and row.get("destination") == deal["destination"]
                    and row.get("cabin_class") == deal["cabin"]
                    and row.get("stops") == deal["stops"]
                ):
                    try:
                        prices.append(float(row["lowest_price_mxn"]))
                    except Exception:
                        pass

        if len(prices) < 3:
            return None

        latest_price = deal["lowest_price"]
        average_price = sum(prices) / len(prices)
        lowest_ever = min(prices)
        highest_ever = max(prices)

        delta_vs_average = ((latest_price - average_price) / average_price) * 100
        volatility = highest_ever - lowest_ever

        if len(prices) >= 10 and volatility <= 3000:
            confidence = "High"
        elif len(prices) >= 5:
            confidence = "Medium"
        else:
            confidence = "Low"

        return {
            "average_price": average_price,
            "lowest_ever": lowest_ever,
            "highest_ever": highest_ever,
            "delta_vs_average": delta_vs_average,
            "observations": len(prices),
            "volatility": volatility,
            "confidence": confidence
        }

    except Exception as e:
        print("Could not calculate historical context:", e)
        return None


def get_telegram_reasons(deal, score, historical_context=None):
    reasons = []

    price = deal["lowest_price"]
    cabin = deal["cabin"].lower()
    stops = deal["stops"]
    airline = deal["airline"]
    duration = str(deal["duration"])

    if historical_context:
        if price <= historical_context["lowest_ever"] * 1.03:
            reasons.append("near lowest historical price")

        if historical_context["delta_vs_average"] <= -15:
            reasons.append(
                f"{abs(historical_context['delta_vs_average']):.1f}% below historical average"
            )
        elif historical_context["delta_vs_average"] >= 10:
            reasons.append(
                f"{historical_context['delta_vs_average']:.1f}% above historical average"
            )

    if cabin == "business" and price <= 50000:
        reasons.append("excellent business fare")
    elif cabin == "economy" and price <= 18000:
        reasons.append("strong economy fare")

    if stops == "Nonstop":
        reasons.append("nonstop flight")
    elif stops == "1 stop":
        reasons.append("reasonable 1-stop itinerary")

    if airline in [
        "Air France", "KLM", "Lufthansa", "British Airways",
        "Iberia", "ANA", "JAL", "Emirates", "Qatar",
        "Turkish Airlines", "Air Canada", "Delta"
    ]:
        reasons.append("premium airline")

    if "hr" in duration:
        try:
            duration_hours = int(duration.split("hr")[0].strip())
            if duration_hours <= 16:
                reasons.append("good travel duration")
        except Exception:
            pass

    if score >= 150:
        reasons.append("high AI deal score")

    if historical_context and historical_context["confidence"] in ["High", "Medium"]:
        reasons.append(f"{historical_context['confidence'].lower()} confidence")

    if not reasons:
        reasons.append("good price and route combination")

    return ", ".join(reasons)


def get_deal_score(price, cabin_class):
    if cabin_class == "economy":
        if price <= 15000:
            return "🔥 Excellent economy deal"
        elif price <= 18000:
            return "✅ Good economy deal"
        else:
            return "Average economy price"

    if cabin_class == "business":
        if price <= 55000:
            return "🔥 Excellent business deal"
        elif price <= 70000:
            return "✅ Good business deal"
        else:
            return "Average business price"

    return "Deal found"


def search_single_trip(page, origin, destination, trip, cabin_class):
    departure_date = trip["departure"]
    return_date = trip["return"]

    google_flights_url = build_google_flights_url(
        origin,
        destination,
        departure_date,
        return_date,
        cabin_class
    )

    print("\nOpening Google Flights...")
    print(f"Route: {origin} → {destination}")
    print(f"Dates: {departure_date} to {return_date}")
    print(f"Cabin: {cabin_class}")

    page.goto(google_flights_url)

    try:
        page.wait_for_function(
            """() => document.body.innerText.includes('Search results')
            && /(\\$|MX\\$)\\s?[\\d,]+/.test(document.body.innerText)""",
            timeout=30000
        )
    except Exception:
        print("Timed out waiting for prices. Using available page text.")

    all_text = page.locator("body").inner_text()

    print("PAGE TEXT PREVIEW:")
    print(all_text[:3000])

    flight_blocks = extract_flight_blocks(all_text)

    valid_blocks = [
        block for block in flight_blocks
        if block["stops"] in ALLOWED_STOPS
    ]

    if valid_blocks:
        best = min(valid_blocks, key=lambda item: item["price"])
    else:
        if flight_blocks:
            print("No flights matched stop filter. Using cheapest detected flight as fallback.")
            best = min(flight_blocks, key=lambda item: item["price"])
        else:
            print("No prices found at all.")
            return None

    print(
        f"Best found: MX${best['price']:,} | "
        f"{best['airline']} | {best['stops']} | {best['duration']}"
    )

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "origin": origin,
        "destination": destination,
        "departure": departure_date,
        "return": return_date,
        "trip_length": trip["trip_length"],
        "cabin": cabin_class,
        "passengers": PASSENGERS,
        "lowest_price": best["price"],
        "airline": best["airline"],
        "stops": best["stops"],
        "duration": best["duration"],
        "url": google_flights_url
    }


def save_results_to_csv(results):
    file_exists = os.path.exists(CSV_FILE)

    with open(CSV_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)

        if not file_exists:
            writer.writerow([
                "timestamp",
                "origin",
                "destination",
                "departure_date",
                "return_date",
                "trip_length_days",
                "cabin_class",
                "passengers",
                "lowest_price_mxn",
                "airline",
                "stops",
                "duration",
                "url"
            ])

        for result in results:
            writer.writerow([
                result["timestamp"],
                result["origin"],
                result["destination"],
                result["departure"],
                result["return"],
                result["trip_length"],
                result["cabin"],
                result["passengers"],
                result["lowest_price"],
                result["airline"],
                result["stops"],
                result["duration"],
                result["url"]
            ])


def send_top_3_deals_alert(destination, cabin_class, top_3_deals, history):
    best_deal = top_3_deals[0]
    current_price = best_deal["lowest_price"]

    history_key = f"{best_deal['origin']}-{destination}-{cabin_class}-best-flexible-date-price"
    previous_best = history.get(history_key)

    max_price = MAX_PRICE_BY_CABIN[cabin_class]

    reason = ""
    price_drop_detected = False

    if previous_best is None:
        reason = "First best flexible-date deal found"
    elif current_price < previous_best:
        price_drop_detected = True
        reason = f"Best price dropped from MX${previous_best:,} to MX${current_price:,}"
    else:
        reason = "Best price did not beat previous alert"

    history[history_key] = current_price
    save_price_history(history)

    print(f"\nTop 3 deals for {destination} / {cabin_class}:")
    for index, deal in enumerate(top_3_deals, start=1):
        print(
            f"{index}. {deal['departure']} to {deal['return']} - "
            f"MX${deal['lowest_price']:,} - {deal['airline']} - {deal['stops']}"
        )

    historical_context = get_historical_context(best_deal)
    best_deal_score = telegram_deal_score(best_deal, historical_context)
    signal = get_telegram_signal(best_deal_score)

    should_alert = False

    if current_price <= max_price and signal:
        should_alert = True

    if price_drop_detected and current_price <= max_price:
        should_alert = True

    if historical_context:
        if current_price <= historical_context["lowest_ever"] * 1.03:
            should_alert = True
        if historical_context["delta_vs_average"] <= -15 and current_price <= max_price:
            should_alert = True

    if not should_alert:
        print(
            f"No Telegram alert sent. "
            f"Score: {best_deal_score}. Signal: {signal}. Reason: {reason}"
        )
        return

    message = (
        f"{signal or '📉 PRICE DROP ALERT'}\n\n"
        f"Route: {best_deal['origin']} → {destination}\n"
        f"Cabin: {cabin_class.title()}\n"
        f"Price: MX${current_price:,}\n"
        f"AI Score: {best_deal_score}\n"
        f"Reason: {reason}\n"
        f"Why it matters: {get_telegram_reasons(best_deal, best_deal_score, historical_context)}\n\n"
    )

    if historical_context:
        message += (
            "📊 Historical Context\n"
            f"Average Price: MX${historical_context['average_price']:,.0f}\n"
            f"Lowest Ever: MX${historical_context['lowest_ever']:,.0f}\n"
            f"Highest Ever: MX${historical_context['highest_ever']:,.0f}\n"
            f"Vs Average: {historical_context['delta_vs_average']:.1f}%\n"
            f"Volatility Range: MX${historical_context['volatility']:,.0f}\n"
            f"Confidence: {historical_context['confidence']}\n"
            f"Observations: {historical_context['observations']}\n\n"
        )

    message += "Top 3 flexible-date options:\n\n"

    for index, deal in enumerate(top_3_deals, start=1):
        deal_context = get_historical_context(deal)
        deal_score = telegram_deal_score(deal, deal_context)

        message += (
            f"{index}. {deal['departure']} to {deal['return']}\n"
            f"   Trip Length: {deal['trip_length']} days\n"
            f"   Price: MX${deal['lowest_price']:,}\n"
            f"   Airline: {deal['airline']}\n"
            f"   Stops: {deal['stops']}\n"
            f"   Duration: {deal['duration']}\n"
            f"   AI Score: {deal_score}\n\n"
        )

    message += (
        f"Best Google Flights Link:\n"
        f"{best_deal['url']}"
    )

    send_telegram_alert(message)
    print("AI Telegram deal alert sent.")


def get_buy_now_signal(result):
    context = get_historical_context(result)
    score = telegram_deal_score(result, context)
    signal = get_telegram_signal(score)

    if signal == "🔥 STRONG BUY":
        return True

    price = result["lowest_price"]
    cabin = result["cabin"]
    stops = result["stops"]

    if context:
        if price <= context["lowest_ever"] * 1.03:
            return True
        if context["delta_vs_average"] <= -15:
            return True

    if cabin == "business" and price <= 50000:
        return True

    if cabin == "economy" and price <= 18000:
        return True

    if stops == "Nonstop" and price <= 20000:
        return True

    return False

def get_alert_priority_label(deal, all_results):
    context = get_historical_context(deal)
    score = telegram_deal_score(deal, context)

    price = deal["lowest_price"]
    cabin = deal["cabin"]
    stops = deal["stops"]

    if context and price <= context["lowest_ever"] * 1.03:
        return "📉 New Historical Low"

    if deal == min(all_results, key=lambda r: r["lowest_price"]):
        return "🔥 Deal of the Day"

    if cabin == "business":
        business_results = [r for r in all_results if r["cabin"] == "business"]
        if business_results and deal == min(business_results, key=lambda r: r["lowest_price"]):
            return "💼 Best Business Deal"

    if stops == "Nonstop":
        nonstop_results = [r for r in all_results if r["stops"] == "Nonstop"]
        if nonstop_results and deal == min(nonstop_results, key=lambda r: r["lowest_price"]):
            return "🛫 Best Nonstop Deal"

    if score >= 150:
        return "🔥 Strong Buy"

    if score >= 130:
        return "✅ Good Buy"

    return "👀 Watch"

def send_daily_summary(results):
    if not results:
        print("No results available for daily summary.")
        return

    economy_results = [r for r in results if r["cabin"] == "economy"]
    business_results = [r for r in results if r["cabin"] == "business"]
    buy_now_results = [r for r in results if get_buy_now_signal(r)]

    best_economy = min(economy_results, key=lambda r: r["lowest_price"]) if economy_results else None
    best_business = min(business_results, key=lambda r: r["lowest_price"]) if business_results else None
    best_buy_now = min(buy_now_results, key=lambda r: r["lowest_price"]) if buy_now_results else None
    deal_of_day = min(results, key=lambda r: r["lowest_price"]) if results else None

    message = (
        "📊 DAILY FLIGHT DEAL SUMMARY\n"
        f"⏰ Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )

    if deal_of_day:
        context = get_historical_context(deal_of_day)
        message += (
            f"{get_alert_priority_label(deal_of_day, results)}\n"
            f"{deal_of_day['origin']} → {deal_of_day['destination']}\n"
            f"Cabin: {deal_of_day['cabin'].title()}\n"
            f"Dates: {deal_of_day['departure']} to {deal_of_day['return']}\n"
            f"Price: MX${deal_of_day['lowest_price']:,}\n"
            f"Airline: {deal_of_day['airline']}\n"
            f"Stops: {deal_of_day['stops']}\n"
            f"Duration: {deal_of_day['duration']}\n"
            f"AI Score: {telegram_deal_score(deal_of_day, context)}\n"
        )

        if context:
            message += (
                f"Vs Average: {context['delta_vs_average']:.1f}%\n"
                f"Confidence: {context['confidence']}\n"
            )

        message += f"Link: {deal_of_day['url']}\n\n"

    if best_economy:
        context = get_historical_context(best_economy)
        message += (
            "🏆 Best Economy Deal\n"
            f"{best_economy['origin']} → {best_economy['destination']}\n"
            f"Dates: {best_economy['departure']} to {best_economy['return']}\n"
            f"Price: MX${best_economy['lowest_price']:,}\n"
            f"Airline: {best_economy['airline']}\n"
            f"Stops: {best_economy['stops']}\n"
            f"Duration: {best_economy['duration']}\n"
            f"AI Score: {telegram_deal_score(best_economy, context)}\n"
        )

        if context:
            message += (
                f"Vs Average: {context['delta_vs_average']:.1f}%\n"
                f"Confidence: {context['confidence']}\n"
            )

        message += "\n"

    if best_business:
        context = get_historical_context(best_business)
        message += (
            "💼 Best Business Deal\n"
            f"{best_business['origin']} → {best_business['destination']}\n"
            f"Dates: {best_business['departure']} to {best_business['return']}\n"
            f"Price: MX${best_business['lowest_price']:,}\n"
            f"Airline: {best_business['airline']}\n"
            f"Stops: {best_business['stops']}\n"
            f"Duration: {best_business['duration']}\n"
            f"AI Score: {telegram_deal_score(best_business, context)}\n"
        )

        if context:
            message += (
                f"Vs Average: {context['delta_vs_average']:.1f}%\n"
                f"Confidence: {context['confidence']}\n"
            )

        message += "\n"

    if best_buy_now:
        context = get_historical_context(best_buy_now)
        message += (
            "🔥 Best Buy Now Signal\n"
            f"{best_buy_now['origin']} → {best_buy_now['destination']}\n"
            f"Cabin: {best_buy_now['cabin'].title()}\n"
            f"Dates: {best_buy_now['departure']} to {best_buy_now['return']}\n"
            f"Price: MX${best_buy_now['lowest_price']:,}\n"
            f"Airline: {best_buy_now['airline']}\n"
            f"Stops: {best_buy_now['stops']}\n"
            f"Duration: {best_buy_now['duration']}\n"
            f"AI Score: {telegram_deal_score(best_buy_now, context)}\n"
        )

        if context:
            message += (
                f"Average Price: MX${context['average_price']:,.0f}\n"
                f"Lowest Ever: MX${context['lowest_ever']:,.0f}\n"
                f"Vs Average: {context['delta_vs_average']:.1f}%\n"
                f"Confidence: {context['confidence']}\n"
            )

        message += f"Link: {best_buy_now['url']}\n\n"

    message += "Dashboard updated automatically."

    send_telegram_alert(message)
    print("Daily summary sent.")


def run_searches():
    trips = generate_trips()
    history = load_price_history()

    print(f"Generated {len(trips)} trip combinations.")

    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS_MODE)
        page = browser.new_page()

        for origin in ORIGINS:
            for destination in DESTINATIONS:
                for cabin_class in CABIN_CLASSES:
                    cabin_results = []

                    for trip in trips:
                        result = search_single_trip(
                            page,
                            origin,
                            destination,
                            trip,
                            cabin_class
                        )

                        if result:
                            cabin_results.append(result)
                            all_results.append(result)

                    if cabin_results:
                        top_3_deals = sorted(
                            cabin_results,
                            key=lambda item: item["lowest_price"]
                        )[:3]

                        send_top_3_deals_alert(
                            destination,
                            cabin_class,
                            top_3_deals,
                            history
                        )
                    else:
                        print(f"No valid prices found for {destination} / {cabin_class}.")

        browser.close()

    if all_results:
        save_results_to_csv(all_results)
        print(f"Saved {len(all_results)} results to {CSV_FILE}.")

        send_daily_summary(all_results)

        if os.getenv("GITHUB_ACTIONS") != "true":
            auto_push_to_github()
        else:
            print("Running in GitHub Actions. Skipping internal auto-push.")


def auto_push_to_github():
    try:
        subprocess.run(["git", "pull", "origin", "main", "--no-rebase"], check=True)
        subprocess.run(["git", "add", "flight_results.csv"], check=True)

        commit_result = subprocess.run(
            ["git", "commit", "-m", "Auto update flight results"],
            capture_output=True,
            text=True
        )

        if commit_result.returncode != 0:
            print("No CSV changes to commit.")
            return

        subprocess.run(["git", "push"], check=True)
        print("GitHub auto-push successful.")

    except Exception as e:
        print("GitHub auto-push failed:", e)


run_searches()

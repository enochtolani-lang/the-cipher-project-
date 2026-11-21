import csv
import io
import json
import re
import os
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.db import transaction
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from .models import MenuConversion, SwappedDish
from .services import DishInput, MenuConversion as MenuConversionResult, smart_menu_parser


MAX_MENU_LENGTH = 50000
MAX_DISHES = 100
SUPPORTED_CURRENCIES = (
	"AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
	"BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL",
	"BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY",
	"COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP",
	"ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD",
	"GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR",
	"IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF",
	"KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD",
	"MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR",
	"MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD",
	"OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON",
	"RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP",
	"SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS",
	"TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD",
	"UYU", "UZS", "VES", "VND", "VUV", "WST", "XAF", "XCD", "XOF", "XPF",
	"YER", "ZAR", "ZMW", "ZWG",
)
SAMPLE_MENU = (
	"Dish Name,Ingredients,Current Price\n"
	'Beef Suya,"beef; suya spice; ginger; pepper",NGN 6500\n'
	'Chicken Pepper Soup,"chicken; uziza; pepper; onions",NGN 5800\n'
	'Goat Meat Stew,"goat meat; tomato; thyme; chili",NGN 7200\n'
	'Classic Beef Burger,"beef patty; lettuce; tomato; house sauce",NGN 6800'
)


def index(request):
	recent_conversions = (
		MenuConversion.objects.filter(user=request.user)[:6]
		if request.user.is_authenticated
		else MenuConversion.objects.none()
	)
	return render(request, "core/index.html", {
		"recent_conversions": recent_conversions,
		"currencies": SUPPORTED_CURRENCIES,
	})


def _auth_redirect(request):
	target = request.POST.get("next") or request.GET.get("next")
	if target and url_has_allowed_host_and_scheme(
		target,
		allowed_hosts={request.get_host()},
		require_https=request.is_secure(),
	):
		return redirect(target)
	return redirect("index")


def login_view(request):
	if request.user.is_authenticated:
		return redirect("index")
	form = AuthenticationForm(request, data=request.POST or None)
	if request.method == "POST" and form.is_valid():
		auth_login(request, form.get_user())
		return _auth_redirect(request)
	return render(request, "registration/auth.html", {
		"form": form,
		"form_title": "Welcome back",
		"form_subtitle": "Sign in to your restaurant workspace.",
		"submit_label": "Sign in",
		"alternate_prompt": "New to PlantSwap?",
		"alternate_url": "signup",
		"alternate_label": "Create an account",
		"next_url": request.GET.get("next", ""),
	})


def signup_view(request):
	if request.user.is_authenticated:
		return redirect("index")
	form = UserCreationForm(request.POST or None)
	if request.method == "POST" and form.is_valid():
		user = form.save()
		auth_login(request, user)
		return _auth_redirect(request)
	return render(request, "registration/auth.html", {
		"form": form,
		"form_title": "Create your workspace",
		"form_subtitle": "Save your menu conversions to a private account.",
		"submit_label": "Create account",
		"alternate_prompt": "Already have an account?",
		"alternate_url": "login",
		"alternate_label": "Sign in instead",
		"next_url": request.GET.get("next", ""),
	})


@require_POST
def logout_view(request):
	auth_logout(request)
	return redirect("index")


@require_GET
def sample_menu_view(request):
	return JsonResponse({"menu_text": SAMPLE_MENU})


def _detect_currency(source: str) -> str:
	currency_markers = (
		(r"(?:\bNGN\b|₦)", "NGN"),
		(r"(?:\bUSD\b|\$)", "USD"),
		(r"(?:\bGHS\b|₵)", "GHS"),
		(r"(?:\bKES\b|KSh)", "KES"),
		(r"(?:\bZAR\b)", "ZAR"),
		(r"(?:\bGBP\b|£)", "GBP"),
		(r"(?:\bEUR\b|€)", "EUR"),
	)
	for pattern, currency in currency_markers:
		if re.search(pattern, source, re.IGNORECASE):
			return currency
	return "USD"


def _parse_price(value: str, currency: str) -> Decimal:
	normalized = re.sub(
		r"\b(?:" + "|".join(SUPPORTED_CURRENCIES) + r")\b",
		"",
		value,
		flags=re.IGNORECASE,
	)
	normalized = normalized.replace(",", "")
	normalized = re.sub(r"[^0-9.\-]", "", normalized)
	try:
		price = Decimal(normalized)
	except InvalidOperation as error:
		raise ValueError(f"Could not read menu price '{value.strip()}'.") from error
	if not price.is_finite() or price <= 0:
		raise ValueError(f"Menu price '{value.strip()}' must be greater than zero.")
	if price > Decimal("999999.99"):
		raise ValueError("A dish price cannot exceed 999,999.99.")
	return price


def _parse_menu(source: str) -> tuple[list[DishInput], str]:
	source = source.strip()
	if not source:
		raise ValueError("Paste a menu or load the sample menu to get started.")
	if len(source) > MAX_MENU_LENGTH:
		raise ValueError("The menu is too large. Keep it under 50,000 characters.")

	try:
		dialect = csv.Sniffer().sniff(source[:4096], delimiters=",\t;|")
		parsed_rows = csv.reader(io.StringIO(source), dialect=dialect)
	except csv.Error:
		delimiter = "|" if "|" in source else "\t" if "\t" in source else ","
		parsed_rows = csv.reader(io.StringIO(source), delimiter=delimiter)

	rows = [
		[cell.strip() for cell in row]
		for row in parsed_rows
		if any(cell.strip() for cell in row)
	]
	if not rows:
		raise ValueError("No menu dishes were found.")

	first_row = [cell.casefold() for cell in rows[0]]
	has_header = any(
		any(label in cell for label in ("dish", "name", "ingredient", "price"))
		for cell in first_row
	)
	header = first_row if has_header else []

	def find_column(labels: tuple[str, ...]) -> int | None:
		for index, value in enumerate(header):
			if any(label in value for label in labels):
				return index
		return None

	if has_header:
		name_index = find_column(("dish", "name"))
		price_index = find_column(("price",))
		if name_index is None or price_index is None or name_index == price_index:
			raise ValueError("Include menu headers for Dish Name, Ingredients, and Current Price.")
		ingredient_indices = [
			index for index in range(len(header))
			if index not in (name_index, price_index)
		]
		data_rows = rows[1:]
	else:
		name_index = 0
		price_index = -1
		ingredient_indices = []
		data_rows = rows

	currency = _detect_currency(source)
	dishes: list[DishInput] = []
	for line_number, row in enumerate(data_rows, start=2 if has_header else 1):
		if len(row) < 3:
			raise ValueError(f"Menu row {line_number} needs a dish name, ingredients, and price.")
		actual_price_index = price_index if price_index >= 0 else len(row) - 1
		if max(name_index, actual_price_index) >= len(row):
			raise ValueError(f"Menu row {line_number} does not match the menu headers.")
		dish_name = row[name_index].strip()
		ingredient_text = ", ".join(
			row[index].strip()
			for index in ingredient_indices
			if index < len(row) and row[index].strip()
		) if has_header else ", ".join(row[1:actual_price_index]).strip()
		if not dish_name:
			raise ValueError(f"Menu row {line_number} is missing a dish name.")
		if len(dish_name) > 160:
			raise ValueError(f"Dish names must be 160 characters or fewer (row {line_number}).")
		if not ingredient_text:
			raise ValueError(f"Menu row {line_number} is missing ingredients.")
		dishes.append(
			DishInput(
				name=dish_name,
				ingredients=ingredient_text,
				price=_parse_price(row[actual_price_index], currency),
			)
		)
		if len(dishes) > MAX_DISHES:
			raise ValueError(f"A menu can contain up to {MAX_DISHES} dishes per conversion.")

	if not dishes:
		raise ValueError("The menu contains headers but no dishes.")
	return dishes, currency


def _serialize_result(result: MenuConversionResult, conversion: MenuConversion, currency: str) -> dict:
	return {
		"id": conversion.pk,
		"name": conversion.name,
		"restaurant_name": conversion.restaurant_name,
		"engine": result.engine,
		"currency": currency,
		"original_total_price": str(result.original_total_price),
		"new_total_price": str(result.new_total_price),
		"customer_savings": str(result.original_total_price - result.new_total_price),
		"original_gross_profit": str(result.original_gross_profit),
		"new_gross_profit": str(result.new_gross_profit),
		"profit_change": str(result.profit_change),
		"profit_increase_percent": str(result.profit_increase_percent),
		"dishes": [
			{
				"original_name": dish.original_name,
				"original_ingredients": dish.original_ingredients,
				"original_price": str(dish.original_price),
				"original_ingredient_cost": str(dish.original_ingredient_cost),
				"swapped_name": dish.swapped_name,
				"swapped_ingredients": dish.swapped_ingredients,
				"new_price": str(dish.new_price),
				"swapped_ingredient_cost": str(dish.swapped_ingredient_cost),
				"profit_increase_percent": str(dish.profit_increase_percent),
				"original_nutrition": dish.original_nutrition,
				"swapped_nutrition": dish.swapped_nutrition,
				"texture_notes": dish.texture_notes,
				"flavor_profile": dish.flavor_profile,
				"price_sources": dish.price_sources,
				"market_price_note": dish.market_price_note,
				"swaps": [
					{"original": swap.original, "replacement": swap.replacement}
					for swap in dish.swaps
				],
				"converted": dish.converted,
				"original_cost_is_estimated": dish.original_cost_is_estimated,
			}
			for dish in result.dishes
		],
	}


def _serialize_saved_conversion(conversion: MenuConversion) -> dict:
	dishes = list(conversion.dishes.all())
	original_gross_profit = sum(
		(dish.original_price - dish.original_ingredient_cost for dish in dishes),
		Decimal("0.00"),
	)
	new_gross_profit = sum(
		(dish.new_price - dish.swapped_ingredient_cost for dish in dishes),
		Decimal("0.00"),
	)
	serialized_dishes = []
	for dish in dishes:
		converted = (
			dish.original_name != dish.swapped_name
			or dish.original_ingredients != dish.swapped_ingredients
		)
		swaps = []
		if dish.original_ingredients != dish.swapped_ingredients:
			swaps.append({
				"original": dish.original_ingredients,
				"replacement": dish.swapped_ingredients,
			})
		serialized_dishes.append({
			"original_name": dish.original_name,
			"original_ingredients": dish.original_ingredients,
			"original_price": str(dish.original_price),
			"original_ingredient_cost": str(dish.original_ingredient_cost),
			"swapped_name": dish.swapped_name,
			"swapped_ingredients": dish.swapped_ingredients,
			"new_price": str(dish.new_price),
			"swapped_ingredient_cost": str(dish.swapped_ingredient_cost),
			"profit_increase_percent": str(dish.profit_increase_percent),
			"original_nutrition": dish.original_nutrition,
			"swapped_nutrition": dish.swapped_nutrition,
			"texture_notes": dish.texture_notes,
			"flavor_profile": dish.flavor_profile,
			"price_sources": dish.price_sources,
			"market_price_note": dish.market_price_note,
			"swaps": swaps,
			"converted": converted,
			"original_cost_is_estimated": None,
		})

	return {
		"id": conversion.pk,
		"name": conversion.name,
		"restaurant_name": conversion.restaurant_name,
		"engine": "saved",
		"currency": conversion.currency,
		"original_total_price": str(conversion.original_total_price),
		"new_total_price": str(conversion.new_total_price),
		"customer_savings": str(conversion.original_total_price - conversion.new_total_price),
		"original_gross_profit": str(original_gross_profit),
		"new_gross_profit": str(new_gross_profit),
		"profit_change": str(new_gross_profit - original_gross_profit),
		"profit_increase_percent": str(conversion.profit_increase_percent),
		"dishes": serialized_dishes,
	}


@require_POST
def convert_menu_view(request):
	is_json = request.content_type == "application/json"
	if not request.user.is_authenticated:
		if is_json or "application/json" in request.headers.get("Accept", ""):
			return JsonResponse({
				"error": "Sign in to convert and save a menu.",
				"login_url": redirect("login").url,
			}, status=401)
		return _auth_redirect(request)
	source_menu = ""
	restaurant_name = ""
	try:
		if is_json:
			payload = json.loads(request.body)
			if not isinstance(payload, dict):
				raise ValueError("The request body must be a JSON object.")
			source_menu = payload.get("menu_text", "")
			restaurant_name = payload.get("restaurant_name", "")
			currency_override = payload.get("currency", "")
		else:
			source_menu = request.POST.get("menu_text", "")
			restaurant_name = request.POST.get("restaurant_name", "")
			currency_override = request.POST.get("currency", "")
			if not source_menu and request.FILES.get("menu_file"):
				try:
					source_menu = request.FILES["menu_file"].read().decode("utf-8-sig")
				except UnicodeDecodeError as error:
					raise ValueError("Upload a UTF-8 encoded .txt or .csv file.") from error
		if not isinstance(source_menu, str):
			raise ValueError("Menu text must be a string.")
		if not isinstance(restaurant_name, str):
			raise ValueError("Restaurant name must be a string.")
		restaurant_name = restaurant_name.strip()[:120]
		if not isinstance(currency_override, str):
			raise ValueError("Currency must be a string.")
		currency_override = currency_override.strip().upper()
		if currency_override and currency_override not in SUPPORTED_CURRENCIES:
			raise ValueError("Choose a valid ISO currency code.")
		try:
			fallback_dishes, detected_currency = _parse_menu(source_menu)
		except ValueError:
			fallback_dishes = None
			detected_currency = _detect_currency(source_menu)
		currency = currency_override or detected_currency
		result = smart_menu_parser(source_menu, fallback_dishes=fallback_dishes)
		menu_name = f"{restaurant_name} menu" if restaurant_name else "Menu conversion"
		menu_name = f"{menu_name} · {timezone.localtime():%Y-%m-%d %H:%M}"

		with transaction.atomic():
			conversion = MenuConversion.objects.create(
				user=request.user,
				name=menu_name,
				restaurant_name=restaurant_name,
				source_menu=source_menu,
				currency=currency,
				original_total_price=result.original_total_price,
				new_total_price=result.new_total_price,
				profit_increase_percent=result.profit_increase_percent,
			)
			SwappedDish.objects.bulk_create([
				SwappedDish(
					conversion=conversion,
					position=position,
					original_name=dish.original_name,
					original_ingredients=dish.original_ingredients,
					original_price=dish.original_price,
					swapped_name=dish.swapped_name,
					swapped_ingredients=dish.swapped_ingredients,
					new_price=dish.new_price,
					original_ingredient_cost=dish.original_ingredient_cost,
					swapped_ingredient_cost=dish.swapped_ingredient_cost,
					profit_increase_percent=dish.profit_increase_percent,
					original_nutrition=dish.original_nutrition,
					swapped_nutrition=dish.swapped_nutrition,
					texture_notes=dish.texture_notes,
					flavor_profile=dish.flavor_profile,
					price_sources=dish.price_sources,
					market_price_note=dish.market_price_note,
				)
				for position, dish in enumerate(result.dishes)
			])

		if is_json:
			return JsonResponse(_serialize_result(result, conversion, currency))
		return render(request, "core/results.html", {
			"result": _serialize_result(result, conversion, currency),
		})
	except (json.JSONDecodeError, UnicodeDecodeError):
		message = "The request body must contain valid JSON."
		if is_json:
			return JsonResponse({"error": message}, status=400)
		return render(request, "core/index.html", {
			"recent_conversions": MenuConversion.objects.all()[:6],
			"error": message,
			"restaurant_name": restaurant_name,
			"raw_menu_text": source_menu,
		}, status=400)
	except ValueError as error:
		if is_json:
			return JsonResponse({"error": str(error)}, status=400)
		return render(request, "core/index.html", {
			"recent_conversions": MenuConversion.objects.all()[:6],
			"error": str(error),
			"restaurant_name": restaurant_name,
			"raw_menu_text": source_menu,
		}, status=400)


@require_GET
def openai_health_view(request):
	health = cache.get("plantswap:openai-health")
	if health is not None:
		return JsonResponse(health)

	api_key = os.environ.get("OPENAI_API_KEY", "").strip()
	if not api_key:
		health = {"status": "local", "model": None, "message": "OpenAI key is not configured; local rules are active."}
		cache.set("plantswap:openai-health", health, timeout=300)
		return JsonResponse(health)

	try:
		from openai import OpenAI
		client = OpenAI(api_key=api_key, timeout=8.0)
		response = client.responses.create(
			model="gpt-4.1-mini",
			input="Reply with exactly: OK",
			max_output_tokens=8,
		)
		text = (response.output_text or "").strip()
		health = {
			"status": "openai" if text else "degraded",
			"model": "gpt-4.1-mini",
			"message": "OpenAI is responding." if text else "OpenAI returned an empty response.",
		}
	except Exception as error:
		message = str(error)
		if "429" in message or "credits remaining" in message.lower():
			detail = "OpenAI credits are unavailable; local conversion rules are active."
		else:
			detail = "OpenAI is unavailable; local conversion rules are active."
		health = {"status": "degraded", "model": "gpt-4.1-mini", "message": detail}
		cache.set("plantswap:openai-health:error", message, timeout=300)

	cache.set("plantswap:openai-health", health, timeout=300)
	return JsonResponse(health)


@require_GET
def live_food_data_view(request):
	query = request.GET.get("query", "").strip()
	currency = request.GET.get("currency", "USD").strip().upper()
	if not query:
		return JsonResponse({"error": "Enter a dish name or ingredients first."}, status=400)
	if len(query) > 300:
		return JsonResponse({"error": "The lookup query is too long."}, status=400)

	result = {"nutrition": None, "price": None, "sources": []}
	try:
		params = urllib.parse.urlencode({
			"search_terms": query,
			"search_simple": "1",
			"action": "process",
			"json": "1",
			"page_size": "3",
			"fields": "product_name,nutriments,url",
		})
		request_obj = urllib.request.Request(
			"https://world.openfoodfacts.org/cgi/search.pl?" + params,
			headers={"Accept": "application/json", "User-Agent": "PlantSwap-Django/1.0"},
		)
		with urllib.request.urlopen(request_obj, timeout=8) as response:
			payload = json.loads(response.read().decode("utf-8"))
		product = next((item for item in payload.get("products", []) if item.get("nutriments")), None)
		if product:
			nutriments = product["nutriments"]
			result["nutrition"] = {
				"calories": nutriments.get("energy-kcal_100g"),
				"protein_g": nutriments.get("proteins_100g"),
				"fat_g": nutriments.get("fat_100g"),
				"fiber_g": nutriments.get("fiber_100g"),
			}
		if product and product.get("url"):
			result["sources"].append(product["url"])
	except Exception:
		pass

	try:
		params = urllib.parse.urlencode({"search_terms": query, "page_size": "5"})
		request_obj = urllib.request.Request(
			"https://prices.openfoodfacts.org/api/v1/prices?" + params,
			headers={"Accept": "application/json", "User-Agent": "PlantSwap-Django/1.0"},
		)
		with urllib.request.urlopen(request_obj, timeout=8) as response:
			payload = json.loads(response.read().decode("utf-8"))
		record = next((item for item in (payload.get("items") or payload.get("results") or []) if item and (item.get("price") or item.get("price_value"))), None)
		if record:
			price_value = record.get("price") or record.get("price_value")
			try:
				result["price"] = float(price_value)
			except (TypeError, ValueError):
				result["price"] = None
		if record:
			result["sources"].append(record.get("url") or "https://prices.openfoodfacts.org")
	except Exception:
		pass

	result["currency"] = currency
	return JsonResponse(result)


@require_GET
def conversion_detail(request, conversion_id):
	if not request.user.is_authenticated:
		return JsonResponse({"error": "Sign in to open saved menus."}, status=401)
	conversion = get_object_or_404(MenuConversion, pk=conversion_id, user=request.user)
	return JsonResponse(_serialize_saved_conversion(conversion))

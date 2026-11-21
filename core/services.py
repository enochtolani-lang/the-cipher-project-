"""Rule-based menu conversions and per-dish profitability estimates."""

from __future__ import annotations

import importlib
import csv
import io
import json
import logging
import os
import re
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Protocol

from django.core.cache import cache
from pydantic import BaseModel, ConfigDict


ZERO = Decimal("0.00")
ONE = Decimal("1.00")
MONEY_PLACES = Decimal("0.01")
PERCENT_PLACES = Decimal("0.01")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DishInput:
    name: str
    ingredients: str
    price: Decimal | str | int
    ingredient_cost: Decimal | str | int | None = None


@dataclass(frozen=True)
class IngredientSwap:
    original: str
    replacement: str


@dataclass(frozen=True)
class DishConversion:
    original_name: str
    original_ingredients: str
    original_price: Decimal
    swapped_name: str
    swapped_ingredients: str
    new_price: Decimal
    original_ingredient_cost: Decimal
    swapped_ingredient_cost: Decimal
    original_gross_profit: Decimal
    new_gross_profit: Decimal
    profit_change: Decimal
    profit_increase_percent: Decimal
    swaps: tuple[IngredientSwap, ...]
    converted: bool
    original_cost_is_estimated: bool
    original_nutrition: dict[str, object] = field(default_factory=dict)
    swapped_nutrition: dict[str, object] = field(default_factory=dict)
    texture_notes: str = ""
    flavor_profile: str = ""
    price_sources: list[str] = field(default_factory=list)
    market_price_note: str = ""


@dataclass(frozen=True)
class MenuConversion:
    dishes: tuple[DishConversion, ...]
    original_total_price: Decimal
    new_total_price: Decimal
    original_gross_profit: Decimal
    new_gross_profit: Decimal
    profit_change: Decimal
    profit_increase_percent: Decimal
    engine: str = "local"


class MenuConverter(Protocol):
    def convert_menu(self, dishes: Iterable[DishInput]) -> MenuConversion: ...


@dataclass(frozen=True)
class _SwapChoice:
    name: str
    ingredients: str


_PROTEIN_TERMS = (
    "ground beef",
    "minced beef",
    "beef mince",
    "goat meat",
    "lamb meat",
    "chicken",
    "turkey",
    "sausage",
    "bacon",
    "mutton",
    "lamb",
    "goat",
    "pork",
    "beef",
    "ribeye",
    "sirloin",
    "veal",
    "duck",
    "steak",
    "fish",
    "prawn",
    "shrimp",
    "tilapia",
    "salmon",
    "mackerel",
    "cod",
    "haddock",
    "hake",
    "tuna",
    "trout",
    "sardine",
    "crab",
    "lobster",
    "duck",
    "egg",
    "eggs",
    "cheese",
    "milk",
    "meat",
    "venison",
    "rabbit",
    "hare",
    "boar",
    "buffalo",
    "bison",
    "kangaroo",
    "alligator",
    "squid",
    "calamari",
    "octopus",
    "mussel",
    "mussels",
    "clam",
    "clams",
    "oyster",
    "oysters",
    "scallop",
    "scallops",
    "eel",
    "halibut",
    "bass",
    "grouper",
    "snapper",
    "sole",
    "anchovy",
    "anchovies",
    "ham",
    "prosciutto",
    "pepperoni",
    "pastrami",
    "corned beef",
    "meatball",
    "meatballs",
    "yogurt",
    "cream",
    "butter",
    "gelatin",
)
_PROTEIN_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in _PROTEIN_TERMS) + r")\b",
    re.IGNORECASE,
)
_STOCK_PATTERN = re.compile(
    r"\b(?:beef|chicken|goat|lamb|mutton|pork|fish)\s+(?:stock|broth)\b",
    re.IGNORECASE,
)
_ANIMAL_PATTERN = re.compile(
    r"\b(?:" + "|".join(
        re.escape(term) for term in (*_PROTEIN_TERMS, "beef stock", "beef broth", "chicken stock", "chicken broth", "goat stock", "goat broth", "lamb stock", "lamb broth", "mutton stock", "mutton broth", "pork stock", "pork broth", "fish stock", "fish broth")
    ) + r")\b",
    re.IGNORECASE,
)


def _decimal(value: Decimal | str | int, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a valid number.") from error
    if not result.is_finite() or result < ZERO:
        raise ValueError(f"{field_name} must be a finite, non-negative number.")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def _percent_change(new_value: Decimal, old_value: Decimal) -> Decimal:
    if old_value <= ZERO:
        return ZERO
    return ((new_value - old_value) / old_value * Decimal("100")).quantize(
        PERCENT_PLACES,
        rounding=ROUND_HALF_UP,
    )


def _contains(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def _choose_swap(name: str, ingredients: str) -> _SwapChoice | None:
    text = f"{name} {ingredients}"
    if not _PROTEIN_PATTERN.search(text):
        return None

    if _contains(r"\bburger\b", name):
        return _SwapChoice("Black Bean & Mushroom", "black beans and oyster mushrooms")
    if _contains(r"\bsuya\b", name) and _contains(r"\b(?:beef|steak|meat)\b", text):
        return _SwapChoice("Soya Chunk", "soya chunks")
    if _contains(r"\bpepper soup\b", name) and _contains(r"\bchicken\b", text):
        return _SwapChoice("Oyster Mushroom", "oyster mushrooms")
    if _contains(r"\bstew\b", name) and _contains(r"\b(?:goat|goat meat|mutton|lamb)\b", text):
        return _SwapChoice("Butter Bean", "butter beans")

    if _contains(r"\b(?:egg|eggs|omelette|omelet|quiche|frittata|custard)\b", text):
        return _SwapChoice("Tofu", "tofu")
    if _contains(r"\b(?:cheese|milk|cream|butter|yogurt|gelatin)\b", text):
        return _SwapChoice("Cashew", "cashew cream and plant protein")

    for pattern, choice in (
        (r"\b(?:chicken|turkey|duck)\b", _SwapChoice("Oyster Mushroom", "oyster mushrooms")),
        (r"\b(?:goat|mutton|lamb)\b", _SwapChoice("Soya Chunk", "soya chunks")),
        (r"\b(?:fish|prawn|shrimp|tilapia|salmon|mackerel|cod|haddock|hake|tuna|trout|sardine|crab|lobster|squid|calamari|octopus|mussel|mussels|clam|clams|oyster|oysters|scallop|scallops|eel|halibut|bass|grouper|snapper|sole|anchovy|anchovies)\b", _SwapChoice("Bean", "beans")),
        (r"\b(?:beef|ribeye|sirloin|veal|steak|pork|bacon|sausage|duck|venison|rabbit|hare|boar|buffalo|bison|kangaroo|alligator|ham|prosciutto|pepperoni|pastrami|meat|meatball|meatballs)\b", _SwapChoice("Soya Chunk", "soya chunks")),
    ):
        if _contains(pattern, text):
            return choice
    return None


def _replace_case(match: re.Match[str], replacement: str) -> str:
    original = match.group(0)
    if original.isupper():
        return replacement.upper()
    if original.islower():
        return replacement.lower()
    if original.istitle():
        return replacement.title()
    return replacement


def _convert_name(name: str, choice: _SwapChoice | None) -> str:
    if choice is None:
        return name
    return _PROTEIN_PATTERN.sub(lambda match: _replace_case(match, choice.name), name)


def _convert_ingredients(
    ingredients: str,
    choice: _SwapChoice | None,
) -> tuple[str, tuple[IngredientSwap, ...]]:
    swaps: list[IngredientSwap] = []

    def replace_stock(match: re.Match[str]) -> str:
        original = match.group(0)
        swaps.append(IngredientSwap(original, "vegetable stock"))
        return _replace_case(match, "vegetable stock")

    result = _STOCK_PATTERN.sub(replace_stock, ingredients)
    if choice is not None:
        def replace_protein(match: re.Match[str]) -> str:
            original = match.group(0)
            swaps.append(IngredientSwap(original, choice.ingredients))
            return _replace_case(match, choice.ingredients)

        result = _PROTEIN_PATTERN.sub(replace_protein, result)

    unique_swaps: list[IngredientSwap] = []
    seen: set[tuple[str, str]] = set()
    for swap in swaps:
        key = (swap.original.casefold(), swap.replacement.casefold())
        if key not in seen:
            seen.add(key)
            unique_swaps.append(swap)
    return result, tuple(unique_swaps)


class RuleBasedSwapEngine:
    """Convert common meat dishes using local plant proteins and fixed estimates.

    When no ingredient cost is provided, the original cost is estimated as 50%
    of the listed price. Plant ingredient cost is estimated as 28% of that
    original cost; converted dish prices default to 18% below the original.
    These assumptions are configurable and are not a substitute for supplier
    quotes or a restaurant's actual recipe costing.
    """

    def __init__(
        self,
        *,
        price_discount: Decimal | str | int = Decimal("0.18"),
        estimated_original_cost_ratio: Decimal | str | int = Decimal("0.50"),
        plant_cost_multiplier: Decimal | str | int = Decimal("0.28"),
    ) -> None:
        self.price_discount = _decimal(price_discount, "price_discount")
        self.estimated_original_cost_ratio = _decimal(
            estimated_original_cost_ratio,
            "estimated_original_cost_ratio",
        )
        self.plant_cost_multiplier = _decimal(plant_cost_multiplier, "plant_cost_multiplier")
        if self.price_discount >= ONE:
            raise ValueError("price_discount must be less than 1.")
        if self.estimated_original_cost_ratio > ONE:
            raise ValueError("estimated_original_cost_ratio must not exceed 1.")
        if self.plant_cost_multiplier > ONE:
            raise ValueError("plant_cost_multiplier must not exceed 1.")

    def convert_dish(self, dish: DishInput) -> DishConversion:
        name = dish.name.strip()
        if not name:
            raise ValueError("Every dish must have a name.")

        original_price = _money(_decimal(dish.price, "price"))
        if original_price <= ZERO:
            raise ValueError("Every dish price must be greater than zero.")

        original_ingredients = dish.ingredients.strip()
        original_cost_is_estimated = dish.ingredient_cost is None
        if original_cost_is_estimated:
            original_cost = _money(original_price * self.estimated_original_cost_ratio)
        else:
            original_cost = _money(_decimal(dish.ingredient_cost, "ingredient_cost"))

        choice = _choose_swap(name, original_ingredients)
        swapped_ingredients, swaps = _convert_ingredients(original_ingredients, choice)
        if choice is None:
            stock_swaps = _STOCK_PATTERN.findall(original_ingredients)
            swaps = tuple(IngredientSwap(stock, "vegetable stock") for stock in stock_swaps)
        converted = bool(choice is not None or swaps)

        if converted:
            swapped_name = _convert_name(name, choice)
            new_price = _money(original_price * (Decimal("1") - self.price_discount))
            swapped_cost = _money(original_cost * self.plant_cost_multiplier)
        else:
            swapped_name = name
            new_price = original_price
            swapped_cost = original_cost

        original_profit = _money(original_price - original_cost)
        new_profit = _money(new_price - swapped_cost)
        profit_change = _money(new_profit - original_profit)

        original_nutrition = {
            "calories": 420,
            "protein_g": 26,
            "fat_g": 22,
            "fiber_g": 3,
            "basis": "Estimated per menu serving; verify with recipe costing.",
        }
        swapped_nutrition = {
            "calories": 300,
            "protein_g": 20,
            "fat_g": 12,
            "fiber_g": 10,
            "basis": "Estimated from plant-protein replacement; verify with recipe testing.",
        }
        if not converted:
            swapped_nutrition = dict(original_nutrition)

        return DishConversion(
            original_name=name,
            original_ingredients=original_ingredients,
            original_price=original_price,
            swapped_name=swapped_name,
            swapped_ingredients=swapped_ingredients,
            new_price=new_price,
            original_ingredient_cost=original_cost,
            swapped_ingredient_cost=swapped_cost,
            original_gross_profit=original_profit,
            new_gross_profit=new_profit,
            profit_change=profit_change,
            profit_increase_percent=_percent_change(new_profit, original_profit),
            swaps=swaps,
            converted=converted,
            original_cost_is_estimated=original_cost_is_estimated,
            original_nutrition=original_nutrition,
            swapped_nutrition=swapped_nutrition,
            texture_notes=(
                "Use a short soak or steam, crisp exterior cooking, and a binding sauce to reproduce the original bite."
                if converted else "No texture substitution is required."
            ),
            flavor_profile=(
                "Preserve the original herbs, spices, aromatics, stock profile and finishing sauce while replacing only the animal protein."
                if converted else "Original flavor profile retained."
            ),
        )

    def convert_menu(self, dishes: Iterable[DishInput]) -> MenuConversion:
        converted_dishes = tuple(self.convert_dish(dish) for dish in dishes)
        if not converted_dishes:
            raise ValueError("A menu must contain at least one dish.")

        original_total_price = _money(sum((dish.original_price for dish in converted_dishes), ZERO))
        new_total_price = _money(sum((dish.new_price for dish in converted_dishes), ZERO))
        original_gross_profit = _money(
            sum((dish.original_gross_profit for dish in converted_dishes), ZERO)
        )
        new_gross_profit = _money(sum((dish.new_gross_profit for dish in converted_dishes), ZERO))

        return MenuConversion(
            dishes=converted_dishes,
            original_total_price=original_total_price,
            new_total_price=new_total_price,
            original_gross_profit=original_gross_profit,
            new_gross_profit=new_gross_profit,
            profit_change=_money(new_gross_profit - original_gross_profit),
            profit_increase_percent=_percent_change(new_gross_profit, original_gross_profit),
            engine="local",
        )


def _configured_converter() -> MenuConverter:
    backend_path = os.environ.get("PLANTSWAP_CONVERTER_BACKEND", "").strip()
    if not backend_path:
        return RuleBasedSwapEngine()

    module_name, separator, attribute_name = backend_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError(
            "PLANTSWAP_CONVERTER_BACKEND must use the format 'module.path:ConverterClass'."
        )
    backend_factory = getattr(importlib.import_module(module_name), attribute_name)
    backend = backend_factory()
    if not callable(getattr(backend, "convert_menu", None)):
        raise TypeError("The configured converter must provide a convert_menu(dishes) method.")
    return backend


def convert_menu(
    dishes: Iterable[DishInput],
    *,
    converter: MenuConverter | None = None,
) -> MenuConversion:
    """Convert a menu with the rule engine or an optional configured adapter.

    An optional backend can be selected with
    ``PLANTSWAP_CONVERTER_BACKEND=package.module:ConverterClass``. The class
    must implement ``convert_menu(dishes)`` and return a ``MenuConversion``.
    """
    selected_converter = converter or _configured_converter()
    return selected_converter.convert_menu(tuple(dishes))


_SMART_MENU_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "dishes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "original_name": {"type": "STRING"},
                    "original_ingredients": {"type": "STRING"},
                    "original_price": {"type": "NUMBER"},
                    "swapped_name": {"type": "STRING"},
                    "swapped_ingredients": {"type": "STRING"},
                    "new_price": {"type": "NUMBER"},
                    "profit_increase_percent": {"type": "NUMBER"},
                    "original_nutrition": {
                        "type": "OBJECT",
                        "properties": {
                            "calories": {"type": "NUMBER"},
                            "protein_g": {"type": "NUMBER"},
                            "fat_g": {"type": "NUMBER"},
                            "fiber_g": {"type": "NUMBER"},
                        },
                        "required": ["calories", "protein_g", "fat_g", "fiber_g"],
                    },
                    "swapped_nutrition": {
                        "type": "OBJECT",
                        "properties": {
                            "calories": {"type": "NUMBER"},
                            "protein_g": {"type": "NUMBER"},
                            "fat_g": {"type": "NUMBER"},
                            "fiber_g": {"type": "NUMBER"},
                        },
                        "required": ["calories", "protein_g", "fat_g", "fiber_g"],
                    },
                    "texture_notes": {"type": "STRING"},
                    "flavor_profile": {"type": "STRING"},
                    "swaps": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "original": {"type": "STRING"},
                                "replacement": {"type": "STRING"},
                            },
                            "required": ["original", "replacement"],
                        },
                    },
                    "converted": {"type": "BOOLEAN"},
                },
                "required": [
                    "original_name",
                    "original_ingredients",
                    "original_price",
                    "swapped_name",
                    "swapped_ingredients",
                    "new_price",
                    "profit_increase_percent",
                    "original_nutrition",
                    "swapped_nutrition",
                    "texture_notes",
                    "flavor_profile",
                    "swaps",
                    "converted",
                ],
            },
        },
    },
    "required": ["dishes"],
}


def _fallback_menu_inputs(raw_menu_text: str) -> tuple[DishInput, ...]:
    prose_lines = [
        line.strip()
        for line in raw_menu_text.splitlines()
        if re.search(
            r"(?:[$₦€£]\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s*(?:dollars?|USD|NGN|GHS|KES|ZAR|GBP|EUR)|(?:NGN|USD|GHS|KES|ZAR|GBP|EUR)\s*[\d,]+(?:\.\d+)?)",
            line,
            re.IGNORECASE,
        )
    ]
    if prose_lines:
        dishes = []
        for line in prose_lines[:100]:
            price_match = re.search(
                r"(?:[$₦€£]\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:dollars?|USD|NGN|GHS|KES|ZAR|GBP|EUR)|(?:NGN|USD|GHS|KES|ZAR|GBP|EUR)\s*([\d,]+(?:\.\d+)?))",
                line,
                re.IGNORECASE,
            )
            if not price_match:
                continue
            price = next((value for value in price_match.groups() if value), "")
            line_without_price = (line[:price_match.start()] + line[price_match.end():]).strip()
            line_without_price = re.sub(r"^\s*(?:[-*•]|\d+[).:-]\s*)", "", line_without_price).strip(" \t-,:—")
            if not line_without_price:
                continue
            parts = re.split(r"\s+[—–-]\s+|\bwith\b|[,;:]", line_without_price, maxsplit=1, flags=re.IGNORECASE)
            name = parts[0].strip(" \t-,:—")
            ingredients = parts[1].strip(" \t-,:—") if len(parts) > 1 else line_without_price
            if name:
                dishes.append(DishInput(name=name[:160], ingredients=ingredients[:1000] or name, price=price))
        if dishes:
            return tuple(dishes)

    try:
        dialect = csv.Sniffer().sniff(raw_menu_text[:4096], delimiters=",\t;|")
        rows = list(csv.reader(io.StringIO(raw_menu_text), dialect=dialect))
    except csv.Error:
        delimiter = "|" if "|" in raw_menu_text else "\t" if "\t" in raw_menu_text else ","
        rows = list(csv.reader(io.StringIO(raw_menu_text), delimiter=delimiter))

    rows = [[cell.strip() for cell in row] for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("No menu dishes were found.")

    header = [cell.casefold() for cell in rows[0]]
    has_header = any(any(term in cell for term in ("dish", "name", "ingredient", "price")) for cell in header)
    if has_header:
        name_index = next((i for i, cell in enumerate(header) if "dish" in cell or "name" in cell), None)
        price_index = next((i for i, cell in enumerate(header) if "price" in cell), None)
        if name_index is None or price_index is None or name_index == price_index:
            raise ValueError("Include dish name, ingredients, and price columns.")
        ingredient_indices = [i for i in range(len(header)) if i not in (name_index, price_index)]
        data_rows = rows[1:]
    else:
        name_index = 0
        price_index = -1
        ingredient_indices = []
        data_rows = rows

    dishes = []
    for row in data_rows[:100]:
        if len(row) < 3:
            continue
        actual_price_index = price_index if price_index >= 0 else len(row) - 1
        name = row[name_index].strip()
        ingredients = ", ".join(
            row[index].strip()
            for index in ingredient_indices
            if index < len(row) and row[index].strip()
        ) if has_header else ", ".join(row[1:actual_price_index]).strip()
        normalized_price = re.sub(r"[^0-9.\-]", "", row[actual_price_index].replace(",", ""))
        if not name or not ingredients:
            continue
        dishes.append(DishInput(name, ingredients, normalized_price))

    if dishes:
        return tuple(dishes)

    for line in raw_menu_text.splitlines():
        line = line.strip()
        if not line:
            continue
        price_match = re.search(
            r"(?:[$₦€£]\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:dollars?|USD|NGN|GHS|KES|ZAR|GBP|EUR)|(?:NGN|USD|GHS|KES|ZAR|GBP|EUR)\s*([\d,]+(?:\.\d+)?))",
            line,
            re.IGNORECASE,
        )
        if not price_match:
            continue
        price = next((value for value in price_match.groups() if value), "")
        if not price:
            continue
        line_without_price = line[:price_match.start()] + line[price_match.end():]
        line_without_price = re.sub(r"^\s*(?:[-*•]|\d+[).:-]\s*)", "", line_without_price).strip(" \t-,:—")
        if not line_without_price:
            continue
        parts = re.split(r"\s+[—–-]\s+|\bwith\b|[,;:]", line_without_price, maxsplit=1, flags=re.IGNORECASE)
        name = parts[0].strip(" \t-,:—")
        ingredients = parts[1].strip(" \t-,:—") if len(parts) > 1 else line_without_price
        if name:
            dishes.append(DishInput(name=name[:160], ingredients=ingredients[:1000] or name, price=price))
        if len(dishes) >= 100:
            break

    if not dishes:
        raise ValueError("Could not find menu rows with a dish name, ingredients, and price.")
    return tuple(dishes)


def _menu_from_smart_data(data: object, fallback_rows: tuple[DishInput, ...] | None = None) -> MenuConversion:
    if not isinstance(data, dict) or not isinstance(data.get("dishes"), list):
        raise ValueError("OpenAI returned an invalid menu structure.")
    rows = data["dishes"]
    if not rows or len(rows) > 100:
        raise ValueError("OpenAI must return between 1 and 100 menu dishes.")

    engine = RuleBasedSwapEngine()
    converted_dishes: list[DishConversion] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"OpenAI returned an invalid dish at row {index}.")
        original_name = row.get("original_name")
        original_ingredients = row.get("original_ingredients")
        swapped_name = row.get("swapped_name")
        swapped_ingredients = row.get("swapped_ingredients")
        swaps_data = row.get("swaps")
        if not all(isinstance(value, str) for value in (
            original_name,
            original_ingredients,
            swapped_name,
            swapped_ingredients,
        )) or not isinstance(swaps_data, list):
            raise ValueError(f"OpenAI returned invalid text fields at row {index}.")

        original_name = original_name.strip()
        original_ingredients = original_ingredients.strip()
        if not original_name or not original_ingredients:
            raise ValueError(f"OpenAI returned an empty dish name or ingredients at row {index}.")
        try:
            original_price = _money(_decimal(row.get("original_price"), "original_price"))
        except ValueError:
            original_price = ZERO
        if original_price <= ZERO and fallback_rows:
            matching_fallback = fallback_rows[index - 1] if index - 1 < len(fallback_rows) else None
            if matching_fallback:
                original_price = _money(_decimal(matching_fallback.price, "original_price"))
        try:
            suggested_price = _money(_decimal(row.get("new_price"), "new_price"))
        except ValueError:
            suggested_price = ZERO
        model_profit_percent = _decimal(row.get("profit_increase_percent"), "profit_increase_percent")
        converted = row.get("converted")
        if not isinstance(converted, bool):
            raise ValueError(f"OpenAI returned an invalid conversion flag at row {index}.")

        original_nutrition = row.get("original_nutrition")
        swapped_nutrition = row.get("swapped_nutrition")
        texture_notes = row.get("texture_notes")
        flavor_profile = row.get("flavor_profile")
        if not isinstance(original_nutrition, dict) or not isinstance(swapped_nutrition, dict):
            raise ValueError(f"OpenAI returned invalid nutrition data at row {index}.")
        if not isinstance(texture_notes, str) or not isinstance(flavor_profile, str):
            raise ValueError(f"OpenAI returned invalid sensory data at row {index}.")
        for nutrition_name, nutrition in (("original", original_nutrition), ("swapped", swapped_nutrition)):
            for metric in ("calories", "protein_g", "fat_g", "fiber_g"):
                value = nutrition.get(metric)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(f"OpenAI returned invalid {nutrition_name} {metric} at row {index}.")
                if value < 0:
                    raise ValueError(f"OpenAI returned a negative {nutrition_name} {metric} at row {index}.")
            nutrition["basis"] = (
                "OpenAI live or estimated per-serving data; verify with a recipe or nutrition test."
            )

        if original_price <= ZERO:
            raise ValueError(f"OpenAI and fallback parsing returned no valid price at row {index}.")

        if converted:
            expected_price = _money(original_price * (Decimal("1") - engine.price_discount))
            if not (original_price * Decimal("0.80") <= suggested_price <= original_price * Decimal("0.90")):
                raise ValueError(f"OpenAI returned an invalid reduced price at row {index}.")
            if _ANIMAL_PATTERN.search(swapped_ingredients):
                raise ValueError(f"OpenAI left an animal protein in the swapped ingredients at row {index}.")
            if model_profit_percent < ZERO:
                raise ValueError(f"OpenAI returned an invalid profit estimate at row {index}.")
            final_name = swapped_name.strip() or original_name
            final_ingredients = swapped_ingredients
            final_price = expected_price
            original_cost = _money(original_price * engine.estimated_original_cost_ratio)
            swapped_cost = _money(original_cost * engine.plant_cost_multiplier)
            swaps: list[IngredientSwap] = []
            for swap in swaps_data:
                if not isinstance(swap, dict):
                    raise ValueError(f"OpenAI returned an invalid ingredient swap at row {index}.")
                original = swap.get("original")
                replacement = swap.get("replacement")
                if not isinstance(original, str) or not isinstance(replacement, str):
                    raise ValueError(f"OpenAI returned invalid ingredient swap fields at row {index}.")
                swaps.append(IngredientSwap(original, replacement))
            if not swaps:
                swaps.append(IngredientSwap(original_ingredients, final_ingredients))
        else:
            final_name = original_name
            final_ingredients = original_ingredients
            final_price = original_price
            original_cost = _money(original_price * engine.estimated_original_cost_ratio)
            swapped_cost = original_cost
            swaps = []

        original_profit = _money(original_price - original_cost)
        new_profit = _money(final_price - swapped_cost)
        converted_dishes.append(DishConversion(
            original_name=original_name,
            original_ingredients=original_ingredients,
            original_price=original_price,
            swapped_name=final_name,
            swapped_ingredients=final_ingredients,
            new_price=final_price,
            original_ingredient_cost=original_cost,
            swapped_ingredient_cost=swapped_cost,
            original_gross_profit=original_profit,
            new_gross_profit=new_profit,
            profit_change=_money(new_profit - original_profit),
            profit_increase_percent=_percent_change(new_profit, original_profit),
            swaps=tuple(swaps),
            converted=converted,
            original_cost_is_estimated=True,
            original_nutrition=original_nutrition,
            swapped_nutrition=swapped_nutrition,
            texture_notes=texture_notes,
            flavor_profile=flavor_profile,
        ))

    original_total_price = _money(sum((dish.original_price for dish in converted_dishes), ZERO))
    new_total_price = _money(sum((dish.new_price for dish in converted_dishes), ZERO))
    original_gross_profit = _money(sum((dish.original_gross_profit for dish in converted_dishes), ZERO))
    new_gross_profit = _money(sum((dish.new_gross_profit for dish in converted_dishes), ZERO))
    return MenuConversion(
        dishes=tuple(converted_dishes),
        original_total_price=original_total_price,
        new_total_price=new_total_price,
        original_gross_profit=original_gross_profit,
        new_gross_profit=new_gross_profit,
        profit_change=_money(new_gross_profit - original_gross_profit),
        profit_increase_percent=_percent_change(new_gross_profit, original_gross_profit),
        engine="openai",
    )


class OpenAINutrition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calories: float
    protein_g: float
    fat_g: float
    fiber_g: float


class OpenAISwap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original: str
    replacement: str


class OpenAIDish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_name: str
    original_ingredients: str
    original_price: float
    swapped_name: str
    swapped_ingredients: str
    new_price: float
    profit_increase_percent: float
    original_nutrition: OpenAINutrition
    swapped_nutrition: OpenAINutrition
    texture_notes: str
    flavor_profile: str
    swaps: list[OpenAISwap]
    converted: bool
    price_sources: list[str]
    market_price_note: str


class OpenAIMenu(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dishes: list[OpenAIDish]


def smart_menu_parser(
    raw_menu_text: str,
    *,
    fallback_dishes: Iterable[DishInput] | None = None,
) -> MenuConversion:
    """Parse, price and convert a menu with OpenAI web search and local fallback."""
    if not isinstance(raw_menu_text, str) or not raw_menu_text.strip():
        raise ValueError("Menu text is required.")
    if len(raw_menu_text) > 50000:
        raise ValueError("The menu is too large. Keep it under 50,000 characters.")

    fallback_rows = tuple(fallback_dishes) if fallback_dishes is not None else None
    cache_key = "plantswap:openai:menu:" + hashlib.sha256(raw_menu_text.strip().encode("utf-8")).hexdigest()
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return cached_result

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        result = RuleBasedSwapEngine().convert_menu(
            fallback_rows if fallback_rows is not None else _fallback_menu_inputs(raw_menu_text)
        )
        cache.set(cache_key, result, timeout=1800)
        return result

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=30.0)
        prompt = (
            "Convert the restaurant menu below into plant-forward versions. "
            "Use live web search to find current local market and restaurant dish prices. "
            "Infer the currency only from clear symbols or regional context. Replace animal proteins "
            "with soya chunks, oyster mushrooms, beans or lentils while preserving herbs, spices, stocks, "
            "acidity, heat, aromatics and finishing sauces. Explain texture preservation through moisture, "
            "binding and cooking technique. Estimate calories, protein_g, fat_g and fiber_g per serving. "
            "Return price_sources as source names or URLs and never invent sources or dishes. "
            "Set converted false for dishes with no animal protein.\n\n"
            f"RAW MENU:\n{raw_menu_text}"
        )
        response = client.responses.parse(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": "Return JSON only through the provided schema. Use live web search for current ingredient and menu prices. Clearly distinguish estimates from listed restaurant prices."},
                {"role": "user", "content": prompt},
            ],
            tools=[{"type": "web_search_preview", "search_context_size": "medium"}],
            text_format=OpenAIMenu,
            temperature=0.2,
            max_output_tokens=6000,
            timeout=30.0,
        )
        if response.status != "completed" or response.output_parsed is None:
            raise RuntimeError("OpenAI did not return a completed structured result.")

        payload = response.output_parsed.model_dump()
        result = _menu_from_smart_data(payload, fallback_rows)

        for dish, parsed_dish in zip(result.dishes, payload["dishes"]):
            object.__setattr__(dish, "price_sources", list(parsed_dish.get("price_sources", [])))
            object.__setattr__(dish, "market_price_note", parsed_dish.get("market_price_note", ""))

        cache.set(cache_key, result, timeout=1800)
        return result
    except Exception as error:
        logger.warning("OpenAI menu conversion unavailable; using local rules: %s", error)
        rows = fallback_rows if fallback_rows is not None else _fallback_menu_inputs(raw_menu_text)
        result = RuleBasedSwapEngine().convert_menu(rows)
        cache.set(cache_key, result, timeout=1800)
        return result
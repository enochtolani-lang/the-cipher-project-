import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import MenuConversion
from .services import DishInput, smart_menu_parser


class AccountAndConversionAccessTests(TestCase):
	menu_text = (
		"Dish Name,Ingredients,Current Price\n"
		'Beef Suya,"beef; suya spice; pepper",NGN 6500\n'
		'Chicken Pepper Soup,"chicken; pepper; uziza",NGN 5800'
	)

	@classmethod
	def setUpTestData(cls):
		user_model = get_user_model()
		cls.owner = user_model.objects.create_user(
			username="menu-owner",
			password="Local-Test-Password-984!",
		)
		cls.other_user = user_model.objects.create_user(
			username="other-restaurant",
			password="Different-Test-Password-527!",
		)

	def test_anonymous_user_can_load_sample_but_cannot_convert(self):
		sample = self.client.get(reverse("sample_menu"))
		self.assertEqual(sample.status_code, 200)
		self.assertIn("Beef Suya", sample.json()["menu_text"])

		response = self.client.post(
			reverse("convert_menu"),
			data=json.dumps({"menu_text": self.menu_text}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 401)
		self.assertEqual(response.json()["login_url"], reverse("login"))

	def test_signed_in_conversion_is_owned_and_readable_by_owner(self):
		self.client.force_login(self.owner)
		with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
			response = self.client.post(
				reverse("convert_menu"),
				data=json.dumps({
					"menu_text": self.menu_text,
					"restaurant_name": "Test Kitchen",
				}),
				content_type="application/json",
			)

		self.assertEqual(response.status_code, 200)
		conversion = MenuConversion.objects.get(pk=response.json()["id"])
		self.assertEqual(conversion.user, self.owner)
		self.assertEqual(conversion.restaurant_name, "Test Kitchen")
		self.assertEqual(
			self.client.get(reverse("conversion_detail", args=[conversion.pk])).status_code,
			200,
		)

	def test_another_user_cannot_read_conversion(self):
		conversion = MenuConversion.objects.create(
			user=self.owner,
			name="Owner menu",
			restaurant_name="Owner restaurant",
			source_menu=self.menu_text,
		)
		self.client.force_login(self.other_user)
		response = self.client.get(reverse("conversion_detail", args=[conversion.pk]))
		self.assertEqual(response.status_code, 404)

	def test_signup_creates_an_authenticated_account(self):
		response = self.client.post(reverse("signup"), {
			"username": "new-restaurant",
			"password1": "PlantSwap-Testing-Password-931!",
			"password2": "PlantSwap-Testing-Password-931!",
		})
		self.assertRedirects(response, reverse("index"))
		self.assertTrue(self.client.session.get("_auth_user_id"))

	def test_login_and_post_logout_manage_session(self):
		login_response = self.client.post(reverse("login"), {
			"username": "menu-owner",
			"password": "Local-Test-Password-984!",
		})
		self.assertRedirects(login_response, reverse("index"))
		self.assertTrue(self.client.session.get("_auth_user_id"))

		logout_response = self.client.post(reverse("logout"))
		self.assertRedirects(logout_response, reverse("index"))
		self.assertNotIn("_auth_user_id", self.client.session)

	def test_structured_dish_csv_payload_converts_three_distinct_dishes(self):
		self.client.force_login(self.owner)
		payload = (
			"Dish Name,Ingredients,Current Price\n"
			'Beef Suya,"beef, suya spice, ginger, pepper",NGN 6500\n'
			'Chicken Pepper Soup,"chicken, uziza, pepper, onions",NGN 5800\n'
			'Plantain Bowl,"plantain, beans, red pepper",NGN 4200'
		)
		with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
			response = self.client.post(
				reverse("convert_menu"),
				data=json.dumps({"menu_text": payload, "restaurant_name": "Test Kitchen"}),
				content_type="application/json",
			)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.json()["dishes"]), 3)
		self.assertEqual(response.json()["dishes"][0]["swapped_name"], "Soya Chunk Suya")

	def test_currency_override_is_saved_and_returned(self):
		self.client.force_login(self.owner)
		payload = {
			"menu_text": "Dish Name,Ingredients,Current Price\nBeef Suya,beef; pepper,6500",
			"restaurant_name": "Currency Test",
			"currency": "EUR",
		}
		with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
			response = self.client.post(
				reverse("convert_menu"),
				data=json.dumps(payload),
				content_type="application/json",
			)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["currency"], "EUR")

	def test_broad_animal_protein_rules_swap_venison_and_calamari(self):
		with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
			venison = smart_menu_parser(
				"unused",
				fallback_dishes=(DishInput("Venison Burger", "venison, onion, pepper", "30"),),
			)
			calamari = smart_menu_parser(
				"unused",
				fallback_dishes=(DishInput("Calamari Stew", "squid, tomato, chilli", "20"),),
			)
		self.assertTrue(venison.dishes[0].converted)
		self.assertIn("Mushroom", venison.dishes[0].swapped_name)
		self.assertTrue(calamari.dishes[0].converted)
		self.assertIn("Bean", calamari.dishes[0].swapped_name)

	def test_index_only_lists_the_signed_in_users_conversions(self):
		MenuConversion.objects.create(
			user=self.owner,
			name="Private owner menu",
			source_menu=self.menu_text,
		)
		MenuConversion.objects.create(
			user=self.other_user,
			name="Private other menu",
			source_menu=self.menu_text,
		)
		self.client.force_login(self.owner)
		response = self.client.get(reverse("index"))
		self.assertContains(response, "Private owner menu")
		self.assertNotContains(response, "Private other menu")

from django.db import models
from django.conf import settings

class MenuConversion(models.Model):
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="menu_conversions",
		null=True,
		blank=True,
	)
	name = models.CharField(max_length=120, blank=True, default="")
	restaurant_name = models.CharField(max_length=120, blank=True, default="")
	source_menu = models.TextField()
	currency = models.CharField(max_length=3, default="USD")
	original_total_price = models.DecimalField(max_digits=10, decimal_places=2, default="0.00")
	new_total_price = models.DecimalField(max_digits=10, decimal_places=2, default="0.00")
	profit_increase_percent = models.DecimalField(max_digits=6, decimal_places=2, default="0.00")
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["-created_at", "-pk"]

	def __str__(self):
		return self.name or f"Menu conversion {self.pk or 'unsaved'}"


class SwappedDish(models.Model):
	conversion = models.ForeignKey(
		MenuConversion,
		on_delete=models.CASCADE,
		related_name="dishes",
	)
	position = models.PositiveIntegerField(default=0)
	original_name = models.CharField(max_length=160)
	original_ingredients = models.TextField(blank=True)
	original_price = models.DecimalField(max_digits=8, decimal_places=2)
	swapped_name = models.CharField(max_length=160)
	swapped_ingredients = models.TextField(blank=True)
	new_price = models.DecimalField(max_digits=8, decimal_places=2)
	original_ingredient_cost = models.DecimalField(max_digits=8, decimal_places=2, default="0.00")
	swapped_ingredient_cost = models.DecimalField(max_digits=8, decimal_places=2, default="0.00")
	profit_increase_percent = models.DecimalField(max_digits=6, decimal_places=2, default="0.00")
	original_nutrition = models.JSONField(default=dict, blank=True)
	swapped_nutrition = models.JSONField(default=dict, blank=True)
	texture_notes = models.TextField(blank=True)
	flavor_profile = models.TextField(blank=True)
	price_sources = models.JSONField(default=list, blank=True)
	market_price_note = models.TextField(blank=True)
	price_sources = models.JSONField(default=list, blank=True)
	market_price_note = models.TextField(blank=True)

	class Meta:
		ordering = ["position", "pk"]

	def __str__(self):
		return f"{self.original_name} -> {self.swapped_name}"

"""
URL configuration for plantswap project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path

from core import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.index, name="index"),
    path("accounts/signup/", views.signup_view, name="signup"),
    path("accounts/login/", views.login_view, name="login"),
    path("accounts/logout/", views.logout_view, name="logout"),
    path("api/convert/", views.convert_menu_view, name="convert_menu"),
    path("api/sample/", views.sample_menu_view, name="sample_menu"),
    path("api/health/", views.openai_health_view, name="openai_health"),
    path("api/live-food-data/", views.live_food_data_view, name="live_food_data"),
    path("api/conversions/<int:conversion_id>/", views.conversion_detail, name="conversion_detail"),
]

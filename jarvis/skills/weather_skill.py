"""Weather via Open-Meteo (no API key, needs internet)."""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from .base import Skill, ToolResult, fn, s
from .core import pct, plural_ru, units

log = logging.getLogger(__name__)

WEATHER_RE = re.compile(r"\b(погод\w*|прогноз\w*|температур\w* на улице|сколько градусов|что на улице|дождь будет|зонт)\b")
CITY_RE = re.compile(r"\bв (?:городе )?([а-яa-z][\w-]+(?: [а-яa-z][\w-]+)?)\s*$")

WMO = {0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно", 45: "туман", 48: "изморозь",
       51: "лёгкая морось", 53: "морось", 55: "сильная морось", 56: "ледяная морось", 57: "ледяная морось",
       61: "небольшой дождь", 63: "дождь", 65: "ливень", 66: "ледяной дождь", 67: "ледяной дождь",
       71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа", 80: "ливневый дождь",
       81: "ливни", 82: "сильные ливни", 85: "снегопад", 86: "сильный снегопад", 95: "гроза",
       96: "гроза с градом", 99: "сильная гроза с градом"}


def deg(n: float) -> str:
    v = int(round(n))
    return f"{v} {plural_ru(v, 'градус', 'градуса', 'градусов')}"


# 1.0.9: «загугли погода в москве», «найди в хроме погоду» -> an explicit browser search (core), not the forecast
BROWSER_SEARCH_RE = re.compile(r"^(?:загугли|погугли|гугли)\b|\b(?:в|на|через) (?:гугле?|яндексе?|хроме|chrome|google|"
                               r"браузере|ютубе?)\b")

class WeatherSkill(Skill):
    name = "weather"

    def __init__(self, ctx):
        super().__init__(ctx)
        w = ctx.cfg.get("weather", {})
        self.city = w.get("city", "Сумы")
        self.lat, self.lon = float(w.get("latitude", 50.9077)), float(w.get("longitude", 34.7981))
        self.tz = w.get("timezone", "Europe/Kyiv")

    @staticmethod
    def geocode(name: str) -> Optional[tuple[float, float, str, str]]:
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": name, "count": 1, "language": "ru"}, timeout=10)
        res = (r.json().get("results") or [])
        if not res:
            return None
        x = res[0]
        return x["latitude"], x["longitude"], x.get("name", name), x.get("timezone", "auto")

    def forecast(self, city: Optional[str] = None, tomorrow: bool = False) -> str:
        lat, lon, name, tz = self.lat, self.lon, self.city, self.tz
        try:
            if city and city.lower() not in (self.city.lower(), "сумах", "сумы"):
                g = self.geocode(city)
                if not g:
                    return f"Не знаю город {city}."
                lat, lon, name, tz = g
            r = requests.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "timezone": tz, "forecast_days": 2,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "wind_speed_unit": "ms"}, timeout=10)
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            log.warning("Погода: %s", e)
            return "Не могу получить погоду, похоже нет интернета."
        daily = d["daily"]
        i = 1 if tomorrow else 0
        day_desc = WMO.get(daily["weather_code"][i], "")
        rng = f"от {deg(daily['temperature_2m_min'][i])} до {deg(daily['temperature_2m_max'][i])}"
        rain = daily.get("precipitation_probability_max", [None, None])[i]
        rain_s = f", вероятность осадков {pct(rain)}" if rain is not None else ""
        if tomorrow:
            return f"Завтра в городе {name} {day_desc}, {rng}{rain_s}."
        c = d["current"]
        return (f"Сейчас в городе {name} {deg(c['temperature_2m'])}, ощущается как {deg(c['apparent_temperature'])}, "
                f"{WMO.get(c['weather_code'], '')}, ветер {units(c['wind_speed_10m'], 'метр', 'метра', 'метров')} в секунду. "
                f"Днём {rng}{rain_s}.")

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        if not WEATHER_RE.search(t) or BROWSER_SEARCH_RE.search(t):
            return None
        m = CITY_RE.search(t)
        city = m.group(1) if m and m.group(1) not in ("сегодня", "завтра", "городе") else None
        tomorrow = "завтра" in t
        return ToolResult("", then=lambda: self.forecast(city, tomorrow))

    def tools(self):
        return [fn("weather", "Погода сейчас или завтра.", {"city": s("город (необязательно)"),
                                                             "day": s("день", ["today", "tomorrow"])})]

    def call(self, name, args):
        if name == "weather":
            city, day = args.get("city") or None, args.get("day") == "tomorrow"
            return ToolResult("", then=lambda: self.forecast(city, day))
        return None

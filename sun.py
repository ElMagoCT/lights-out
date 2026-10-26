"""Sunrise / sunset for the sun-synced brightness curve. Pure Python (NOAA
solar equations), no dependencies. Location comes from config, else a single
cached IP geolocation lookup (ip-api.com, no key, one request ever).
"""
import datetime as dt
import json
import logging
import math
import os
import urllib.request

log = logging.getLogger("lightsout.sun")


def _jd(date):
    """Julian day at 0h UT for a date."""
    y, m, d = date.year, date.month, date.day
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def sun_times(date, lat, lon):
    """Return (sunrise, sunset) as timezone-naive UTC datetimes, or (None, None)
    for polar day/night."""
    jd = _jd(date)
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t) + math.sin(3 * mr) * 0.000289)
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    lam = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(lam))))
    y = math.tan(math.radians(eps / 2)) ** 2
    l0r, mr = math.radians(l0), math.radians(m)
    eqt = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                           - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    latr, declr = math.radians(lat), math.radians(decl)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(latr) * math.cos(declr))) - math.tan(latr) * math.tan(declr)
    if cos_ha > 1 or cos_ha < -1:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    noon = 720 - 4 * lon - eqt            # minutes after 0h UT
    rise = noon - ha * 4
    sett = noon + ha * 4
    base = dt.datetime(date.year, date.month, date.day)
    return base + dt.timedelta(minutes=rise), base + dt.timedelta(minutes=sett)


def local_sun_times(date_local, lat, lon):
    """Sunrise/sunset as naive local datetimes for the machine's local zone."""
    rise, sett = sun_times(date_local, lat, lon)
    if rise is None:
        return None, None
    off = dt.datetime.now().astimezone().utcoffset()
    return rise + off, sett + off


def brightness_factor(now, lat, lon, transition_min=45, night_strength=0.6):
    """0..1 multiplier for the day brightness. 1.0 all day; (1 - strength) at
    night; smooth ramps centred on sunrise and sunset."""
    rise, sett = local_sun_times(now.date(), lat, lon)
    if rise is None:
        return 1.0
    night = max(0.0, 1.0 - night_strength)
    half = dt.timedelta(minutes=transition_min / 2)

    def ramp(x):                         # 0..1 smoothstep
        x = max(0.0, min(1.0, x))
        return x * x * (3 - 2 * x)

    if now < rise - half or now > sett + half:
        return night
    if now < rise + half:
        return night + (1 - night) * ramp((now - (rise - half)) / (2 * half))
    if now > sett - half:
        return 1 - (1 - night) * ramp((now - (sett - half)) / (2 * half))
    return 1.0


def locate(cache_path):
    """(lat, lon, label) from the cache file, else one IP lookup, cached forever."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            c = json.load(f)
        if "lat" in c and "lon" in c:
            return c["lat"], c["lon"], c.get("label", "")
    except Exception:
        pass
    # HTTPS first (ipinfo.io), plain-HTTP ip-api.com as the fallback. Either way the
    # only thing sent is the request itself; the answer is cached locally forever.
    lookups = (
        ("https://ipinfo.io/json", lambda d: (float(d["loc"].split(",")[0]), float(d["loc"].split(",")[1]),
                                               f"{d.get('city', '')}, {d.get('region', '')}".strip(", "))),
        ("http://ip-api.com/json/?fields=status,city,regionName,lat,lon",
         lambda d: (d["lat"], d["lon"], f"{d.get('city', '')}, {d.get('regionName', '')}".strip(", "))),
    )
    for url, parse in lookups:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "LightsOut/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode())
            lat, lon, label = parse(d)
            c = {"lat": lat, "lon": lon, "label": label}
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(c, f)
            log.info("located via %s: %s", url.split("/")[2], c)
            return lat, lon, label
        except Exception as e:
            log.warning("geolocation via %s failed: %s", url.split("/")[2], e)
    return None, None, ""


if __name__ == "__main__":
    lat, lon = 33.6262, -112.0074
    now = dt.datetime.now()
    print("sunrise/sunset today (local):", local_sun_times(now.date(), lat, lon))
    for h in range(0, 24, 2):
        t = now.replace(hour=h, minute=0)
        print(f"{h:02d}:00 factor {brightness_factor(t, lat, lon):.2f}")

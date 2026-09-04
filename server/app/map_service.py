"""高德地理编码与演示房源坐标生成。"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")

AMAP_WEB_SERVICE_KEY = os.environ.get("AMAP_WEB_SERVICE_KEY", "").strip()
MAP_GEOCODE_ENABLED = os.environ.get(
    "MAP_GEOCODE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"

# GCJ-02 坐标。演示库里的门牌号是随机生成的，不能假装是真实地址；
# 因此存量演示房源以商圈中心点为基准做稳定的小范围偏移。
COMMUNITY_CENTERS = {
    "青泥洼桥": (121.6346, 38.9186),
    "人民路": (121.6603, 38.9215),
    "桂林街": (121.6470, 38.9049),
    "五惠路": (121.6330, 38.9107),
    "海军广场": (121.6742, 38.9180),
    "八一路": (121.6150, 38.8990),
    "香炉礁": (121.5980, 38.9370),
    "五四广场": (121.6110, 38.9160),
    "北京街": (121.6130, 38.9230),
    "星海广场": (121.5880, 38.8810),
    "黑石礁": (121.5650, 38.8790),
    "西安路": (121.5930, 38.9210),
    "马栏广场": (121.5720, 38.9290),
    "李家街": (121.5840, 38.9460),
    "华南广场": (121.5960, 39.0030),
    "泡崖": (121.5550, 38.9840),
    "红旗": (121.5380, 38.9220),
    "革镇堡": (121.5260, 39.0690),
    "椒金山": (121.6110, 38.9650),
    "水师营": (121.2540, 38.8510),
    "龙王塘": (121.4010, 38.8460),
    "铁山": (121.1740, 38.7970),
    "拥政街道": (121.7180, 39.1040),
    "光明街道": (121.7220, 39.1000),
    "friendship 友谊": (121.7040, 39.1040),
}

DISTRICT_CENTERS = {
    "中山区": (121.6560, 38.9180),
    "西岗区": (121.6120, 38.9140),
    "沙河口区": (121.5800, 38.9100),
    "甘井子区": (121.5700, 38.9550),
    "旅顺口区": (121.2610, 38.8120),
    "金州区": (121.7180, 39.1050),
}


def approximate_coordinates(district, community, identity):
    """为虚构演示地址生成可复现、不过度重叠的坐标。"""
    center = COMMUNITY_CENTERS.get(community) or DISTRICT_CENTERS.get(district)
    if not center:
        return None
    digest = hashlib.sha256(str(identity).encode("utf-8")).digest()
    longitude_offset = (int.from_bytes(digest[:2], "big") / 65535 - 0.5) * 0.012
    latitude_offset = (int.from_bytes(digest[2:4], "big") / 65535 - 0.5) * 0.009
    return round(center[0] + longitude_offset, 6), round(center[1] + latitude_offset, 6)


def geocode_address(address, city="大连市", timeout=6):
    """调用高德 Web 服务，把真实地址转换为 GCJ-02 坐标。"""
    if not MAP_GEOCODE_ENABLED or not AMAP_WEB_SERVICE_KEY or not address.strip():
        return None
    query = urlencode({
        "key": AMAP_WEB_SERVICE_KEY,
        "address": address,
        "city": city,
        "output": "JSON",
    })
    request = Request(
        f"{AMAP_GEOCODE_URL}?{query}",
        headers={"User-Agent": "ai-house-rental/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if payload.get("status") != "1" or not payload.get("geocodes"):
        return None
    location = payload["geocodes"][0].get("location", "")
    try:
        longitude, latitude = (float(value) for value in location.split(",", 1))
    except (TypeError, ValueError):
        return None
    return round(longitude, 6), round(latitude, 6)


def locate_house(house, prefer_geocode=True):
    """更新房源坐标，返回本次使用的来源。"""
    coordinates = None
    source = ""
    if prefer_geocode:
        district = "" if house.district in house.address else house.district
        full_address = "辽宁省大连市%s%s" % (district, house.address)
        coordinates = geocode_address(full_address)
        if coordinates:
            source = "amap"
    if not coordinates:
        coordinates = approximate_coordinates(
            house.district, house.community, house.house_no or house.id)
        if coordinates:
            source = "approximate"
    if coordinates:
        house.lng, house.lat = coordinates
        house.location_source = source
        house.geocoded_at = datetime.now()
    return source

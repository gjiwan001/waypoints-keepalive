#!/usr/bin/env python3
"""
build.py — Rebuild P_KR, P_JP, P_TW, P_KG in korea_map.html from korea_saved_places.csv.

Geocoding:
  Korea  → Naver Local Search API (NAVER_CLIENT_ID + NAVER_CLIENT_SECRET)
  Others → Nominatim / OpenStreetMap (no key, 1 req/s)

Cache: geocache.json stores {lat, lng, source, approx?} per place name.
  - Real geocoded entries are never re-tried.
  - Approx entries are re-tried only if they haven't been stable for N runs.
"""

import csv, json, re, time, urllib.request, urllib.parse
import os, sys, hashlib, math, datetime

BASE            = os.path.dirname(os.path.abspath(__file__))
CSV_FILE        = os.path.join(BASE, 'korea_saved_places.csv')
HTML_FILE       = os.path.join(BASE, 'korea_map.html')
CACHE_FILE      = os.path.join(BASE, 'geocache.json')
LAST_BUILD_FILE = os.path.join(BASE, 'last_build.json')
REMOVED_FILE    = os.path.join(BASE, 'removed_places.json')

# Country → JS array name + ZONES name
COUNTRY_ARRAYS = {
    'Korea':      ('P_KR', 'ZONES_KR'),
    'Japan':      ('P_JP', 'ZONES_JP'),
    'Taiwan':     ('P_TW', 'ZONES_TW'),
    'Kyrgyzstan': ('P_KG', 'ZONES_KG'),
}

COUNTRY_DEFAULTS = {
    'Korea':      {'Seoul': (37.5665, 126.9780), 'Busan': (35.1796, 129.0756),
                   'Jeju':  (33.4890, 126.4983), '_': (37.5665, 126.9780)},
    'Japan':      {'Tokyo': (35.6762, 139.6503), 'Osaka': (34.6937, 135.5023),
                   'Kyoto': (35.0116, 135.7681), '_': (36.2048, 138.2529)},
    'Taiwan':     {'Taipei': (25.0330, 121.5654), '_': (23.6978, 120.9605)},
    'Kyrgyzstan': {'Bishkek': (42.8746, 74.5698), '_': (41.2044, 74.7661)},
    '_':          {'_': (20.0000, 0.0000)},
}

CAT_MAP = {
    'DayTrip': 'DayTrip', 'Day Trip': 'DayTrip', 'Nature / Day Trip': 'DayTrip',
    'Island': 'DayTrip', 'City': 'DayTrip',
    'Clothing Store': 'Shopping', 'Fashion Brand / Store': 'Shopping',
    'Fashion Brand/Store': 'Shopping', 'Shopping': 'Shopping',
    'Shopping Tip': 'Shopping', 'Market': 'Shopping', 'Department Store': 'Shopping',
    'Food': 'Food', 'Café / Bakery': 'Food', 'Café/Bakery': 'Food',
    'Café': 'Food', 'Bakery': 'Food', 'Restaurant': 'Food',
    'Culture': 'Culture', 'Landmark': 'Culture', 'Cultural Site': 'Culture',
    'Museum': 'Culture', 'Jjimjilbang': 'Culture', 'Jjimjilbang / Spa': 'Culture',
    'Hidden Gem': 'Culture', 'Event / Festival': 'Culture', 'Nightlife': 'Culture',
    'Neighbourhood': 'Neighbourhood',
    'Skincare': 'Skincare', 'Skincare Clinic': 'Skincare', 'Wellness/Clinic': 'Skincare',
    'Wellness / Clinic': 'Skincare', 'Wellness': 'Skincare', 'Beauty': 'Skincare',
    'Beauty / Hair': 'Skincare', 'Pharmacy / Skincare': 'Skincare',
    'Transport': 'Transport', 'Accommodation': 'Accommodation',
    'Accommodation Platform': 'Accommodation', 'Tip': 'Culture', 'Resource': 'Culture',
}


# ── geocoders ──────────────────────────────────────────────────────────────────

def _fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def geocode_naver(name, city, client_id, client_secret):
    query = f"{name} {city}".strip() if city else name
    url = 'https://openapi.naver.com/v1/search/local.json?' + urllib.parse.urlencode(
        {'query': query, 'display': 1})
    try:
        data = _fetch(url, {'X-Naver-Client-Id': client_id, 'X-Naver-Client-Secret': client_secret})
        items = data.get('items', [])
        if items:
            mapx, mapy = float(items[0]['mapx']), float(items[0]['mapy'])
            if abs(mapy) > 90:  mapy /= 1e7
            if abs(mapx) > 180: mapx /= 1e7
            return mapy, mapx
    except Exception as e:
        print(f"  ⚠ Naver error for {name!r}: {e}", file=sys.stderr)
    return None

def geocode_nominatim(name, city, country_name):
    queries = []
    suffix = f", {country_name}" if country_name else ''
    city_part = f", {city}" if city else ''
    queries.append(f"{name}{city_part}{suffix}")
    kr = re.search(r'\(([^)]+)\)', name)
    if kr:
        queries.append(f"{kr.group(1)}{city_part}{suffix}")
    clean = re.sub(r'\s*\([^)]*\)', '', name).strip()
    if clean and clean != name:
        queries.append(f"{clean}{city_part}{suffix}")

    countrycodes = {'Korea': 'kr', 'Japan': 'jp', 'Taiwan': 'tw', 'Kyrgyzstan': 'kg'}.get(country_name, '')
    for query in queries:
        params = {'q': query, 'format': 'json', 'limit': 1}
        if countrycodes:
            params['countrycodes'] = countrycodes
        url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(params)
        try:
            results = _fetch(url, {'User-Agent': 'waypoints-map-builder/1.0 (github.com/gjiwan001/waypoints)'})
            if results:
                time.sleep(1.1)
                return float(results[0]['lat']), float(results[0]['lon'])
        except Exception as e:
            print(f"  ⚠ Nominatim error for {query!r}: {e}", file=sys.stderr)
        time.sleep(1.1)
    return None

def country_default(country, city):
    cmap = COUNTRY_DEFAULTS.get(country, COUNTRY_DEFAULTS['_'])
    for key, coords in cmap.items():
        if key != '_' and key.lower() in city.lower():
            return coords
    return cmap['_']


# ── cache ──────────────────────────────────────────────────────────────────────

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

def load_last_build():
    if os.path.exists(LAST_BUILD_FILE):
        with open(LAST_BUILD_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_last_build(data):
    with open(LAST_BUILD_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_removed():
    if os.path.exists(REMOVED_FILE):
        with open(REMOVED_FILE, encoding='utf-8') as f:
            return json.load(f)
    return []

def save_removed(data):
    with open(REMOVED_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def seed_from_html(cache):
    """Seed cache with coords already in HTML — only for entries not yet cached."""
    with open(HTML_FILE, encoding='utf-8') as f:
        content = f.read()
    added = 0
    for m in re.finditer(r'\{n:"([^"]+)"[^}]*lat:(-?[\d.]+),lng:(-?[\d.]+)', content):
        name = m.group(1)
        if name not in cache:
            lat, lng = float(m.group(2)), float(m.group(3))
            # Skip Seoul placeholder
            if not (abs(lat - 37.5665) < 0.0002 and abs(lng - 126.978) < 0.0002):
                cache[name] = {'lat': lat, 'lng': lng, 'source': 'html', 'approx': True}
                added += 1
    return added


# ── formatting ─────────────────────────────────────────────────────────────────

def map_cat(raw):
    return CAT_MAP.get(raw.strip(), 'Culture')

def clean_area(city):
    city = city.split(',')[0].strip()
    if city.endswith(' Seoul') and city != 'Seoul':
        city = city[:-6].strip()
    return city or 'Seoul'

def parse_tags(raw_tags, name, cat, desc):
    """Merge CSV tags with auto-detected ones."""
    tags = []
    if raw_tags:
        tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
    # Auto-add category tags if not already present
    d = desc.lower(); c = cat.lower()
    if any(x in c for x in ('food','café','restaurant','bakery')) and 'food' not in tags:
        tags.append('food')
    if any(x in c for x in ('shopping','store','market','fashion','clothing')) and 'shopping' not in tags:
        tags.append('shopping')
    if any(x in c for x in ('skincare','clinic','wellness','beauty','pharmacy')) and 'wellness' not in tags:
        tags.append('wellness')
    if 'neighbourhood' in c and 'stay' not in tags:
        tags.append('stay')
    if any(x in c for x in ('daytrip','day trip','nature','island')) and 'daytrip' not in tags:
        tags.append('daytrip')
    if ('★' in name or 'top pick' in d) and '★ top pick' not in tags:
        tags.append('★ top pick')
    return tags

def js_str(s):
    return (str(s)
            .replace('\\', '\\\\')
            .replace('"', '\\"')
            .replace('\n', ' ')
            .replace('\r', '')
            .replace('</', '<\\/'))

def fmt_entry(e):
    tags_js = ', '.join(f'"{js_str(t)}"' for t in e['tags'])
    optional = ''
    if e.get('hours'): optional += f',hours:"{js_str(e["hours"])}"'
    if e.get('price'): optional += f',price:"{js_str(e["price"])}"'
    if e.get('url'):   optional += f',url:"{js_str(e["url"])}"'
    return (
        f'  {{n:"{js_str(e["name"])}",'
        f'c:"{js_str(map_cat(e["cat"]))}",'
        f'lat:{e["lat"]:.6f},'
        f'lng:{e["lng"]:.6f},'
        f'area:"{js_str(clean_area(e["city"]))}",'
        f'd:"{js_str(e["desc"])}",'
        f'tags:[{tags_js}],'
        f'day:"{js_str(e["day"])}"{optional}}}'
    )

def fmt_removed_entry(e):
    tags_js = ', '.join(f'"{js_str(t)}"' for t in e.get('tags', []))
    return (
        f'  {{n:"{js_str(e["name"])}",'
        f'c:"{js_str(map_cat(e["cat"]))}",'
        f'lat:{e["lat"]:.6f},'
        f'lng:{e["lng"]:.6f},'
        f'area:"{js_str(clean_area(e["city"]))}",'
        f'd:"{js_str(e.get("desc",""))}",'
        f'tags:[{tags_js}],'
        f'day:"{js_str(e.get("day","Flexible"))}",'
        f'removedAt:"{js_str(e.get("removedAt",""))}"}}'
    )

def _day_sort_key(d):
    m = re.search(r'\d+', d)
    return (int(m.group()) if m else 999, d)

def resolve_collisions(entries):
    """Golden-angle spiral to guarantee unique map coords."""
    used = set()
    def key(lat, lng): return (round(lat * 10000), round(lng * 10000))
    result = []
    for item in entries:
        k = key(item['lat'], item['lng'])
        if k not in used:
            used.add(k)
        else:
            seed = int(hashlib.md5(item['name'].encode()).hexdigest(), 16)
            angle0 = math.radians(seed % 360)
            step = 0.0002
            for i in range(1, 500):
                angle = angle0 + i * 2.39996
                dist  = step * i
                nlat  = round(item['lat'] + dist * math.sin(angle), 6)
                nlng  = round(item['lng'] + dist * math.cos(angle), 6)
                nk    = key(nlat, nlng)
                if nk not in used:
                    item = dict(item)
                    item['lat'], item['lng'] = nlat, nlng
                    used.add(nk)
                    break
        result.append(item)
    return result


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    naver_id     = os.getenv('NAVER_CLIENT_ID', '').strip()
    naver_secret = os.getenv('NAVER_CLIENT_SECRET', '').strip()
    use_naver    = bool(naver_id and naver_secret)
    print(f"Naver API: {'enabled' if use_naver else 'not configured — using Nominatim'}")

    # 1. Load cache; seed from HTML
    cache = load_cache()
    seeded = seed_from_html(cache)
    if seeded:
        print(f"Seeded {seeded} coords from HTML")
        save_cache(cache)

    # 2. Read + deduplicate CSV
    places = {}
    with open(CSV_FILE, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            name    = row.get('Place Name', '').strip()
            country = row.get('Country', '').strip()
            if not name or not country:
                continue
            # Skip removed entries (build.py on Claude Code adds this column)
            if row.get('removedAt', '').strip():
                continue
            cat   = row.get('Category', '').strip()
            city  = row.get('Neighbourhood / City', '').strip()
            desc  = row.get('Description / Notes', '').strip()
            day   = row.get('Day', '').strip()
            tags  = row.get('Tags', '').strip()
            hours = row.get('Hours', '').strip()
            price = row.get('Price', '').strip()
            url   = row.get('URL', '').strip()

            if name not in places:
                places[name] = {
                    'country': country, 'cat': cat, 'city': city,
                    'descs': [desc] if desc else [],
                    'days':  {day} if day else set(),
                    'tags': tags, 'hours': hours, 'price': price, 'url': url,
                }
            else:
                p = places[name]
                if desc and desc not in p['descs']:
                    p['descs'].append(desc)
                if day: p['days'].add(day)
                if p['cat'] in ('Culture', 'Tip', 'Resource', '') and cat not in ('', 'Tip', 'Resource'):
                    p['cat'] = cat
                if not p['city'] and city: p['city'] = city
                if not p['tags'] and tags: p['tags'] = tags

    print(f"Loaded {len(places)} unique places from CSV")

    # 3. Geocode missing/approx entries
    geocoded = approx = 0
    changed = False
    for name, info in places.items():
        cached = cache.get(name)
        # Only retry if not in cache OR if it's approx and has been approx for <3 builds
        if cached and not cached.get('approx'):
            continue
        if cached and cached.get('approx') and cached.get('approx_stable', 0) >= 3:
            continue  # stable approx — stop retrying

        country, city = info['country'], info['city']
        print(f"Geocoding ({country}): {name!r} …", end=' ', flush=True)

        coords = None
        if use_naver and country == 'Korea':
            coords = geocode_naver(name, city, naver_id, naver_secret)
            if coords: source = 'naver'

        if not coords:
            coords = geocode_nominatim(name, city, country)
            if coords: source = 'nominatim'

        if coords:
            cache[name] = {'lat': coords[0], 'lng': coords[1], 'source': source}
            geocoded += 1
            print(f"→ {coords[0]:.4f}, {coords[1]:.4f}  [{source}]")
        else:
            base = country_default(country, city)
            seed = int(hashlib.md5(name.encode()).hexdigest(), 16)
            angle = (seed % 3600) / 3600 * 2 * math.pi
            dist  = (((seed >> 12) % 1000) / 1000) * 0.018
            jlat  = round(base[0] + dist * math.sin(angle), 6)
            jlng  = round(base[1] + dist * math.cos(angle), 6)
            prev_stable = cached.get('approx_stable', 0) if cached else 0
            cache[name] = {'lat': jlat, 'lng': jlng, 'source': 'approx',
                           'approx': True, 'approx_stable': prev_stable + 1}
            approx += 1
            print(f"→ approx near {city or country}")

        changed = True

    if changed:
        save_cache(cache)
        if geocoded or approx:
            print(f"Geocoded {geocoded} new, {approx} still approx")

    # 4. Build per-country entry lists
    all_entries = {country: [] for country in COUNTRY_ARRAYS}
    for name, info in places.items():
        country = info['country']
        if country not in COUNTRY_ARRAYS:
            continue
        c    = cache.get(name, {'lat': country_default(country, info['city'])[0],
                                 'lng': country_default(country, info['city'])[1]})
        desc = info['descs'][0] if info['descs'] else ''
        days = sorted(info['days'], key=_day_sort_key)
        day  = 'Flexible' if not days else (days[0] if len(days) == 1 else ', '.join(days))
        all_entries[country].append({
            'name': name, 'country': country, 'cat': info['cat'],
            'lat': c['lat'], 'lng': c['lng'],
            'city': info['city'], 'desc': desc,
            'tags': parse_tags(info.get('tags', ''), name, info['cat'], desc),
            'day': day, 'hours': info.get('hours', ''),
            'price': info.get('price', ''), 'url': info.get('url', ''),
        })

    # 5. Detect removed Korea places
    today = datetime.date.today().isoformat()
    last_build = load_last_build()
    old_korea  = {e['name']: e for e in last_build.get('Korea', [])}
    new_korea_names = {e['name'] for e in all_entries.get('Korea', [])}
    newly_removed = [old_korea[n] for n in old_korea if n not in new_korea_names]

    archive = [e for e in load_removed() if e['name'] not in new_korea_names]
    archived_names = {e['name'] for e in archive}
    for e in newly_removed:
        if e['name'] not in archived_names:
            e_copy = dict(e); e_copy['removedAt'] = today
            archive.append(e_copy)
            print(f"  ⚠ Removed: {e['name']!r}")
    save_removed(archive)
    if archive:
        print(f"Removed places archive: {len(archive)} total")

    save_last_build({'Korea': all_entries.get('Korea', [])})

    # 6. Resolve collisions + format JS for each country
    with open(HTML_FILE, encoding='utf-8') as f:
        html = f.read()

    for country, (arr_name, zones_name) in COUNTRY_ARRAYS.items():
        entries_data = resolve_collisions(all_entries.get(country, []))
        entries = [fmt_entry(e) for e in entries_data]
        new_block = f'const {arr_name}=[\n' + ',\n'.join(entries) + '\n];'

        html, n = re.subn(
            rf'const {arr_name}=\[[\s\S]*?\n\];',
            lambda b=new_block: b,
            html
        )
        if n == 0:
            print(f"WARNING: {arr_name} block not found in HTML", file=sys.stderr)
        else:
            print(f"✓ Wrote {len(entries)} {country} places to {arr_name}")

    # 7. Update P_REMOVED
    if archive:
        new_removed = 'const P_REMOVED=[\n' + ',\n'.join(fmt_removed_entry(e) for e in archive) + '\n];'
    else:
        new_removed = 'const P_REMOVED=[];'
    html, n2 = re.subn(r'const P_REMOVED=\[[\s\S]*?\];', lambda b=new_removed: b, html)
    if n2 == 0:
        print("WARNING: P_REMOVED block not found", file=sys.stderr)

    # 8. Atomic write
    tmp = HTML_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(html)
    os.replace(tmp, HTML_FILE)

    if archive:
        print(f"✓ Wrote {len(archive)} removed places to P_REMOVED")


if __name__ == '__main__':
    main()

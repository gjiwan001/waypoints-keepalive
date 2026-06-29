#!/usr/bin/env python3
"""
build.py — Rebuild the P_KR array in korea_map.html from korea_saved_places.csv.

Geocoding strategy:
  Korea  → Naver Local Search API  (NAVER_CLIENT_ID + NAVER_CLIENT_SECRET env vars)
  Others → Nominatim / OpenStreetMap (no key required, 1 req/s limit)

Set env vars locally or as GitHub Actions secrets. If Naver creds are absent,
Korea entries fall back to Nominatim automatically.

Cache: geocache.json stores {lat, lng, source} per place name. Entries marked
"approx" are re-tried on the next run when a better geocoder is available.
"""

import csv, json, re, time, urllib.request, urllib.parse, os, sys, hashlib, math, datetime

BASE            = os.path.dirname(os.path.abspath(__file__))
CSV_FILE        = os.path.join(BASE, 'korea_saved_places.csv')
HTML_FILE       = os.path.join(BASE, 'korea_map.html')
CACHE_FILE      = os.path.join(BASE, 'geocache.json')
LAST_BUILD_FILE = os.path.join(BASE, 'last_build.json')
REMOVED_FILE    = os.path.join(BASE, 'removed_places.json')

# Country-level fallback coordinates
COUNTRY_DEFAULTS = {
    'Korea':  {'Seoul':  (37.5665, 126.9780),
               'Busan':  (35.1796, 129.0756),
               'Jeju':   (33.4890, 126.4983),
               'Pohang': (36.0190, 129.3435),
               '_':      (37.5665, 126.9780)},
    'Japan':  {'Tokyo':  (35.6762, 139.6503),
               'Osaka':  (34.6937, 135.5023),
               'Kyoto':  (35.0116, 135.7681),
               '_':      (36.2048, 138.2529)},
    'Taiwan': {'Taipei': (25.0330, 121.5654),
               '_':      (23.6978, 120.9605)},
    '_':      {'_':      (20.0000,  0.0000)},
}

# CSV category → HTML CC object key
CAT_MAP = {
    'DayTrip': 'DayTrip', 'Day Trip': 'DayTrip',
    'Nature / Day Trip': 'DayTrip', 'Island': 'DayTrip', 'City': 'DayTrip',
    'Clothing Store': 'Shopping', 'Fashion Brand / Store': 'Shopping',
    'Fashion Brand/Store': 'Shopping', 'Shopping': 'Shopping',
    'Shopping Tip': 'Shopping', 'Market': 'Shopping', 'Department Store': 'Shopping',
    'Food': 'Food', 'Café / Bakery': 'Food', 'Café/Bakery': 'Food',
    'Café': 'Food', 'Bakery': 'Food', 'Restaurant': 'Food',
    'Culture': 'Culture', 'Landmark': 'Culture', 'Cultural Site': 'Culture',
    'Museum': 'Culture', 'Jjimjilbang': 'Culture', 'Jjimjilbang / Spa': 'Culture',
    'Hidden Gem': 'Culture', 'Event / Festival': 'Culture',
    'Neighbourhood': 'Neighbourhood',
    'Skincare': 'Skincare', 'Skincare Clinic': 'Skincare',
    'Wellness/Clinic': 'Skincare', 'Wellness / Clinic': 'Skincare',
    'Wellness': 'Skincare', 'Beauty': 'Skincare', 'Beauty / Hair': 'Skincare',
    'Pharmacy / Skincare': 'Skincare',
    'Transport': 'Transport',
    'Accommodation': 'Accommodation', 'Accommodation Platform': 'Accommodation',
    'Tip': 'Culture', 'Resource': 'Culture',
}


# ── geocoders ─────────────────────────────────────────────────────────────────

def _fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def geocode_naver(name, city, client_id, client_secret):
    """Naver Local Search — best for Korean business/place names."""
    query = f"{name} {city}".strip() if city else name
    url = 'https://openapi.naver.com/v1/search/local.json?' + urllib.parse.urlencode(
        {'query': query, 'display': 1}
    )
    try:
        data = _fetch(url, {
            'X-Naver-Client-Id': client_id,
            'X-Naver-Client-Secret': client_secret,
        })
        items = data.get('items', [])
        if items:
            mapx = float(items[0]['mapx'])
            mapy = float(items[0]['mapy'])
            # Naver may return WGS84 * 1e7 — use abs() to handle negative coords too
            if abs(mapy) > 90:   mapy /= 1e7
            if abs(mapx) > 180:  mapx /= 1e7
            return mapy, mapx   # (lat, lng)
    except Exception as e:
        print(f"  ⚠ Naver error for '{name}': {e}", file=sys.stderr)
    return None

def geocode_nominatim(name, city, country_name):
    """Nominatim / OpenStreetMap — universal fallback, 1 req/s."""
    # Build a few query variants: full name, Korean parenthetical, clean name
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

    countrycodes = {'Korea': 'kr', 'Japan': 'jp', 'Taiwan': 'tw'}.get(country_name, '')

    for query in queries:
        params = {'q': query, 'format': 'json', 'limit': 1}
        if countrycodes:
            params['countrycodes'] = countrycodes
        url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(params)
        try:
            results = _fetch(url, {'User-Agent': 'waypoints-map-builder/1.0 (github.com/gjiwan001/waypoints)'})
            if results:
                time.sleep(1.1)  # respect 1 req/s policy before returning
                return float(results[0]['lat']), float(results[0]['lon'])
        except Exception as e:
            print(f"  ⚠ Nominatim error for '{query}': {e}", file=sys.stderr)
        time.sleep(1.1)
    return None

def country_default(country, city):
    """Return a fallback (lat, lng) for places we truly cannot geocode."""
    country_map = COUNTRY_DEFAULTS.get(country, COUNTRY_DEFAULTS['_'])
    for key, coords in country_map.items():
        if key != '_' and key.lower() in city.lower():
            return coords
    return country_map['_']


# ── cache helpers ─────────────────────────────────────────────────────────────

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
    """Seed cache from any hand-placed coords already in the HTML.

    Marked approx so a real geocoder can improve them on the next run.
    Regex handles negative coords (southern hemisphere / west of prime meridian).
    """
    with open(HTML_FILE, encoding='utf-8') as f:
        content = f.read()
    added = 0
    for m in re.finditer(r'\{n:"([^"]+)"[^}]*lat:(-?[\d.]+),lng:(-?[\d.]+)', content):
        name = m.group(1)
        lat, lng = float(m.group(2)), float(m.group(3))
        if name not in cache:
            # Skip Seoul default — it's a placeholder, not a real coord
            if not (abs(lat - 37.5665) < 0.0002 and abs(lng - 126.978) < 0.0002):
                # Mark approx so a real geocoder can improve it next run
                cache[name] = {'lat': lat, 'lng': lng, 'source': 'html', 'approx': True}
                added += 1
    return added


# ── formatting helpers ────────────────────────────────────────────────────────

def map_cat(raw):
    return CAT_MAP.get(raw.strip(), 'Culture')

def clean_area(city):
    city = city.split(',')[0].strip()
    if city.endswith(' Seoul') and city != 'Seoul':
        city = city[:-6].strip()
    return city or 'Seoul'

def make_tags(name, cat, desc):
    tags = []
    d = desc.lower(); c = cat.lower()
    if any(x in c for x in ('food', 'café', 'restaurant', 'bakery')): tags.append('food')
    if any(x in c for x in ('shopping', 'store', 'market', 'fashion', 'clothing')): tags.append('shopping')
    if any(x in c for x in ('skincare', 'clinic', 'wellness', 'beauty', 'pharmacy')): tags.append('wellness')
    if 'neighbourhood' in c: tags.append('stay')
    if any(x in c for x in ('daytrip', 'day trip', 'nature', 'island')): tags.append('daytrip')
    if '★' in name or 'top pick' in d or 'best' in d[:60]: tags.append('★ top pick')
    if 'free' in d: tags.append('free')
    if 'english' in d: tags.append('english friendly')
    return tags

def js_str(s):
    return (s.replace('\\', '\\\\')
             .replace('"', '\\"')
             .replace('\n', ' ')
             .replace('\r', '')
             .replace('</', '<\\/'))  # prevent </script> from breaking the HTML block

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
    """Sort day strings numerically (e.g. 'Day 10' before 'Day 2' would be wrong)."""
    m = re.search(r'\d+', d)
    return (int(m.group()) if m else 999, d)

def resolve_collisions(entries):
    """Golden-angle spiral: guarantee every entry has a unique map coord key."""
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
            resolved = False
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
                    resolved = True
                    break
            if not resolved:
                print(f"WARNING: coord collision unresolved for {item['name']!r}", file=sys.stderr)
        result.append(item)
    return result


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    naver_id     = os.getenv('NAVER_CLIENT_ID', '').strip()
    naver_secret = os.getenv('NAVER_CLIENT_SECRET', '').strip()
    use_naver    = bool(naver_id and naver_secret)
    if use_naver:
        print("Naver API: enabled (Korea entries will use Naver Local Search)")
    else:
        print("Naver API: not configured — falling back to Nominatim for Korea")
        print("  Set NAVER_CLIENT_ID and NAVER_CLIENT_SECRET to enable")

    # 1. Load geocache; seed from hand-placed HTML coords
    cache = load_cache()
    seeded = seed_from_html(cache)
    if seeded:
        print(f"Seeded {seeded} coords from existing HTML")
        save_cache(cache)

    # 2. Read + deduplicate CSV by Place Name
    # utf-8-sig strips the Excel BOM (﻿) if present
    places = {}   # name → {country, cat, city, descs[], days{}}
    with open(CSV_FILE, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            name    = row.get('Place Name', '').strip()
            country = row.get('Country', '').strip()
            if not name or not country:
                continue
            cat   = row.get('Category', '').strip()
            city  = row.get('Neighbourhood / City', '').strip()
            desc  = row.get('Description / Notes', '').strip()
            day   = row.get('Day', '').strip()
            hours = row.get('Hours', '').strip()
            price = row.get('Price', '').strip()
            url   = row.get('URL', '').strip()

            if name not in places:
                places[name] = {
                    'country': country, 'cat': cat, 'city': city,
                    'descs': [desc] if desc else [],
                    'days':  {day} if day else set(),
                    'hours': hours, 'price': price, 'url': url,
                }
            else:
                p = places[name]
                if desc and desc not in p['descs']:
                    p['descs'].append(desc)
                if day:
                    p['days'].add(day)
                if p['cat'] in ('Culture', 'Tip', 'Resource', '') and cat not in ('', 'Tip', 'Resource'):
                    p['cat'] = cat
                if not p['city'] and city:
                    p['city'] = city
                if not p['hours'] and hours:
                    p['hours'] = hours
                if not p['price'] and price:
                    p['price'] = price
                if not p['url'] and url:
                    p['url'] = url

    print(f"Loaded {len(places)} unique places from CSV")

    # 3. Geocode entries not yet in cache (or previously marked approx)
    geocoded = approx = 0
    changed = False

    for name, info in places.items():
        cached = cache.get(name)
        country = info['country']
        city    = info['city']

        # Retry any approx entry — geocoders may succeed now (Naver added, transient failure, etc.)
        should_retry = cached is None or cached.get('approx')
        if not should_retry:
            continue

        print(f"Geocoding ({country}): {name!r} …", end=' ', flush=True)

        coords = None
        source = None

        if use_naver and country == 'Korea':
            coords = geocode_naver(name, city, naver_id, naver_secret)
            if coords:
                source = 'naver'

        if not coords:
            coords = geocode_nominatim(name, city, country)
            if coords:
                source = 'nominatim'

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
            cache[name] = {'lat': jlat, 'lng': jlng, 'source': 'approx', 'approx': True}
            approx += 1
            print(f"→ approx near {city or country}")

        changed = True

    if changed:
        save_cache(cache)
        print(f"Geocoded {geocoded} places ({approx} still approx); cache updated")

    # 4. Build Korea entries list from geocoded places
    # Only include places whose country maps to P_KR (Korea for now)
    entries_data = []
    for name, info in places.items():
        if info['country'] != 'Korea':
            continue
        c    = cache.get(name, {'lat': 37.5665, 'lng': 126.9780})
        desc = info['descs'][0] if info['descs'] else ''
        # Sort day values numerically so "Day 10" comes after "Day 9", not before "Day 2"
        days = sorted(info['days'], key=_day_sort_key)
        if not days:
            day = 'Flexible'
        elif len(days) == 1:
            day = days[0]
        else:
            # Comma-separated discrete days — JS placeMatchesDay handles this format
            day = ', '.join(days)
        entries_data.append({
            'name': name, 'country': info['country'], 'cat': info['cat'],
            'lat': c['lat'], 'lng': c['lng'],
            'city': info['city'], 'desc': desc,
            'tags': make_tags(name, info['cat'], desc), 'day': day,
            'hours': info.get('hours', ''), 'price': info.get('price', ''), 'url': info.get('url', ''),
        })

    if not entries_data:
        print("WARNING: no Korea entries found in CSV — P_KR will be empty", file=sys.stderr)

    # 5. Detect removed places (compare against last build)
    today = datetime.date.today().isoformat()
    last_build  = load_last_build()
    old_entries = {e['name']: e for e in last_build.get('Korea', [])}
    new_names   = {e['name'] for e in entries_data}

    newly_removed = [old_entries[n] for n in old_entries if n not in new_names]

    archive          = load_removed()
    archived_names   = {e['name'] for e in archive}
    # Drop anything that came back in the new CSV
    archive = [e for e in archive if e['name'] not in new_names]
    # Add newly removed places
    for e in newly_removed:
        if e['name'] not in archived_names:
            e_copy = dict(e)
            e_copy['removedAt'] = today
            archive.append(e_copy)
            print(f"  ⚠ Removed from CSV: {e['name']!r}")

    save_removed(archive)
    if archive:
        print(f"Removed places archive: {len(archive)} total")

    # Save current Korea entries as the new last_build snapshot
    save_last_build({'Korea': entries_data})

    # 6. Resolve collisions and format JS
    entries_data = resolve_collisions(entries_data)
    entries = [fmt_entry(e) for e in entries_data]

    # 7. Replace P_KR and P_REMOVED blocks in HTML (atomic write via temp file)
    new_pkr_block = 'const P_KR=[\n' + ',\n'.join(entries) + '\n];'
    if archive:
        new_removed_block = 'const P_REMOVED=[\n' + ',\n'.join(fmt_removed_entry(e) for e in archive) + '\n];'
    else:
        new_removed_block = 'const P_REMOVED=[];'

    with open(HTML_FILE, encoding='utf-8') as f:
        html = f.read()

    # Use lambdas so re doesn't interpret backslashes in replacement strings as group refs
    html_new, n = re.subn(r'const P_KR=\[[\s\S]*?\n\];', lambda _: new_pkr_block, html)
    if n == 0:
        print("ERROR: 'const P_KR=[...];' block not found in HTML", file=sys.stderr)
        sys.exit(1)

    html_new, n2 = re.subn(r'const P_REMOVED=\[[\s\S]*?\];', lambda _: new_removed_block, html_new)
    if n2 == 0:
        print("WARNING: 'const P_REMOVED=[...];' block not found in HTML — removed-places UI won't update", file=sys.stderr)

    # Write atomically — avoids a corrupt HTML if the process is killed mid-write
    tmp = HTML_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(html_new)
    os.replace(tmp, HTML_FILE)

    print(f"✓ Wrote {len(entries)} Korea places to P_KR in {os.path.basename(HTML_FILE)}")
    if archive:
        print(f"✓ Wrote {len(archive)} removed places to P_REMOVED")


if __name__ == '__main__':
    main()

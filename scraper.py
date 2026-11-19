"""Busca gratuita de estabelecimentos em dados abertos do OpenStreetMap."""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
import unicodedata
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx

from classifier import extract_public_contacts, merge_contacts, normalize_url


NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OSM_USER_AGENT = os.getenv(
    "OSM_USER_AGENT",
    "ProspectorLocal/2.0 (aplicacao de pesquisa local; operador responsavel)",
)
_GEOCODE_CACHE: dict[str, tuple[float, dict | None]] = {}
_GEOCODE_LOCK = asyncio.Lock()
_LAST_NOMINATIM_REQUEST = 0.0
_OVERPASS_LOCK = asyncio.Lock()
_LAST_OVERPASS_REQUEST = 0.0
_DDG_LOCK = asyncio.Lock()
_LAST_DDG_REQUEST = 0.0
_OVERPASS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_BUSINESS_KEYS = ("shop", "amenity", "craft", "office", "healthcare", "leisure", "tourism", "sport")


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").casefold())
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


async def geocode_location(city: str, country: str, timeout: float = 8.0) -> dict | None:
    """Resolve uma cidade uma vez; resultados são cacheados por 24h para reduzir tráfego."""
    global _LAST_NOMINATIM_REQUEST
    query = ", ".join(part.strip() for part in (city, country) if part and part.strip())
    cache_key = _normalize(query)
    if not query:
        return None
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    async with _GEOCODE_LOCK:
        cached = _GEOCODE_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        delay = 1.05 - (time.monotonic() - _LAST_NOMINATIM_REQUEST)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(timeout, 4.0)),
                headers={"User-Agent": OSM_USER_AGENT},
            ) as client:
                response = await client.get(
                    NOMINATIM_URL,
                    params={"q": query, "format": "jsonv2", "limit": 1},
                )
                response.raise_for_status()
                data = response.json()
            _LAST_NOMINATIM_REQUEST = time.monotonic()
            if not data:
                _GEOCODE_CACHE[cache_key] = (time.monotonic() + 1800, None)
                return None
            record = data[0]
            result = {
                "lat": float(record["lat"]),
                "lon": float(record["lon"]),
                "boundingbox": [float(item) for item in record.get("boundingbox", [])],
                "display_name": str(record.get("display_name") or query),
                "osm_type": record.get("osm_type"),
                "osm_id": record.get("osm_id"),
            }
            _GEOCODE_CACHE[cache_key] = (time.monotonic() + 86400, result)
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            _LAST_NOMINATIM_REQUEST = time.monotonic()
            return None


async def geocode_city(city: str, country: str, timeout: float = 8.0) -> tuple[float, float] | None:
    """Compatibilidade com a interface antiga: retorna apenas o centro da cidade."""
    location = await geocode_location(city, country, timeout)
    return (location["lat"], location["lon"]) if location else None


def _bbox_selector(bbox: list[float]) -> str:
    # Nominatim: sul, norte, oeste, leste. Overpass: sul, oeste, norte, leste.
    if len(bbox) != 4:
        raise ValueError("A cidade não possui limites geográficos válidos.")
    south, north, west, east = bbox
    return f"{south},{west},{north},{east}"


def _business_query(selectors: list[str], timeout_seconds: int = 120) -> str:
    statements: list[str] = []
    for selector in selectors:
        for key in _BUSINESS_KEYS:
            statements.append(f'nwr{selector}["name"]["{key}"];')
    return f"[out:json][timeout:{timeout_seconds}];({''.join(statements)});out center tags;"


async def _overpass(query: str, *, timeout: float = 150.0) -> list[dict]:
    global _LAST_OVERPASS_REQUEST
    key = str(hash(query))
    cached = _OVERPASS_CACHE.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    async with _OVERPASS_LOCK:
        delay = 1.2 - (time.monotonic() - _LAST_OVERPASS_REQUEST)
        if delay > 0:
            await asyncio.sleep(delay)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=12.0),
            headers={"User-Agent": OSM_USER_AGENT},
        ) as client:
            response = await client.post(OVERPASS_URL, data={"data": query})
            _LAST_OVERPASS_REQUEST = time.monotonic()
            if response.status_code == 429:
                raise RuntimeError("O serviço público do OpenStreetMap limitou consultas. Aguarde alguns minutos e tente novamente.")
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Overpass retornou uma resposta inválida.") from exc
    elements = payload.get("elements", [])
    _OVERPASS_CACHE[key] = (time.monotonic() + 1800, elements)
    return elements


async def _neighbourhood_centres(bbox: list[float]) -> list[tuple[float, float]]:
    selector = _bbox_selector(bbox)
    query = (
        f'[out:json][timeout:40];(nwr["place"~"^(suburb|quarter|neighbourhood|district)$"]'
        f'["name"]({selector}););out center tags;'
    )
    try:
        elements = await _overpass(query, timeout=55)
    except (httpx.HTTPError, RuntimeError):
        return []
    centres: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()
    for element in elements:
        location = element.get("center") or element
        try:
            lat, lon = float(location["lat"]), float(location["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        rounded = (round(lat * 1000), round(lon * 1000))
        if rounded not in seen:
            seen.add(rounded)
            centres.append((lat, lon))
    # Reduz payload e custo para uma consulta pública compartilhada.
    return centres[:40]


def _niche_terms(niche: str) -> tuple[str, ...]:
    text = _normalize(niche)
    category_map = {
        "restaurante": ("restaurant", "fast food", "cafe", "food", "bar"),
        "restaurant": ("restaurant", "fast food", "cafe", "food", "bar"),
        "salao": ("beauty", "hairdresser", "barber", "beautician", "nail"),
        "cabeleireiro": ("hairdresser", "beauty"), "hair salon": ("hairdresser", "beauty"),
        "barbearia": ("barber", "hairdresser"), "barbeiro": ("barber", "hairdresser"),
        "beauty": ("beauty", "hairdresser", "barber", "beautician", "nail"),
        "spa": ("spa", "beauty", "massage"), "massagem": ("massage", "beauty"),
        "massage": ("massage", "beauty"), "tattoo": ("tattoo",), "tatuagem": ("tattoo",),
        "pizzeria": ("restaurant", "pizza"), "pizza": ("restaurant", "pizza"),
        "bar": ("bar", "pub", "nightclub"), "cafe": ("cafe", "coffee"),
        "cafeteria": ("cafe", "coffee"),
        "dentista": ("dentist",), "dentist": ("dentist",),
        "clinica": ("clinic", "doctors", "dentist", "healthcare"),
        "clinic": ("clinic", "doctors", "dentist", "healthcare"),
        "doctor": ("doctor", "clinic", "doctors"), "medico": ("doctor", "clinic", "doctors"),
        "psychologist": ("psychologist",), "psicologo": ("psychologist",),
        "physiotherapist": ("physiotherapist",), "fisioterapia": ("physiotherapist",),
        "optometrist": ("optometrist",), "oculista": ("optometrist", "ophthalmologist"),
        "hotel": ("hotel", "guest_house", "motel", "hostel"),
        "academia": ("fitness_centre", "gym", "sports_centre"),
        "gym": ("fitness_centre", "gym", "sports_centre"),
        "pet": ("pet", "veterinary", "animal_boarding"),
        "veterinario": ("veterinary",), "veterinary": ("veterinary",),
        "advogado": ("lawyer",), "lawyer": ("lawyer",),
        "accountant": ("accountant",), "contador": ("accountant",),
        "architect": ("architect",), "arquiteto": ("architect",),
        "marketing": ("advertising", "marketing", "company"),
        "photographer": ("photographer",), "fotografo": ("photographer",),
        "floricultura": ("florist",),
        "farmacia": ("chemist", "pharmacy", "drugstore"), "pharmacy": ("chemist", "pharmacy", "drugstore"),
        "imobiliaria": ("estate_agent",), "real estate": ("estate_agent",),
        "mecanico": ("car_repair", "motorcycle_repair", "tyres"),
        "mechanic": ("car_repair", "motorcycle_repair", "tyres"),
        "plumber": ("plumber",), "encanador": ("plumber",),
        "electrician": ("electrician",), "eletricista": ("electrician",),
        "bakery": ("bakery",), "padaria": ("bakery",),
        "escola": ("school", "college", "kindergarten"), "school": ("school", "college", "kindergarten"),
        "florist": ("florist",),
    }
    matched_terms: list[str] = []
    for key, terms in category_map.items():
        if re.search(r"\b" + re.escape(key) + r"\b", text):
            matched_terms.extend(terms)
    if matched_terms:
        return tuple(dict.fromkeys(matched_terms))
    ignored = {"de", "da", "do", "para", "com", "the", "and", "of", "in", "near"}
    tokens = tuple(token for token in text.split() if len(token) > 2 and token not in ignored)
    return tokens


def _matches_niche(tags: dict, name: str, niche: str) -> bool:
    terms = _niche_terms(niche)
    if not terms:
        return True
    category_text = " ".join(str(value) for key, value in tags.items() if key not in {"website", "contact:website", "phone", "contact:phone", "email", "contact:email"})
    haystack = _normalize(f"{name} {category_text}")
    return any(_normalize(term) in haystack for term in terms)


def _address(tags: dict, city: str, country: str) -> str:
    street = " ".join(part for part in (tags.get("addr:street"), tags.get("addr:housenumber")) if part)
    parts = [street, tags.get("addr:suburb") or tags.get("addr:neighbourhood"), tags.get("addr:city") or city,
             tags.get("addr:postcode"), tags.get("addr:country") or country]
    return ", ".join(str(part).strip() for part in parts if part and str(part).strip())


def _element_to_place(element: dict, niche: str, city: str, country: str) -> dict | None:
    tags = element.get("tags") or {}
    name = str(tags.get("name") or "").strip()
    if not name:
        return None
    contact_links = {
        "website": tags.get("contact:website") or tags.get("website") or "",
        "instagram": tags.get("contact:instagram") or tags.get("instagram") or "",
        "facebook": tags.get("contact:facebook") or tags.get("facebook") or "",
        "tiktok": tags.get("contact:tiktok") or tags.get("tiktok") or "",
        "linkedin": tags.get("contact:linkedin") or tags.get("linkedin") or "",
        "youtube": tags.get("contact:youtube") or tags.get("youtube") or "",
        "whatsapp": tags.get("contact:whatsapp") or tags.get("whatsapp") or "",
        "email": tags.get("contact:email") or tags.get("email") or "",
        "phone": tags.get("contact:phone") or tags.get("phone") or "",
    }
    if not _matches_niche(tags, name, niche):
        return None
    osm_type = element.get("type", "node")
    osm_id = element.get("id", "")
    center = element.get("center") or element
    website_raw = str(contact_links["website"] or "").strip()
    website = normalize_url(website_raw) or website_raw
    for social_key in ("instagram", "facebook", "tiktok", "linkedin", "youtube"):
        if not website and contact_links[social_key]:
            raw_social = str(contact_links[social_key]).strip()
            if not re.match(r"(?i)^https?://", raw_social) and (raw_social.startswith("@") or "." not in raw_social):
                platform = {"instagram": "instagram.com", "facebook": "facebook.com", "tiktok": "tiktok.com",
                            "linkedin": "linkedin.com", "youtube": "youtube.com"}[social_key]
                website = f"https://{platform}/{raw_social.lstrip('@ /')}"
            else:
                website = normalize_url(raw_social) or raw_social
            break
    return {
        "place_id": f"osm:{osm_type}:{osm_id}",
        "nome": name,
        "nicho": niche,
        "endereco": _address(tags, city, country),
        "cidade": tags.get("addr:city") or city,
        "pais": tags.get("addr:country") or country,
        "telefone": str(contact_links["phone"] or "").strip(),
        "nota": None,
        "avaliacoes": 0,
        "link_maps": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
        "website_original": website,
        "business_status": "",
        "latitude": center.get("lat"),
        "longitude": center.get("lon"),
        "osm_tags": tags,
        "osm_contacts": contact_links,
    }


async def iter_openstreetmap(
    *,
    niche: str,
    city: str,
    country: str,
    max_results: int,
    min_rating: float = 0,
    min_reviews: int = 0,
    only_open: bool = False,
    radius_km: float = 15,
    deep_scan: bool = False,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict]:
    """Consulta Overpass em lote; no modo profundo cobre as regiões nomeadas no bbox."""
    if min_rating > 0 or min_reviews > 0:
        raise RuntimeError("OpenStreetMap não fornece nota nem contagem de avaliações; remova esses filtros para continuar.")
    if only_open:
        raise RuntimeError("O OpenStreetMap não informa de forma confiável se o estabelecimento está aberto agora; desmarque esse filtro.")
    location = await geocode_location(city, country)
    if not location:
        raise RuntimeError("Não foi possível localizar a cidade no Nominatim. Confira cidade e país e tente novamente.")
    if stop_event and stop_event.is_set():
        return

    if deep_scan and location.get("boundingbox"):
        centres = await _neighbourhood_centres(location["boundingbox"])
        selectors: list[str] = [f"(around:3500,{lat:.6f},{lon:.6f})" for lat, lon in centres]
        if not selectors:
            selectors = [f"({_bbox_selector(location['boundingbox'])})"]
    elif radius_km > 0:
        selectors = [f"(around:{min(radius_km, 100) * 1000:.0f},{location['lat']:.6f},{location['lon']:.6f})"]
    else:
        selectors = [f"({_bbox_selector(location['boundingbox'])})"] if location.get("boundingbox") else [
            f"(around:15000,{location['lat']:.6f},{location['lon']:.6f})"
        ]

    query = _business_query(selectors)
    try:
        elements = await _overpass(query)
    except httpx.TimeoutException as exc:
        raise RuntimeError("A consulta aberta do OpenStreetMap excedeu o tempo limite. Reduza o raio ou tente mais tarde.") from exc
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Overpass respondeu HTTP {exc.response.status_code}. Tente novamente mais tarde.") from exc

    seen: set[str] = set()
    yielded = 0
    for element in elements:
        if stop_event and stop_event.is_set():
            return
        identity = f"{element.get('type')}:{element.get('id')}"
        if identity in seen:
            continue
        seen.add(identity)
        place = _element_to_place(element, niche, city, country)
        if not place:
            continue
        if yielded >= max_results:
            return
        yield place
        yielded += 1


async def enrich_from_web_search(lead: dict, *, stop_event: asyncio.Event | None = None) -> dict:
    """Complementa contatos faltantes com duas consultas públicas moderadas no DuckDuckGo."""
    contacts = {
        "whatsapp": lead.get("whatsapp"), "link_whatsapp_direto": lead.get("link_whatsapp_direto"),
        "instagram": lead.get("instagram"), "facebook": lead.get("facebook"),
        "tiktok": lead.get("tiktok"), "linkedin": lead.get("linkedin"), "youtube": lead.get("youtube"),
        "email": lead.get("email"), "link_agendamento": lead.get("link_agendamento"),
    }
    missing_social = any(not contacts.get(field) for field in ("instagram", "facebook", "tiktok", "linkedin", "youtube"))
    if not missing_social and contacts.get("email") and contacts.get("whatsapp"):
        return contacts
    try:
        from ddgs import DDGS
    except ImportError:
        return contacts

    name = str(lead.get("nome") or "").strip()
    city = str(lead.get("cidade") or "").strip()
    if not name:
        return contacts
    queries = (
        f'"{name}" "{city}" (site:instagram.com OR site:facebook.com OR site:tiktok.com OR site:linkedin.com OR site:youtube.com)',
        f'"{name}" "{city}" ("@gmail.com" OR "@hotmail.com" OR "@yahoo.com" OR "wa.me" OR "whatsapp")',
    )
    company_terms = [token for token in _normalize(name).split() if len(token) > 2]
    for query in queries:
        if stop_event and stop_event.is_set():
            break
        try:
            results = await _ddg_text_search_rate_limited(query)
        except Exception:
            results = []
        for result in results:
            title = str(result.get("title") or "")
            body = str(result.get("body") or result.get("description") or "")
            searchable = _normalize(f"{title} {body}")
            if company_terms and not any(term in searchable for term in company_terms):
                continue
            if city and _normalize(city) not in searchable and city.casefold() not in body.casefold():
                continue
            href = str(result.get("href") or result.get("url") or "")
            extracted = extract_public_contacts(f"{title} {body}", href)
            host = (urlsplit(href).hostname or "").lower()
            if any(host == domain or host.endswith("." + domain) for domain in (
                "yelp.com", "tripadvisor.com", "yellowpages.com", "mapquest.com",
                "foursquare.com", "hotfrog.com", "manta.com", "dnb.com",
            )):
                extracted["link_agendamento"] = None
            contacts = merge_contacts(contacts, extracted)
    return contacts


async def _ddg_text_search_rate_limited(query: str) -> list[dict]:
    global _LAST_DDG_REQUEST
    async with _DDG_LOCK:
        if _LAST_DDG_REQUEST:
            await asyncio.sleep(random.uniform(2.0, 4.0))
        try:
            return await asyncio.to_thread(_ddg_text_search, query)
        finally:
            _LAST_DDG_REQUEST = time.monotonic()


def _ddg_text_search(query: str) -> list[dict]:
    from ddgs import DDGS
    with DDGS() as search:
        return list(search.text(query, max_results=8))

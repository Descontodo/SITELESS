"""Classificação de websites e extração de contatos públicos."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


SOCIAL_AND_BOOKING_DOMAINS = (
    "instagram.com", "facebook.com", "fb.com", "tiktok.com", "wa.me",
    "whatsapp.com", "linkedin.com", "youtube.com", "youtu.be", "booksy.com",
    "fresha.com", "vagaro.com", "yelp.com", "tripadvisor.com", "linktr.ee",
    "bio.site", "beacons.ai", "ifood.com.br", "anota.ai", "doctoralia.com.br",
    "getninjas.com.br", "ubereats.com", "booking.com", "treatwell.com",
    "square.site", "calendly.com", "simplybook.me", "zenoti.com",
    "taplink.cc", "taplink.at", "msha.ke", "solo.to", "campsite.bio",
    "stan.store", "milkshake.app", "linkpop.com",
    "yellowpages.com", "yell.com", "justdial.com", "sulekha.com", "zomato.com",
    "doordash.com", "grubhub.com", "rappi.com", "opentable.com", "resy.com",
    "zocdoc.com", "healthgrades.com", "doctolib.com", "practo.com", "thumbtack.com",
    "angi.com", "trustpilot.com", "houzz.com", "zillow.com", "realtor.com",
)

FREE_SITE_DOMAINS = (
    "negocio.site", "wixsite.com", "wordpress.com", "blogspot.com",
    "sites.google.com", "canva.site",
)

PARKED_DOMAIN_MARKERS = (
    "domain is for sale", "this domain is for sale", "buy this domain",
    "domain for sale", "sedoparking", "parked free", "godaddy", "afternic",
    "parkingcrew", "hugedomains", "em construção", "em construcao",
    "site em construção", "under construction", "coming soon",
)

EMAIL_RE = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_|~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b")
WHATSAPP_LINK_RE = re.compile(
    r"""(?i)(?:(?:https?:)?//)?(?:api\.|www\.)?(?:wa\.me|whatsapp\.com)/[^\s"'<>]+"""
)
PUBLIC_CONTACT_URL_RE = re.compile(
    r"(?i)(?:(?:https?:)?//)?(?:www\.)?(?:instagram\.com|facebook\.com|fb\.com|"
    r"tiktok\.com|linkedin\.com|youtube\.com|youtu\.be|wa\.me|whatsapp\.com|"
    r"linktr\.ee|bio\.site|beacons\.ai|taplink\.(?:cc|at)|calendly\.com|"
    r"booksy\.com|fresha\.com|vagaro\.com|simplybook\.me)/[^\s\"'<>]+"
)
WHATSAPP_HOSTS = ("wa.me", "whatsapp.com")
CONTACT_PAGE_WORDS = (
    "contact", "contato", "contacto", "about", "sobre", "fale", "email",
    "e-mail", "whatsapp", "atendimento", "agende", "agendar", "agenda",
    "booking", "book", "appointment", "schedule", "reserv", "connect",
    "reach-us", "get-in-touch", "impressum",
)
BOOKING_LINK_WORDS = (
    "agend", "booking", "appointment", "schedule", "reserv",
    "calendly", "fresha", "booksy", "simplybook", "vagaro",
)

_COUNTRY_DIAL_CODES: dict[str, str] = {}
_DIAL_CODE_GROUPS = {
    "1": "us|usa|united states|united states of america|estados unidos|canada|canadá|ca|can|antigua and barbuda|ag|bahamas|bs|barbados|bb|bermuda|bm|british virgin islands|vg|cayman islands|ky|dominica|dm|dominican republic|do|grenada|gd|guam|gu|jamaica|jm|montserrat|ms|northern mariana islands|mp|puerto rico|pr|saint kitts and nevis|kn|saint lucia|lc|saint vincent and the grenadines|vc|sint maarten|sx|trinidad and tobago|tt|turks and caicos islands|tc|us virgin islands|vi",
    "7": "russia|russian federation|ru|kazakhstan|kz",
    "20": "egypt|eg", "27": "south africa|za", "30": "greece|gr",
    "31": "netherlands|holland|nl", "32": "belgium|be", "33": "france|fr",
    "34": "spain|españa|es", "36": "hungary|hu", "39": "italy|italia|it|vatican|va",
    "40": "romania|ro", "41": "switzerland|ch", "43": "austria|at",
    "44": "united kingdom|uk|gb|great britain|england|scotland|wales",
    "45": "denmark|dk", "46": "sweden|se", "47": "norway|no",
    "48": "poland|pl", "49": "germany|deutschland|de", "51": "peru|pe",
    "52": "mexico|méxico|mx", "53": "cuba|cu", "54": "argentina|ar",
    "55": "brazil|brasil|br", "56": "chile|cl", "57": "colombia|co",
    "58": "venezuela|ve", "60": "malaysia|my", "61": "australia|au",
    "62": "indonesia|id", "63": "philippines|ph", "64": "new zealand|nz",
    "65": "singapore|sg", "66": "thailand|th", "81": "japan|jp",
    "82": "south korea|korea republic of|kr", "84": "vietnam|viet nam|vn",
    "86": "china|cn", "90": "turkey|türkiye|tr", "91": "india|in",
    "92": "pakistan|pk", "93": "afghanistan|af", "94": "sri lanka|lk",
    "95": "myanmar|burma|mm", "98": "iran|ir", "211": "south sudan|ss",
    "212": "morocco|ma", "213": "algeria|dz", "216": "tunisia|tn",
    "218": "libya|ly", "220": "gambia|gm", "221": "senegal|sn",
    "222": "mauritania|mr", "223": "mali|ml", "224": "guinea|gn",
    "225": "ivory coast|cote d ivoire|ci", "226": "burkina faso|bf",
    "227": "niger|ne", "228": "togo|tg", "229": "benin|bj",
    "230": "mauritius|mu", "231": "liberia|lr", "232": "sierra leone|sl",
    "233": "ghana|gh", "234": "nigeria|ng", "235": "chad|td",
    "236": "central african republic|cf", "237": "cameroon|cm",
    "238": "cape verde|cabo verde|cv", "239": "sao tome and principe|st",
    "240": "equatorial guinea|gq", "241": "gabon|ga",
    "242": "republic of the congo|congo brazzaville|cg",
    "243": "democratic republic of the congo|drc|cd", "244": "angola|ao",
    "245": "guinea bissau|guinea-bissau|guiné bissau|gw",
    "248": "seychelles|sc", "249": "sudan|sd", "250": "rwanda|rw",
    "251": "ethiopia|et", "252": "somalia|so", "253": "djibouti|dj",
    "254": "kenya|ke", "255": "tanzania|tz", "256": "uganda|ug",
    "257": "burundi|bi", "258": "mozambique|moçambique|mz",
    "260": "zambia|zm", "263": "zimbabwe|zw", "264": "namibia|na",
    "265": "malawi|mw", "266": "lesotho|ls", "267": "botswana|bw",
    "268": "eswatini|swaziland|sz", "290": "saint helena|tristan da cunha|sh",
    "291": "eritrea|er", "297": "aruba|aw", "298": "faroe islands|fo",
    "299": "greenland|gl", "350": "gibraltar|gi", "351": "portugal|pt",
    "352": "luxembourg|lu", "353": "ireland|ie", "354": "iceland|is",
    "357": "cyprus|cy", "358": "finland|fi|aland islands|ax", "370": "lithuania|lt", "371": "latvia|lv",
    "372": "estonia|ee", "380": "ukraine|ua", "381": "serbia|rs",
    "385": "croatia|hr", "386": "slovenia|si", "387": "bosnia and herzegovina|ba",
    "389": "north macedonia|mk", "420": "czech republic|czechia|cz",
    "421": "slovakia|sk", "423": "liechtenstein|li", "501": "belize|bz",
    "502": "guatemala|gt", "503": "el salvador|sv", "504": "honduras|hn",
    "505": "nicaragua|ni", "506": "costa rica|cr", "507": "panama|pa",
    "500": "falkland islands|malvinas|fk", "508": "saint pierre and miquelon|pm",
    "509": "haiti|ht", "590": "guadeloupe|saint barthelemy|saint martin|gp|bl|mf",
    "596": "martinique|mq",
    "591": "bolivia|bo", "592": "guyana|gy", "594": "french guiana|gf",
    "593": "ecuador|ec", "595": "paraguay|py", "597": "suriname|sr",
    "598": "uruguay|uy", "599": "curacao|curaçao|bonaire|sint eustatius|saba|cw|bq",
    "670": "timor leste|east timor|tl", "672": "norfolk island|nf",
    "673": "brunei|bn", "674": "nauru|nr", "675": "papua new guinea|pg",
    "676": "tonga|to", "677": "solomon islands|sb", "678": "vanuatu|vu",
    "679": "fiji|fj", "680": "palau|pw", "681": "wallis and futuna|wf",
    "682": "cook islands|ck", "683": "niue|nu", "685": "samoa|ws",
    "686": "kiribati|ki", "687": "new caledonia|nc", "688": "tuvalu|tv",
    "689": "french polynesia|pf", "690": "tokelau|tk", "691": "micronesia|fm",
    "692": "marshall islands|mh",
    "852": "hong kong|hk", "853": "macau|macao|mo", "855": "cambodia|kh",
    "850": "north korea|korea democratic people republic of|kp",
    "856": "laos|la", "880": "bangladesh|bd", "886": "taiwan|tw",
    "960": "maldives|mv", "961": "lebanon|lb", "962": "jordan|jo",
    "963": "syria|sy", "964": "iraq|iq", "965": "kuwait|kw",
    "966": "saudi arabia|sa", "967": "yemen|ye", "968": "oman|om",
    "970": "palestine|ps", "971": "united arab emirates|uae|ae",
    "972": "israel|il", "973": "bahrain|bh", "974": "qatar|qa",
    "975": "bhutan|bt", "976": "mongolia|mn", "977": "nepal|np",
    "992": "tajikistan|tj", "993": "turkmenistan|tm",
    "994": "azerbaijan|az", "995": "georgia|ge", "996": "kyrgyzstan|kg",
    "998": "uzbekistan|uz",
}
def _normalized_country(country: str | None) -> str:
    text = unicodedata.normalize("NFKD", (country or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


for _calling_code, _aliases in _DIAL_CODE_GROUPS.items():
    for _alias in _aliases.split("|"):
        _COUNTRY_DIAL_CODES[_normalized_country(_alias)] = _calling_code


def country_calling_code(country: str | None) -> str | None:
    """Retorna o DDI para nomes e códigos ISO reconhecidos."""
    return _COUNTRY_DIAL_CODES.get(_normalized_country(country))


def phone_to_whatsapp_digits(phone: str | None, country: str | None) -> str | None:
    """Normaliza telefone para os dígitos internacionais exigidos por wa.me."""
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    first_plus = raw.find("+")
    first_digit = re.search(r"\d", raw)
    has_international_plus = first_plus >= 0 and (
        first_digit is None or first_plus < first_digit.start()
    )
    if has_international_plus:
        return digits if 10 <= len(digits) <= 15 else None
    if digits.startswith("00"):
        digits = digits[2:]
        return digits if 10 <= len(digits) <= 15 else None
    calling_code = country_calling_code(country)
    if not calling_code:
        return None
    if digits.startswith(calling_code) and len(digits) >= len(calling_code) + 8:
        return digits if len(digits) <= 15 else None
    local_number = digits[1:] if digits.startswith("0") else digits
    international = calling_code + local_number
    return international if 10 <= len(international) <= 15 else None


def make_whatsapp_link(phone: str | None, country: str | None) -> str | None:
    """Gera um link direto wa.me quando o telefone está completo."""
    digits = phone_to_whatsapp_digits(phone, country)
    return f"https://wa.me/{digits}" if digits else None


def whatsapp_from_url(url: str | None) -> tuple[str | None, str | None]:
    """Extrai número internacional e link direto de uma URL WhatsApp."""
    normalized = normalize_url(url)
    if not normalized:
        return None, None
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").lower()
    if not _matches_domain(host, WHATSAPP_HOSTS):
        return None, None
    candidates: list[str] = []
    query = parse_qs(parsed.query)
    for key in ("phone", "phone_number", "number"):
        candidates.extend(query.get(key, []))
    if _matches_domain(host, ("wa.me",)):
        path_number = parsed.path.strip("/").split("/", 1)[0]
        if path_number:
            candidates.insert(0, path_number)
    else:
        path_match = re.search(r"/send/([^/]+)", parsed.path, flags=re.I)
        if path_match:
            candidates.insert(0, path_match.group(1))
    for candidate in candidates:
        digits = re.sub(r"\D", "", unquote(candidate))
        if digits.startswith("00"):
            digits = digits[2:]
        if 8 <= len(digits) <= 15:
            return digits, f"https://wa.me/{digits}"
    if parsed.path.strip("/") or parsed.query:
        return None, normalized
    return None, None


def normalize_url(value: str | None) -> str | None:
    """Retorna uma URL HTTP(S) normalizada ou None quando o valor é vazio/inválido."""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not re.match(r"^https?://", raw, flags=re.I):
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        _ = parsed.port
        return raw
    except ValueError:
        return None


def _host(url: str | None) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    try:
        return (urlsplit(normalized).hostname or "").lower().strip(".")
    except ValueError:
        return ""


def _is_private_target(url: str | None) -> bool:
    normalized = normalize_url(url)
    if not normalized:
        return True
    host = _host(normalized)
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
        return not address.is_global
    except ValueError:
        return False


def _matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def classify_website(
    website: str | None,
    *,
    test_active: bool = False,
    timeout: float = 3.5,
) -> tuple[bool, str | None]:
    """Retorna (é_lead, motivo_flag) segundo as regras de domínio solicitadas."""
    normalized = normalize_url(website)
    if not (website or "").strip():
        return True, "sem website"
    if not normalized:
        return False, None

    host = _host(normalized)
    if _matches_domain(host, SOCIAL_AND_BOOKING_DOMAINS):
        return True, "só rede social/agendamento"
    if _matches_domain(host, FREE_SITE_DOMAINS):
        return True, "página gratuita/amadora"
    if not test_active:
        return False, None
    if _is_private_target(normalized):
        return False, None

    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)),
            follow_redirects=False,
            headers={"User-Agent": "ProspectorLocal/1.0 (verificação leve de URL)"},
        ) as client:
            check_url = normalized
            response = None
            for _ in range(6):
                if _is_private_target(check_url):
                    return False, None
                response = client.get(check_url)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    return False, None
                check_url = urljoin(str(response.url), location)
            if response is None or response.status_code in {301, 302, 303, 307, 308}:
                return False, None
        final_url = str(response.url)
        final_host = _host(final_url)
        if _matches_domain(final_host, SOCIAL_AND_BOOKING_DOMAINS):
            return True, "só rede social/agendamento"
        if _matches_domain(final_host, FREE_SITE_DOMAINS):
            return True, "página gratuita/amadora"
        if response.status_code >= 400:
            return True, "sem website (domínio fora do ar/estacionado)"
        page_text = response.text[:500_000].lower()
        if any(marker in page_text for marker in PARKED_DOMAIN_MARKERS):
            return True, "sem website (domínio fora do ar/estacionado)"
    except (httpx.TimeoutException, httpx.NetworkError, httpx.InvalidURL, ValueError):
        return True, "sem website (domínio fora do ar/estacionado)"
    return False, None


def contacts_from_url(url: str | None) -> dict[str, str | None]:
    """Extrai os contatos que podem ser identificados somente pela URL."""
    host = _host(url)
    result: dict[str, str | None] = {
        "telefone": None,
        "whatsapp": None,
        "link_whatsapp_direto": None,
        "instagram": None,
        "facebook": None,
        "tiktok": None,
        "linkedin": None,
        "youtube": None,
        "email": None,
        "link_agendamento": None,
    }
    normalized = normalize_url(url)
    if not normalized:
        return result
    whatsapp_number, whatsapp_link = whatsapp_from_url(normalized)
    result["whatsapp"] = whatsapp_number
    result["link_whatsapp_direto"] = whatsapp_link
    if _matches_domain(host, ("instagram.com",)):
        result["instagram"] = normalized
    elif _matches_domain(host, ("facebook.com", "fb.com")):
        result["facebook"] = normalized
    elif _matches_domain(host, ("tiktok.com",)):
        result["tiktok"] = normalized
    elif _matches_domain(host, ("linkedin.com",)):
        result["linkedin"] = normalized
    elif _matches_domain(host, ("youtube.com", "youtu.be")):
        result["youtube"] = normalized
    non_social = (
        "instagram.com", "facebook.com", "fb.com", "tiktok.com", "wa.me",
        "whatsapp.com", "linkedin.com", "youtube.com", "youtu.be",
    )
    if _matches_domain(host, SOCIAL_AND_BOOKING_DOMAINS) and not _matches_domain(host, non_social):
        result["link_agendamento"] = normalized
    return result


def extract_public_contacts(html: str, base_url: str) -> dict[str, str | None]:
    """Extrai todos os e-mails visíveis e links públicos reconhecidos no HTML."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: dict[str, list[str]] = {
        "telefone": [],
        "whatsapp": [],
        "link_whatsapp_direto": [],
        "instagram": [],
        "facebook": [],
        "tiktok": [],
        "linkedin": [],
        "youtube": [],
        "email": [],
        "link_agendamento": [],
    }

    def add(field: str, value: str | None) -> None:
        if not value:
            return
        for item in value.split(";"):
            item = item.strip()
            if item and all(item.casefold() != current.casefold() for current in found[field]):
                found[field].append(item)

    for field, value in contacts_from_url(base_url).items():
        add(field, value)

    def add_email(value: str) -> None:
        cleaned = value.strip().strip(".,;:")
        if EMAIL_RE.fullmatch(cleaned):
            add("email", cleaned)

    for anchor in soup.find_all("a", href=True):
        href = unquote(str(anchor.get("href", "")).strip())
        if re.match(r"(?i)^(?:api\.|www\.)?(?:wa\.me|whatsapp\.com)/", href):
            href = "https://" + href
        absolute = urljoin(base_url, href)
        if href.lower().startswith("mailto:"):
            mailto_values = href[7:].split("?", 1)[0].replace(";", ",").split(",")
            for email in mailto_values:
                add_email(email)
        elif href.lower().startswith("tel:"):
            phone = unquote(href[4:].split("?", 1)[0]).strip()
            digits = re.sub(r"\D", "", phone)
            if 7 <= len(digits) <= 15:
                add("telefone", phone)
        contact = contacts_from_url(absolute)
        for field, value in contact.items():
            add(field, value)
        label = str(anchor.get_text(" ", strip=True)).lower()
        searchable_link = (href + " " + label).lower()
        if any(word in searchable_link for word in BOOKING_LINK_WORDS):
            add("link_agendamento", absolute)

    visible_text = soup.get_text(" ", strip=True)
    for email in EMAIL_RE.findall(visible_text):
        add_email(email)
    for email in EMAIL_RE.findall(html or ""):
        add_email(email)
    # Alguns construtores deixam os links públicos em atributos/data JSON no HTML.
    for match in WHATSAPP_LINK_RE.findall(html or ""):
        candidate = match.rstrip(").,;]")
        if not re.match(r"(?i)^https?://", candidate):
            candidate = "https://" + candidate.lstrip("/")
        number, direct_link = whatsapp_from_url(candidate)
        add("whatsapp", number)
        add("link_whatsapp_direto", direct_link)
    for match in PUBLIC_CONTACT_URL_RE.findall(visible_text):
        candidate = match.rstrip(").,;]}")
        if not re.match(r"(?i)^https?://", candidate):
            candidate = "https://" + candidate.lstrip("/")
        for field, value in contacts_from_url(candidate).items():
            add(field, value)
        candidate_host = _host(candidate)
        if _matches_domain(candidate_host, SOCIAL_AND_BOOKING_DOMAINS):
            if _matches_domain(candidate_host, ("calendly.com", "booksy.com", "fresha.com", "vagaro.com", "simplybook.me", "taplink.cc", "taplink.at")):
                add("link_agendamento", candidate)

    return {field: "; ".join(values) if values else None for field, values in found.items()}


def _same_site(host: str, root_host: str) -> bool:
    return bool(host and root_host) and (host == root_host or host.endswith("." + root_host))


def _contact_page_candidates(html: str, page_url: str, root_host: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    scored: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = unquote(str(anchor.get("href", "")).strip())
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target = urljoin(page_url, href)
        parsed = urlsplit(target)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not _same_site(host, root_host):
            continue
        searchable = unquote((parsed.path + " " + str(anchor.get_text(" ", strip=True))).lower())
        matches = [word for word in CONTACT_PAGE_WORDS if word in searchable]
        if matches:
            scored.append((len(matches), target.split("#", 1)[0]))
    scored.sort(key=lambda item: item[0], reverse=True)
    return list(dict.fromkeys(url for _, url in scored))


async def _read_html_page(client: httpx.AsyncClient, url: str, max_bytes: int) -> tuple[str | None, str | None]:
    current_url = url
    for _ in range(6):
        if _is_private_target(current_url):
            return None, None
        async with client.stream("GET", current_url, follow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    return None, None
                current_url = urljoin(str(response.url), location)
                continue
            if response.status_code >= 400:
                return None, None
            content_type = response.headers.get("content-type", "").lower()
            if content_type and "html" not in content_type and "text/plain" not in content_type:
                return None, None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                available = max_bytes - len(body)
                if available <= 0:
                    break
                body.extend(chunk[:available])
                if len(body) >= max_bytes:
                    break
            encoding = response.encoding or "utf-8"
            return body.decode(encoding, errors="replace"), str(response.url)
    return None, None


async def fetch_public_contacts(
    url: str | None,
    *,
    timeout: float = 3.5,
    max_bytes: int = 500_000,
    max_pages: int = 5,
    max_seconds: float = 10.0,
) -> dict[str, str | None]:
    """Lê a página pública e páginas internas de contato/sobre para ampliar os achados."""
    result = contacts_from_url(url)
    normalized = normalize_url(url)
    if not normalized or _is_private_target(normalized):
        return result
    root_host = _host(normalized)
    queue = [normalized]
    visited: set[str] = set()
    deadline = asyncio.get_running_loop().time() + max_seconds
    try:
        timeout_config = httpx.Timeout(timeout, connect=min(timeout, 2.0))
        async with httpx.AsyncClient(
            timeout=timeout_config,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": "ProspectorLocal/1.0 (public contact extraction)"},
        ) as client:
            while queue and len(visited) < max(1, max_pages):
                current_url = queue.pop(0).split("#", 1)[0]
                if current_url in visited:
                    continue
                visited.add(current_url)
                remaining_time = deadline - asyncio.get_running_loop().time()
                if remaining_time <= 0:
                    break
                try:
                    html, page_url = await asyncio.wait_for(
                        _read_html_page(client, current_url, max_bytes),
                        timeout=remaining_time,
                    )
                except (asyncio.TimeoutError, httpx.HTTPError, UnicodeError):
                    continue
                if html is None or page_url is None:
                    continue

                extracted = extract_public_contacts(html, page_url)
                result = merge_contacts(result, extracted)
                page_host = _host(page_url)
                is_aggregator = _matches_domain(page_host, SOCIAL_AND_BOOKING_DOMAINS)
                if not is_aggregator and len(visited) < max_pages:
                    for candidate in _contact_page_candidates(html, page_url, root_host):
                        if candidate not in visited and candidate not in queue:
                            queue.append(candidate)
    except (httpx.HTTPError, UnicodeError, ValueError):
        pass
    return result


async def fetch_public_contacts_browser(
    url: str | None,
    *,
    max_pages: int = 5,
    timeout_ms: int = 12_000,
) -> dict[str, str | None]:
    """Fallback para páginas públicas com conteúdo carregado por JavaScript."""
    result = contacts_from_url(url)
    normalized = normalize_url(url)
    if not normalized or _is_private_target(normalized):
        return result
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return result
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(locale="pt-BR")
                async def restrict_private_requests(route):
                    if _is_private_target(route.request.url):
                        await route.abort()
                    else:
                        await route.continue_()
                await context.route("**/*", restrict_private_requests)
                page = await context.new_page()
                root_host = _host(normalized)
                pending = [normalized]
                visited: set[str] = set()
                while pending and len(visited) < max(1, max_pages):
                    current = pending.pop(0).split("#", 1)[0]
                    if current in visited:
                        continue
                    visited.add(current)
                    try:
                        await page.goto(current, wait_until="domcontentloaded", timeout=timeout_ms)
                        await page.wait_for_timeout(500)
                        final_url = page.url
                        html = await page.content()
                    except Exception:
                        continue
                    result = merge_contacts(result, extract_public_contacts(html, final_url))
                    if not _matches_domain(_host(final_url), SOCIAL_AND_BOOKING_DOMAINS):
                        pending.extend(
                            target for target in _contact_page_candidates(html, final_url, root_host)
                            if target not in visited and target not in pending
                        )
                await context.close()
            finally:
                await browser.close()
    except Exception:
        # A falha do Chromium nunca descarta os contatos já coletados por HTTP.
        pass
    return result


def merge_contacts(*contact_sets: dict[str, Any]) -> dict[str, str | None]:
    """Une listas separadas por ponto e vírgula, removendo duplicatas."""
    fields = (
        "telefone",
        "whatsapp",
        "link_whatsapp_direto",
        "instagram",
        "facebook",
        "tiktok",
        "linkedin",
        "youtube",
        "email",
        "link_agendamento",
    )
    merged: dict[str, str | None] = {field: None for field in fields}
    for contacts in contact_sets:
        for field in fields:
            value = contacts.get(field)
            if value:
                values = [part.strip() for part in str(value).split(";") if part.strip()]
                current = [part.strip() for part in (merged[field] or "").split(";") if part.strip()]
                for item in values:
                    if all(item.casefold() != existing.casefold() for existing in current):
                        current.append(item)
                merged[field] = "; ".join(current) if current else None
    return merged

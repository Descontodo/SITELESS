import unittest
from unittest.mock import patch

import httpx
import classifier
from classifier import (
    classify_website,
    contacts_from_url,
    extract_public_contacts,
    make_whatsapp_link,
    normalize_url,
    phone_to_whatsapp_digits,
)


class ClassifierTests(unittest.TestCase):
    def test_empty_url_is_a_lead(self):
        self.assertEqual(classify_website(None), (True, "sem website"))
        self.assertEqual(classify_website("  "), (True, "sem website"))

    def test_own_domain_is_not_a_lead_without_active_check(self):
        self.assertEqual(classify_website("https://exemplo.com.br"), (False, None))
        self.assertEqual(classify_website("loja.example"), (False, None))

    def test_social_domains_are_leads(self):
        self.assertEqual(
            classify_website("https://www.instagram.com/loja"),
            (True, "só rede social/agendamento"),
        )
        self.assertEqual(
            classify_website("https://fb.com/minhaempresa"),
            (True, "só rede social/agendamento"),
        )

    def test_directory_and_booking_domains_are_leads(self):
        self.assertEqual(
            classify_website("https://www.doctoralia.com.br/clinica"),
            (True, "só rede social/agendamento"),
        )
        self.assertEqual(
            classify_website("https://booksy.com/pt-br/123"),
            (True, "só rede social/agendamento"),
        )

    def test_free_site_domain_has_specific_reason(self):
        self.assertEqual(
            classify_website("https://minhaempresa.wixsite.com/site"),
            (True, "página gratuita/amadora"),
        )
        self.assertEqual(
            classify_website("https://sites.google.com/view/empresa"),
            (True, "página gratuita/amadora"),
        )

    def test_social_and_aggregator_urls_are_extracted(self):
        html = """
        <a href="https://www.tiktok.com/@empresa">TikTok</a>
        <a href="https://www.linkedin.com/company/empresa">LinkedIn</a>
        <a href="https://youtube.com/@empresa">YouTube</a>
        <a href="https://taplink.cc/empresa">Agendar</a>
        """
        contacts = extract_public_contacts(html, "https://empresa.example")
        self.assertEqual(contacts["tiktok"], "https://www.tiktok.com/@empresa")
        self.assertEqual(contacts["linkedin"], "https://www.linkedin.com/company/empresa")
        self.assertEqual(contacts["youtube"], "https://youtube.com/@empresa")
        self.assertEqual(contacts["link_agendamento"], "https://taplink.cc/empresa")

    def test_active_check_marks_http_error_as_down(self):
        class FakeResponse:
            status_code = 404
            url = "https://exemplo.com.br"
            text = ""

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _url):
                return FakeResponse()

        with patch("classifier.httpx.Client", FakeClient):
            self.assertEqual(
                classify_website("https://exemplo.com.br", test_active=True),
                (True, "sem website (domínio fora do ar/estacionado)"),
            )

    def test_normalize_url_adds_https_and_rejects_other_schemes(self):
        self.assertEqual(normalize_url("example.com"), "https://example.com")
        self.assertIsNone(normalize_url("javascript:alert(1)"))

    def test_extracts_public_contacts(self):
        html = """
        <p>Contato: equipe@empresa.example e vendas@empresa.example</p>
        <a href="https://instagram.com/empresa">Instagram</a>
        <a href="https://www.instagram.com/empresa2">Outro Instagram</a>
        <a href="https://www.facebook.com/empresa">Facebook</a>
        <a href="https://facebook.com/empresa2">Outro Facebook</a>
        <a href="https://calendly.com/empresa/consulta">Agende</a>
        <a href="https://booksy.com/empresa">Agende também</a>
        <a href="https://wa.me/5511999990000">WhatsApp</a>
        <a href="https://api.whatsapp.com/send?phone=%2B1%20407-555-0199">WhatsApp internacional</a>
        <a href="tel:+55-71-99999-0000">Telefone</a>
        """
        contacts = extract_public_contacts(html, "https://empresa.example")
        self.assertEqual(contacts["email"], "equipe@empresa.example; vendas@empresa.example")
        self.assertEqual(
            contacts["instagram"],
            "https://instagram.com/empresa; https://www.instagram.com/empresa2",
        )
        self.assertEqual(
            contacts["facebook"],
            "https://www.facebook.com/empresa; https://facebook.com/empresa2",
        )
        self.assertEqual(
            contacts["link_agendamento"],
            "https://calendly.com/empresa/consulta; https://booksy.com/empresa",
        )
        self.assertEqual(contacts["whatsapp"], "5511999990000; 14075550199")
        self.assertEqual(contacts["telefone"], "+55-71-99999-0000")
        self.assertEqual(
            contacts["link_whatsapp_direto"],
            "https://wa.me/5511999990000; https://wa.me/14075550199",
        )

    def test_extracts_whatsapp_from_website_url(self):
        contacts = contacts_from_url(
            "https://api.whatsapp.com/send?phone=%2B55%2071%2099999-0000"
        )
        self.assertEqual(contacts["whatsapp"], "5571999990000")
        self.assertEqual(contacts["link_whatsapp_direto"], "https://wa.me/5571999990000")

    def test_generates_international_whatsapp_links_from_country(self):
        cases = (
            ("+1 407-555-0199", "USA", "14075550199"),
            ("71 99999-0000", "Brasil", "5571999990000"),
            ("912 345 678", "Portugal", "351912345678"),
            ("020 7946 0018", "United Kingdom", "442079460018"),
            ("787 555 0123", "Puerto Rico", "17875550123"),
        )
        for phone, country, expected in cases:
            with self.subTest(country=country):
                self.assertEqual(phone_to_whatsapp_digits(phone, country), expected)
                self.assertEqual(make_whatsapp_link(phone, country), f"https://wa.me/{expected}")

    def test_does_not_guess_unknown_country_calling_code(self):
        self.assertIsNone(phone_to_whatsapp_digits("1234567890", "Unknown country"))


class ContactCrawlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_public_contact_page_and_merges_contacts(self):
        pages = {
            "/": """
                <a href="/contato">Fale conosco</a>
                <a href="https://instagram.com/empresa">Instagram</a>
            """,
            "/contato": """
                <p>contato@empresa.example</p>
                <a href="https://wa.me/5511999990000">WhatsApp</a>
                <a href="https://facebook.com/empresa">Facebook</a>
            """,
        }

        async def handler(request):
            html = pages.get(request.url.path or "/", "")
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=html,
                request=request,
            )

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def mock_async_client(**kwargs):
            return real_async_client(transport=transport, **kwargs)

        with patch("classifier.httpx.AsyncClient", mock_async_client):
            contacts = await classifier.fetch_public_contacts("https://empresa.example")

        self.assertEqual(contacts["email"], "contato@empresa.example")
        self.assertEqual(contacts["whatsapp"], "5511999990000")
        self.assertEqual(contacts["instagram"], "https://instagram.com/empresa")
        self.assertEqual(contacts["facebook"], "https://facebook.com/empresa")


if __name__ == "__main__":
    unittest.main()

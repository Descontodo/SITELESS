import unittest

from scraper import _business_query, _element_to_place, _niche_terms


class OpenStreetMapScraperTests(unittest.TestCase):
    def test_niche_aliases_cover_common_business_categories(self):
        self.assertIn("hairdresser", _niche_terms("salão de beleza"))
        self.assertIn("car_repair", _niche_terms("mecânico"))

    def test_maps_osm_tags_to_lead_fields_and_contacts(self):
        place = _element_to_place({
            "type": "node", "id": 42, "lat": -23.5, "lon": -46.6,
            "tags": {
                "name": "Studio Exemplo", "shop": "beauty", "addr:street": "Rua A",
                "addr:housenumber": "10", "addr:city": "São Paulo", "phone": "+55 11 3333-4444",
                "contact:instagram": "@studioexemplo", "contact:email": "oi@example.test",
            },
        }, "salão de beleza", "São Paulo", "Brasil")
        self.assertEqual(place["place_id"], "osm:node:42")
        self.assertEqual(place["telefone"], "+55 11 3333-4444")
        self.assertEqual(place["website_original"], "https://instagram.com/studioexemplo")
        self.assertEqual(place["osm_contacts"]["instagram"], "@studioexemplo")
        self.assertEqual(place["osm_contacts"]["email"], "oi@example.test")
        self.assertEqual(place["link_maps"], "https://www.openstreetmap.org/node/42")

    def test_overpass_query_covers_business_tags_for_each_region(self):
        query = _business_query(["(around:3500,-23.5,-46.6)"])
        self.assertIn('nwr(around:3500,-23.5,-46.6)["name"]["shop"]', query)
        self.assertIn('["healthcare"]', query)
        self.assertIn("out center tags", query)


if __name__ == "__main__":
    unittest.main()

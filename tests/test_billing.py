from __future__ import annotations

import unittest
from datetime import datetime, timezone

from billing import (
    invoice_subscription_id,
    plan_for_price,
    plan_for_subscription,
    price_matches_expectation,
    subscription_period_end,
    subscription_price_id,
)


class BillingHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.price_ids = {"starter": "price_starter", "agency": "price_agency"}

    def test_price_maps_only_configured_paid_plans(self) -> None:
        self.assertEqual(plan_for_price("price_starter", self.price_ids), "starter")
        self.assertIsNone(plan_for_price("price_unrecognized", self.price_ids))

    def test_plan_is_derived_from_subscription_price(self) -> None:
        subscription = {"items": {"data": [{"price": {"id": "price_agency"}}]}}
        self.assertEqual(subscription_price_id(subscription), "price_agency")
        self.assertEqual(plan_for_subscription(subscription, self.price_ids), "agency")

    def test_subscription_metadata_is_a_fallback_for_mapped_price(self) -> None:
        subscription = {"items": {"data": []}, "metadata": {"plan": "starter"}}
        self.assertEqual(plan_for_subscription(subscription, self.price_ids), "starter")

    def test_price_must_match_usd_amount_and_monthly_interval(self) -> None:
        valid_price = {
            "active": True, "currency": "usd", "unit_amount": 1900,
            "recurring": {"interval": "month"},
        }
        self.assertTrue(price_matches_expectation(valid_price, currency="usd", unit_amount=1900))
        self.assertFalse(price_matches_expectation(valid_price, currency="brl", unit_amount=4700))
        self.assertFalse(price_matches_expectation({**valid_price, "unit_amount": 4900}, currency="usd", unit_amount=1900))
        self.assertFalse(price_matches_expectation({**valid_price, "active": False}, currency="usd", unit_amount=1900))

    def test_period_end_is_saved_as_utc_iso_timestamp(self) -> None:
        timestamp = 1_800_000_000
        expected = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        self.assertEqual(subscription_period_end({"current_period_end": timestamp}), expected)

    def test_invoice_subscription_id_supports_current_nested_shape(self) -> None:
        invoice = {
            "parent": {
                "subscription_details": {"subscription": {"id": "sub_nested"}},
            },
        }
        self.assertEqual(invoice_subscription_id(invoice), "sub_nested")
        self.assertEqual(invoice_subscription_id({"subscription": "sub_direct"}), "sub_direct")


if __name__ == "__main__":
    unittest.main()

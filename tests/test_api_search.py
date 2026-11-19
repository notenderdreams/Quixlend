"""core.api.search builds the Algolia filter string; checked without the network."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
from core import api


def filters(**kwargs):
    with mock.patch.object(api.http, "post", return_value={}) as post:
        api.search(**kwargs)
    return post.call_args.args[1].get("filters")


class SearchFilters(unittest.TestCase):
    def test_no_filter_by_default(self):
        self.assertIsNone(filters())

    def test_type_and_one_category(self):
        self.assertEqual(
            filters(asset_type="surface", category="brick"), 'type:"surface" AND category:"brick"'
        )

    def test_several_categories_match_any(self):
        self.assertEqual(
            filters(asset_type="atlas", category=("decals", "decal")),
            'type:"atlas" AND (category:"decals" OR category:"decal")',
        )

    def test_a_single_category_in_a_list_needs_no_parentheses(self):
        self.assertEqual(filters(category=["decals"]), 'category:"decals"')

    def test_quotes_cannot_break_out_of_a_category(self):
        self.assertEqual(filters(category='a"b'), 'category:"ab"')


if __name__ == "__main__":
    unittest.main()

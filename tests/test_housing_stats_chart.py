import unittest

from user_jobs import housing_stats_chart


class HousingStatsChartTests(unittest.TestCase):
    def test_render_dashboard_returns_nonempty_png_bytes(self):
        rows = [(2.0, 55.0, 800.0), (3.0, 90.0, 1400.0), (None, None, None)]
        axis_labels = {"area": "Площа, м²", "price": "Ціна, €", "rooms": "Кімнати"}

        buf = housing_stats_chart.render_dashboard(rows, "Знайдено оголошень за тиждень: 3", axis_labels)

        data = buf.read()
        self.assertGreater(len(data), 100)
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')

    def test_bucket_counts_places_values_in_the_right_bin(self):
        counts = housing_stats_chart._bucket_counts([10, 35, 999], (0, 30, 40, float('inf')))
        self.assertEqual(counts, [1, 1, 1])

    def test_bucket_counts_skips_none_values(self):
        counts = housing_stats_chart._bucket_counts([None, 5], (0, 10, float('inf')))
        self.assertEqual(counts, [1, 0])


if __name__ == "__main__":
    unittest.main()

"""热门文章接口与安全链接渲染的回归检查（无需网络）。"""
import unittest
from unittest.mock import Mock, patch

from market_intraday_sentiment import fetch_ths_hot_articles, fetch_ths_hot_rankings
from market_sentiment_dashboard import hot_article_list, hot_stock_panel


class HotArticlesTests(unittest.TestCase):
    def fetch(self, rows, status="0"):
        response = Mock()
        response.json.return_value = {
            "status_code": status,
            "answer": {"components": [{"data": {"datas": rows}}]},
        }
        with patch("market_intraday_sentiment._session") as session:
            session.return_value.get.return_value = response
            return fetch_ths_hot_articles()

    def test_source_descending_order_and_optional_stock_fields(self):
        result = self.fetch([
            {"rank": 2, "title": "第二篇", "score": 0, "contentUrl": []},
            {"rank": 1, "title": "<b>第一篇</b>&amp;后续", "score": 38000,
             "contentUrl": ["https://news.10jqka.com.cn/example/"],
             "stockInfo": [{"stockName": "港股", "stockCode": "HK1810"},
                           {"stockName": "A股", "stockZdf": "-1.94"}]},
        ])
        self.assertEqual([r["rank"] for r in result], [1, 2])
        self.assertEqual(result[0]["title"], "第一篇&后续")
        self.assertEqual(result[0]["stocks"][1]["change_pct"], -1.94)
        self.assertIsNone(result[0]["stocks"][0]["change_pct"])
        self.assertEqual(result[1]["heat"], 0)
        self.assertEqual(result[1]["url"], "")

    def test_unavailable_or_changed_response_is_reported(self):
        for rows, status in [([], "0"), ([{"rank": 1}], "0"), ([], "1")]:
            with self.subTest(rows=rows, status=status), self.assertRaises(RuntimeError):
                self.fetch(rows, status)

    def test_article_error_does_not_discard_other_rankings(self):
        with patch("market_intraday_sentiment._ths_hot_get", return_value={}), \
             patch("market_intraday_sentiment._ths_index_hot_get", return_value=[]), \
             patch("market_intraday_sentiment.fetch_ths_hot_etfs", return_value=([{"rank": 1}], [])), \
             patch("market_intraday_sentiment.fetch_ths_hot_articles", side_effect=RuntimeError):
            result = fetch_ths_hot_rankings()
        self.assertEqual(result["etfs"], [{"rank": 1}])
        self.assertEqual(result["articles"], [])
        self.assertIn("热门文章：RuntimeError", result["errors"])

    def test_safe_new_tab_link_and_escaped_content(self):
        row = {"rank": 1, "title": '<script>alert("x")</script>',
               "url": "https://news.10jqka.com.cn/a?x=1&y=2", "heat": 38000,
               "stocks": [{"name": "<股票>", "change_pct": 10.16}]}
        rendered = hot_article_list([row])
        self.assertIn('target="_blank" rel="noopener noreferrer"', rendered)
        self.assertIn("3.8万热度", rendered)
        self.assertIn("+10.16%", rendered)
        self.assertIn("&lt;股票&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        row["url"] = "javascript:alert(1)"
        self.assertNotIn("href=", hot_article_list([row]))
        self.assertIn("原文链接暂不可用", hot_article_list([row]))

    def test_empty_articles_keep_existing_tabs(self):
        rendered = hot_stock_panel([])
        for key in ("ranking", "watchlist", "articles"):
            self.assertIn(f'data-hot-stock-tab="{key}"', rendered)
        self.assertIn("本次刷新未取得热门文章", rendered)


if __name__ == "__main__":
    unittest.main()

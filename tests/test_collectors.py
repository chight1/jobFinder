"""Parser tests against small hand-made responses shaped like each API's real ones."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
from httpx import URL

from jobhunt.collectors import BoardGone, Context, Summary, ashby, greenhouse, hn, json_apis, lever, pages, rss, workday
from jobhunt.collectors.runner import detail_loader
from jobhunt.discover import board_from_url
from jobhunt.fetch import Page


class FakeFetcher:
    """Answers by URL (with query) from a dict; records every request."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def _answer(self, url: str) -> Page:
        self.requests.append(url)
        body = self.responses.get(url)
        if body is None:
            return Page(url, 404, "")
        return Page(url, 200, body if isinstance(body, str) else json.dumps(body))

    def get(self, source_id, url, *, kind="html", params=None, headers=None):
        return self._answer(str(URL(url, params=params)) if params else url)

    def post_json(self, source_id, url, body, *, kind="api"):
        return self._answer(f"{url}#offset={body['offset']}")


def ctx_for(fetcher: FakeFetcher, config: dict | None = None, board: dict | None = None, known=()) -> Context:
    return Context(
        source_id="test",
        config=config or {},
        fetcher=fetcher,  # type: ignore[arg-type]
        known=lambda ids: set(ids) & set(known),
        deadline=time.time() + 60,
        board=board,
    )


def test_greenhouse_list_and_detail():
    base = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    fetcher = FakeFetcher({
        base: {"jobs": [{"id": 7, "absolute_url": "https://boards.greenhouse.io/acme/jobs/7", "title": "Backend Engineer",
                         "company_name": "Acme", "location": {"name": "Tokyo"}, "first_published": "2026-10-01T00:00:00Z"}]},
        f"{base}/7": {"content": "&lt;h2&gt;Requirements&lt;/h2&gt;&lt;p&gt;Go&lt;/p&gt;"},
    })
    ctx = ctx_for(fetcher, board={"slug": "acme"})
    listing = greenhouse.list_postings(ctx, None)
    assert listing.whole_list and listing.seen_ids == {"7"}
    summary = listing.summaries[0]
    assert (summary.title, summary.company, summary.location) == ("Backend Engineer", "Acme", "Tokyo")
    assert greenhouse.fetch_detail(ctx, summary).description == "<h2>Requirements</h2><p>Go</p>"


def test_greenhouse_dead_board():
    with pytest.raises(BoardGone):
        greenhouse.list_postings(ctx_for(FakeFetcher({}), board={"slug": "gone"}), None)


def test_lever_salary_and_remote():
    fetcher = FakeFetcher({
        "https://api.lever.co/v0/postings/acme?mode=json": [{
            "id": "abc", "text": "Frontend Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc",
            "createdAt": 1_790_000_000_000, "workplaceType": "remote",
            "categories": {"location": "Tokyo", "commitment": "Full-time"},
            "description": "<p>Intro</p>", "lists": [{"text": "Requirements", "content": "<li>React</li>"}],
            "salaryRange": {"min": 8_000_000, "max": 10_000_000, "currency": "JPY", "interval": "per-year-salary"},
        }],
    })
    summary = lever.list_postings(ctx_for(fetcher, board={"slug": "acme"}), None).summaries[0]
    assert summary.location == "Tokyo; Remote"
    assert summary.fields["pay_currency"] == "JPY" and summary.fields["pay_period"] == "year"
    assert summary.fields["employment_type"] == "Full-time"
    assert "<h3>Requirements</h3><ul><li>React</li></ul>" in summary.description


def test_ashby_compensation_tiers():
    fetcher = FakeFetcher({
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true": {"jobs": [{
            "id": "j1", "title": "Engineer", "location": "Osaka", "isRemote": False, "employmentType": "FullTime",
            "jobUrl": "https://jobs.ashbyhq.com/acme/j1", "descriptionHtml": "<p>x</p>", "isListed": True,
            "compensation": {"compensationTiers": [{"components": [
                {"compensationType": "EquityPercentage", "minValue": 0.1},
                {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 90000, "maxValue": 120000},
            ]}]},
        }, {"id": "j2", "title": "Hidden", "isListed": False, "jobUrl": "x"}]},
    })
    listing = ashby.list_postings(ctx_for(fetcher, board={"slug": "acme"}), None)
    assert [s.external_id for s in listing.summaries] == ["j1"]
    assert listing.summaries[0].fields["pay_max"] == 120000


def test_workday_reads_total_from_first_page_only():
    base = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    posting = lambda n: {"title": f"Job {n}", "externalPath": f"/job/Tokyo/Job-{n}_R{n}", "locationsText": "Tokyo",
                         "postedOn": "Posted 3 Days Ago"}
    fetcher = FakeFetcher({
        f"{base}#offset=0": {"total": 25, "jobPostings": [posting(i) for i in range(20)]},
        f"{base}#offset=20": {"total": 0, "jobPostings": [posting(i) for i in range(20, 25)]},
    })
    config = {"host": "acme.wd1.myworkdayjobs.com", "tenant": "acme", "site": "External"}
    listing = workday.list_postings(ctx_for(fetcher, config=config, board={"slug": "acme"}), None)
    assert len(listing.summaries) == 25 and listing.whole_list
    assert listing.summaries[0].url == "https://acme.wd1.myworkdayjobs.com/External/job/Tokyo/Job-0_R0"
    assert listing.summaries[0].posted_at is not None


def test_job_links_from_cards_and_sitemaps():
    pattern = re.compile(r"^https://example\.com/jobs/(\d+)")
    html = '<a href="/jobs/1"><img></a><a href="/jobs/1">Backend Engineer</a><div data-href="/jobs/2">Card</div>'
    links = list(pages.job_links(html, "https://example.com/list", pattern))
    assert [i for i, _ in links] == ["1", "1", "2"]
    sitemap = '<?xml version="1.0"?><urlset><url><loc>https://example.com/jobs/9</loc></url></urlset>'
    assert [i for i, _ in pages.job_links(sitemap, "https://example.com/sitemap.xml", pattern)] == ["9"]


def test_pages_list_keeps_titled_link_and_stops_at_known_page():
    html = '<a href="/jobs/1"><img></a><a href="/jobs/1">Backend Engineer</a>'
    fetcher = FakeFetcher({
        "https://example.com/jobs": html,
        "https://example.com/jobs?page=2": '<a href="/jobs/2">Old job</a>',
        "https://example.com/jobs?page=3": '<a href="/jobs/3">Older job</a>',
    })
    config = {"list_urls": ["https://example.com/jobs"], "page_param": "page",
              "job_link": r"^https://example\.com/jobs/(\d+)"}
    listing = pages.list_postings(ctx_for(fetcher, config=config, known={"2"}), None)
    assert {s.external_id: s.title for s in listing.summaries} == {"1": "Backend Engineer", "2": "Old job"}
    assert "https://example.com/jobs?page=3" not in fetcher.requests


def test_job_posting_json_ld():
    ld = {
        "@context": "https://schema.org", "@type": "JobPosting", "title": "Platform Engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Acme KK"},
        "jobLocation": [{"address": {"addressLocality": "Osaka", "addressCountry": {"name": "JP"}}}],
        "jobLocationType": "TELECOMMUTE", "employmentType": ["FULL_TIME"], "datePosted": "2026-10-01",
        "baseSalary": {"currency": "JPY", "value": {"minValue": 7000000, "maxValue": 9000000, "unitText": "YEAR"}},
        "description": "<p>Build things</p>",
    }
    html = f'<html><script type="application/ld+json">{json.dumps({"@graph": [ld]})}</script></html>'
    summary = pages.parse_job_page(html, Summary("1", "https://example.com/jobs/1", "link text", from_link=True), {})
    assert summary.title == "Platform Engineer" and summary.company == "Acme KK"
    assert summary.location == "Osaka, JP; Remote"
    assert summary.fields["pay_min"] == 7_000_000 and summary.fields["employment_type"] == "FULL_TIME"
    assert not summary.from_link


def test_hn_comment_first_line():
    summary = hn._summary({"id": 5, "text": "Acme | Backend Engineer | Tokyo | REMOTE<p>We build...</p>"},
                          "2026-10-01T15:00:00Z")
    assert summary.company == "Acme"
    assert summary.title == "Backend Engineer"
    assert summary.location == "Tokyo | REMOTE"
    assert summary.url == "https://news.ycombinator.com/item?id=5"


def test_remoteok_skips_legal_notice():
    data = [{"legal": "terms"}, {"id": 1, "position": "Dev", "company": "X", "url": "https://remoteok.com/remote-jobs/1",
                                 "salary_min": 0, "salary_max": 0, "epoch": 1_790_000_000}]
    summaries = json_apis._remoteok(data)
    assert len(summaries) == 1 and summaries[0].fields == {}


def test_categories_keep_only_listed_ones():
    url = "https://www.workingnomads.com/api/exposed_jobs/"
    fetcher = FakeFetcher({url: [
        {"url": "https://www.workingnomads.com/jobs/dev", "title": "Backend Developer", "category_name": "Development"},
        {"url": "https://www.workingnomads.com/jobs/seo", "title": "SEO Strategist", "category_name": "Marketing"},
    ]})
    ctx = ctx_for(fetcher, {"api": "workingnomads", "url": url, "categories": ["Development"]})
    listing = json_apis.list_postings(ctx, None)
    assert [s.title for s in listing.summaries] == ["Backend Developer"]


def test_several_searches_merge_by_id():
    url = "https://jobicy.com/api/v2/remote-jobs"
    job = lambda n, geo: {"id": n, "url": f"https://jobicy.com/jobs/{n}", "jobTitle": f"Job {n}", "jobGeo": geo}
    fetcher = FakeFetcher({
        f"{url}?industry=dev": {"jobs": [job(1, "Anywhere"), job(2, "APAC")]},
        f"{url}?geo=japan&industry=dev": {"jobs": [job(2, "APAC"), job(3, "Japan")]},
    })
    ctx = ctx_for(fetcher, {"api": "jobicy", "url": url,
                            "params": [{"industry": "dev"}, {"geo": "japan", "industry": "dev"}]})
    listing = json_apis.list_postings(ctx, None)
    assert sorted(s.external_id for s in listing.summaries) == ["1", "2", "3"]
    assert len(fetcher.requests) == 2


def test_feed_place_line_after_the_teaser():
    entry = {"title": "Senior Engineer", "link": "https://startup.jobs/senior-engineer-1",
             "summary": "We build things. Key...\n\nSan Francisco, California, U.S. · $150,000 · $400,000 per year"}
    assert rss._summary(entry, {"location_in_text": True}).location == "San Francisco, California, U.S."
    assert rss._summary(entry | {"summary": "Only a teaser..."}, {"location_in_text": True}).location == ""


def test_feed_title_with_the_company_after_at():
    entry = {"title": "Head of Data at Chess.com", "link": "https://example.com/1", "author": "Chess.com"}
    summary = rss._summary(entry, {"title_format": "title at company"})
    assert (summary.title, summary.company) == ("Head of Data", "Chess.com")


def test_detail_browser_sends_job_pages_through_the_browser():
    assert detail_loader(json_apis, {}) is json_apis
    assert detail_loader(json_apis, {"detail_browser": True}).__name__ == "jobhunt.collectors.browser"


def test_amazon_date():
    assert json_apis._amazon_date("October  7, 2026") == "2026-10-07"


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://jobs.lever.co/Acme/123-abc/apply?lever-source=x", ("lever", "acme", {})),
        ("https://boards.greenhouse.io/embed/job_board/js?for=hdeen", ("greenhouse", "hdeen", {})),
        ("https://job-boards.greenhouse.io/paidyinc/jobs/1", ("greenhouse", "paidyinc", {})),
        ("https://jobs.ashbyhq.com/notion", ("ashby", "notion", {})),
        ("https://apply.workable.com/j/ABC123", None),
        ("https://rakuten.wd1.myworkdayjobs.com/en-US/RakutenInc/job/Tokyo/X_1",
         ("workday", "rakuten.wd1.myworkdayjobs.com/RakutenInc",
          {"host": "rakuten.wd1.myworkdayjobs.com", "tenant": "rakuten", "site": "RakutenInc"})),
        ("https://example.com/careers", None),
    ],
)
def test_board_from_url(url, board):
    assert board_from_url(url) == board

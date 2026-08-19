"""
Companies House API client with HTTP Basic auth and rate limiting.

Setup:
    1. Register for a free API key at:
       https://developer.company-information.service.gov.uk/manage-applications
    2. Set it as an environment variable before running any script:
       export CH_API_KEY="your_key_here"        (macOS/Linux)
       setx CH_API_KEY "your_key_here"           (Windows, new shell needed after)

Companies House uses HTTP Basic auth: the API key is the username, the
password is left blank. Base URL: https://api.company-information.service.gov.uk
Published rate limit: 600 requests per 5 minutes (roughly 2/sec). This
client throttles conservatively below that ceiling rather than relying
on the API's own 429 response.
"""

import os
import time
import requests
from collections import deque

BASE_URL = "https://api.company-information.service.gov.uk"


class RateLimiter:
    """Keeps outbound requests under a rolling per-window cap."""

    def __init__(self, max_requests=550, window_seconds=300):
        # 550 per 5 min leaves headroom under the published 600/5min limit.
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps = deque()

    def wait_if_needed(self):
        now = time.monotonic()
        while self.timestamps and now - self.timestamps[0] > self.window_seconds:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.max_requests:
            sleep_for = self.window_seconds - (now - self.timestamps[0]) + 0.1
            time.sleep(max(sleep_for, 0))
        self.timestamps.append(time.monotonic())


class CompaniesHouseClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("CH_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "No Companies House API key found. Set the CH_API_KEY "
                "environment variable before running this script."
            )
        self.session = requests.Session()
        self.session.auth = (self.api_key, "")
        self.limiter = RateLimiter()

    def _get(self, path, params=None):
        self.limiter.wait_if_needed()
        url = f"{BASE_URL}{path}"
        response = self.session.get(url, params=params, timeout=15)
        if response.status_code == 404:
            return None
        if response.status_code == 429:
            # Should not normally trigger given the rate limiter above,
            # but back off and retry once if the API disagrees with our count.
            time.sleep(5)
            response = self.session.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()

    def company_profile(self, company_number):
        return self._get(f"/company/{company_number}")

    def officers(self, company_number):
        return self._get(f"/company/{company_number}/officers")

    def filing_history(self, company_number, items_per_page=100):
        return self._get(
            f"/company/{company_number}/filing-history",
            params={"items_per_page": items_per_page},
        )

    def charges(self, company_number):
        return self._get(f"/company/{company_number}/charges")

    def insolvency(self, company_number):
        return self._get(f"/company/{company_number}/insolvency")

    def persons_with_significant_control(self, company_number, items_per_page=100):
        return self._get(
            f"/company/{company_number}/persons-with-significant-control",
            params={"items_per_page": items_per_page},
        )

    def search_companies(self, query, items_per_page=20):
        return self._get(
            "/search/companies",
            params={"q": query, "items_per_page": items_per_page},
        )

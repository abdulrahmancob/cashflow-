"""RevFlow reports API client and EOB catalog builder."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from playwright.async_api import APIRequestContext, BrowserContext, Page

from config import (
    API_BASE_URL,
    BILLING_BASE_URL,
    COMPANY_EOB_LOG_REPORT_ID,
    EOB_DETAIL_REPORT_ID,
    OPEN_835_REPORT_ID,
    RevFlowConfig,
)
from logging_config import get_logger

log = get_logger("reports_api")

HREF_RE = re.compile(
    r'href\s*=\s*"([^"]+)"|href\s*=\s*\'([^\']+)\'|href\s*=\s*([^\s>]+)',
    re.IGNORECASE,
)


@dataclass
class ReportParams:
    rid: str
    from_date: str
    to_date: str
    clinic_code: str = "PV4"
    company_id: str = ""
    eob_key: str = ""
    check_eft_num: str = ""
    payor: str = ""
    eob_date: str = ""

    def to_query(self) -> dict[str, str]:
        params: dict[str, str] = {
            "rid": self.rid,
            "FDate": self.from_date,
            "Tdate": self.to_date,
            "cliniccode": self.clinic_code,
        }
        if self.company_id:
            params["company_id"] = self.company_id
        if self.eob_key:
            params["eob_key"] = self.eob_key
        if self.check_eft_num:
            params["check_eft_num"] = self.check_eft_num
        if self.payor:
            params["Payor"] = self.payor
        if self.eob_date:
            params["eob_date"] = self.eob_date
        return params

    def to_silversurfer_overrides(self) -> dict[str, str]:
        overrides: dict[str, str] = {
            "Fdate": self.from_date,
            "Tdate": self.to_date,
            "cliniccode": self.clinic_code,
        }
        if self.company_id:
            overrides["company_id"] = self.company_id
        if self.eob_key:
            overrides["eob_key"] = self.eob_key
        if self.check_eft_num:
            overrides["check_eft_num"] = self.check_eft_num
        if self.payor:
            overrides["Payor"] = self.payor
        if self.eob_date:
            overrides["eob_date"] = self.eob_date
        return overrides

    def ui_url(self) -> str:
        return _encoded_report_path(
            f"{BILLING_BASE_URL}/report/sub_report_data",
            self.to_query(),
        )

    def report_data_ui_url(self) -> str:
        return f"{BILLING_BASE_URL}/report/report_data"


@dataclass
class EobCatalogEntry:
    company_id: str
    company_code: str
    company_name: str
    from_date: str
    to_date: str
    clinic_code: str
    eob_key: str
    check_eft_num: str
    payor: str
    eob_date: str
    detail_rid: str = str(EOB_DETAIL_REPORT_ID)
    detail_url: str = ""
    row_data: dict = field(default_factory=dict)

    @property
    def selection_key(self) -> str:
        return f"{self.company_id}|{self.eob_key}|{self.check_eft_num}|{self.eob_date}"

    def to_selection_dict(self) -> dict:
        return {
            "company_id": self.company_id,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "clinic_code": self.clinic_code,
            "eob_key": self.eob_key,
            "check_eft_num": self.check_eft_num,
            "payor": self.payor,
            "eob_date": self.eob_date,
            "detail_rid": self.detail_rid,
        }


def parse_report_link(html_value: str) -> dict[str, str]:
    if not html_value:
        return {}
    text = unescape(html_value)
    match = HREF_RE.search(text)
    if not match:
        return {}
    href = next(g for g in match.groups() if g)
    query = href.split("?", 1)[-1] if "?" in href else href
    parsed = parse_qs(query, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _encoded_report_path(base: str, params: dict[str, str]) -> str:
    inner = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{base}?{quote(inner, safe='')}"


def _api_headers(bearer_token: str, *, silversurfer: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if silversurfer:
        headers["Silversurfer"] = silversurfer
    return headers


def _metadata_param_list(metadata: dict) -> list[dict]:
    for key in ("reportParameters", "ReportParameters", "parameters", "params", "silversurfer"):
        value = metadata.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict) and "name" in value[0]:
            return [dict(item) for item in value]

    for value in metadata.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "name" in value[0]:
            return [dict(item) for item in value]

    return []


def build_silversurfer(metadata: dict, overrides: dict[str, str]) -> str:
    params = _metadata_param_list(metadata)
    lookup = {key.lower(): value for key, value in overrides.items() if value}

    if not params:
        params = [
            {"name": key, "default_value": value, "param_name": f"@{key}"}
            for key, value in overrides.items()
            if value
        ]
    else:
        for param in params:
            name = str(param.get("name", "")).lower()
            if name in lookup:
                param["default_value"] = lookup[name]

    return json.dumps(params, separators=(",", ":"))


_RETRYABLE_STATUSES = {500, 502, 503, 504}
_MAX_RETRIES = 3


def _truncate_body(text: str, limit: int = 300) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _valid_report_payload(data) -> bool:
    return isinstance(data, dict)


def _require_report_payload(data, *, label: str) -> dict:
    if not _valid_report_payload(data):
        raise RuntimeError(f"{label} returned no report payload")
    return data


def _params_from_link(link_params: dict, *, rid: str, fallback: ReportParams) -> ReportParams:
    return ReportParams(
        rid=rid,
        from_date=link_params.get("FDate") or fallback.from_date,
        to_date=link_params.get("Tdate") or fallback.to_date,
        clinic_code=link_params.get("cliniccode") or fallback.clinic_code,
        company_id=link_params.get("company_id") or fallback.company_id,
    )


async def _page_snapshot(page: Page) -> str:
    try:
        title = await page.title()
    except Exception:
        title = "(unavailable)"
    return f"url={page.url!r} title={title!r}"


class ReportsClient:
    def __init__(
        self,
        request: APIRequestContext,
        bearer_token: str,
        config: RevFlowConfig,
    ) -> None:
        self.request = request
        self.bearer_token = bearer_token
        self.config = config
        self.headers = _api_headers(bearer_token)
        self._metadata_cache: dict[int, dict] = {}

    async def _get_with_retry(
        self,
        url: str,
        *,
        label: str,
        headers: dict[str, str] | None = None,
    ) -> dict:
        request_headers = headers or self.headers
        last_error: RuntimeError | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            resp = await self.request.get(url, headers=request_headers)
            if resp.ok:
                return await resp.json()

            body = await resp.text()
            last_error = RuntimeError(f"{label} failed: {resp.status} {body}")
            if resp.status not in _RETRYABLE_STATUSES or attempt == _MAX_RETRIES:
                raise last_error

            delay = 2 ** attempt
            log.warning(
                "%s returned %s (attempt %d/%d) — retrying in %ds: %s",
                label,
                resp.status,
                attempt,
                _MAX_RETRIES,
                delay,
                _truncate_body(body),
            )
            await asyncio.sleep(delay)

        raise last_error or RuntimeError(f"{label} failed")

    async def get_report_metadata(self, report_id: int) -> dict:
        if report_id in self._metadata_cache:
            return self._metadata_cache[report_id]

        url = f"{API_BASE_URL}/v1/reports/report_metadata/{report_id}"
        resp = await self.request.get(url, headers=self.headers)
        if not resp.ok:
            raise RuntimeError(f"report_metadata/{report_id} failed: {resp.status} {await resp.text()}")
        data = (await resp.json()).get("data", {})
        self._metadata_cache[report_id] = data
        return data

    async def get_report_data(
        self,
        report_id: int,
        *,
        from_date: str,
        to_date: str,
        clinic_code: str | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> dict:
        metadata = await self.get_report_metadata(report_id)
        overrides: dict[str, str] = {
            "Fdate": from_date,
            "Tdate": to_date,
            "UserId": self.config.user_id or "",
        }
        if clinic_code or self.config.clinic_code:
            overrides["cliniccode"] = clinic_code or self.config.clinic_code
        if extra_params:
            overrides.update(extra_params)

        silversurfer = build_silversurfer(metadata, overrides)
        url = f"{API_BASE_URL}/v1/reports/report_data/{report_id}"
        headers = _api_headers(self.bearer_token, silversurfer=silversurfer)
        body = await self._get_with_retry(
            url,
            label=f"report_data/{report_id}",
            headers=headers,
        )
        return _require_report_payload(body.get("data", body), label=f"report_data/{report_id}")

    async def get_sub_report_data(self, params: ReportParams) -> dict:
        metadata = await self.get_report_metadata(int(params.rid))
        overrides = params.to_silversurfer_overrides()
        if self.config.user_id:
            overrides.setdefault("UserId", self.config.user_id)

        silversurfer = build_silversurfer(metadata, overrides)
        url = f"{API_BASE_URL}/v1/reports/sub_report_data?{urlencode(params.to_query())}"
        headers = _api_headers(self.bearer_token, silversurfer=silversurfer)
        body = await self._get_with_retry(url, label="sub_report_data", headers=headers)
        return _require_report_payload(body.get("data", body), label="sub_report_data")

    async def fetch_via_page(
        self,
        page: Page,
        ui_url: str,
        *,
        expect_url_fragment: str | None = None,
    ) -> dict:
        captured: dict = {}
        last_match: dict[str, str | int] = {}

        def _matches_expectation(response_url: str) -> bool:
            if not expect_url_fragment:
                return True
            return expect_url_fragment in response_url

        async def on_response(response) -> None:
            if "r6prodgoldna.revflow.com/v1/reports/" not in response.url:
                return

            log.debug(
                "Report API response: status=%s url=%s",
                response.status,
                response.url,
            )

            if expect_url_fragment and expect_url_fragment not in response.url:
                return

            body_text = ""
            try:
                body_text = await response.text()
            except Exception:
                pass

            if response.status != 200:
                last_match.update(
                    {
                        "url": response.url,
                        "status": response.status,
                        "body": _truncate_body(body_text),
                    }
                )
                return

            try:
                body = json.loads(body_text) if body_text else await response.json()
            except Exception:
                return

            if not isinstance(body, dict) or "data" not in body:
                return

            if expect_url_fragment and not _matches_expectation(response.url):
                return

            payload = body["data"]
            if not isinstance(payload, dict):
                return

            captured["data"] = payload
            captured["url"] = response.url

        page.on("response", on_response)
        try:
            await page.goto(ui_url, wait_until="domcontentloaded", timeout=120_000)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "data" not in captured:
                await asyncio.sleep(0.3)
            if "data" not in captured:
                footer = page.locator("#applicationFooterSticky, #export_report_button")
                if await footer.count() > 0:
                    await asyncio.sleep(2)
        finally:
            page.remove_listener("response", on_response)

        if captured:
            return captured["data"]

        snapshot = await _page_snapshot(page)
        if last_match:
            fragment = expect_url_fragment or "report API"
            raise RuntimeError(
                f"Browser fallback failed for {fragment}: "
                f"last response {last_match.get('status')} — {last_match.get('body')} | {snapshot}"
            )
        raise RuntimeError(f"No report API response captured for {ui_url} | {snapshot}")


def _report_rows(report_data: dict | None) -> list[dict]:
    if not isinstance(report_data, dict):
        return []
    rows = report_data.get("ReportRows") or []
    return rows if isinstance(rows, list) else []


def _row_to_company_entry(
    row: dict,
    *,
    from_date: str,
    to_date: str,
    clinic_code: str,
    default_company_id: str,
) -> tuple[str, str, str, dict]:
    link_params = parse_report_link(row.get("stringCol0", ""))
    company_id = link_params.get("company_id", default_company_id)
    company_code = link_params.get("cliniccode") or row.get("stringCol0", "")
    if "<" in company_code:
        bold = re.search(r"<b>([^<]+)</b>", company_code)
        company_code = bold.group(1) if bold else company_code
    company_name = row.get("stringCol1", "")
    return company_id, company_code, company_name, link_params


def build_eob_catalog(
    *,
    from_date: str,
    to_date: str,
    clinic_code: str,
    company_id: str,
    open_835_rows: list[dict],
    company_rows: list[dict],
) -> list[EobCatalogEntry]:
    catalog: list[EobCatalogEntry] = []
    seen: set[str] = set()

    company_meta: dict[str, tuple[str, str]] = {}
    for row in open_835_rows:
        cid, code, name, _ = _row_to_company_entry(
            row,
            from_date=from_date,
            to_date=to_date,
            clinic_code=clinic_code,
            default_company_id=company_id,
        )
        company_meta[cid or company_id] = (code, name)

    for row in company_rows:
        link_params = parse_report_link(row.get("stringCol0", ""))
        eob_key = link_params.get("eob_key", "")
        check_num = link_params.get("check_eft_num") or row.get("stringCol0", "")
        if "<" in str(check_num):
            bold = re.search(r"<b>([^<]+)</b>", str(check_num))
            check_num = bold.group(1) if bold else check_num
        payor = link_params.get("Payor") or row.get("stringCol1", "")
        eob_date = link_params.get("eob_date") or row.get("stringCol2", "")
        cid = link_params.get("company_id") or company_id
        code, name = company_meta.get(cid, (clinic_code, ""))

        entry = EobCatalogEntry(
            company_id=cid,
            company_code=code,
            company_name=name,
            from_date=from_date,
            to_date=to_date,
            clinic_code=link_params.get("cliniccode") or clinic_code,
            eob_key=eob_key,
            check_eft_num=str(check_num),
            payor=payor,
            eob_date=eob_date,
            detail_url=ReportParams(
                rid=str(EOB_DETAIL_REPORT_ID),
                from_date=from_date,
                to_date=to_date,
                clinic_code=link_params.get("cliniccode") or clinic_code,
                company_id=cid,
                eob_key=eob_key,
                check_eft_num=str(check_num),
                payor=payor,
                eob_date=eob_date,
            ).ui_url(),
            row_data={k: v for k, v in row.items() if not str(k).startswith("_")},
        )
        if entry.selection_key in seen:
            continue
        seen.add(entry.selection_key)
        catalog.append(entry)

    return catalog


async def discover_eobs(
    page: Page,
    context: BrowserContext,
    client: ReportsClient,
    config: RevFlowConfig,
    *,
    from_date: str,
    to_date: str,
) -> list[EobCatalogEntry]:
    log.info("Fetching Open 835s report metadata (id=%s)", OPEN_835_REPORT_ID)
    await client.get_report_metadata(OPEN_835_REPORT_ID)

    log.info("Fetching Open 835s data %s – %s", from_date, to_date)
    try:
        open_data = await client.get_report_data(
            OPEN_835_REPORT_ID,
            from_date=from_date,
            to_date=to_date,
        )
    except RuntimeError as exc:
        log.warning("Direct API report_data failed (%s) — falling back to browser capture", exc)
        open_data = await client.fetch_via_page(
            page,
            ReportParams(
                rid=str(OPEN_835_REPORT_ID),
                from_date=from_date,
                to_date=to_date,
                clinic_code=config.clinic_code,
            ).report_data_ui_url(),
            expect_url_fragment=f"report_data/{OPEN_835_REPORT_ID}",
        )

    open_rows = _report_rows(open_data)
    if not open_rows:
        log.warning("No company rows in Open 835s report")

    default_company_id = config.company_id
    all_entries: list[EobCatalogEntry] = []

    for row in open_rows:
        cid, code, name, link_params = _row_to_company_entry(
            row,
            from_date=from_date,
            to_date=to_date,
            clinic_code=config.clinic_code,
            default_company_id=config.company_id,
        )
        cid = cid or default_company_id
        company_params = _params_from_link(
            link_params,
            rid=str(COMPANY_EOB_LOG_REPORT_ID),
            fallback=ReportParams(
                rid=str(COMPANY_EOB_LOG_REPORT_ID),
                from_date=from_date,
                to_date=to_date,
                clinic_code=config.clinic_code,
                company_id=cid,
            ),
        )
        log.info(
            "Fetching EOB log for company %s (%s) via %s",
            cid,
            name or code,
            company_params.ui_url(),
        )

        try:
            company_data = await client.get_sub_report_data(company_params)
        except RuntimeError as exc:
            log.warning("Direct API sub_report_data failed (%s) — browser fallback", exc)
            company_data = await client.fetch_via_page(
                page,
                company_params.ui_url(),
                expect_url_fragment="sub_report_data",
            )

        company_rows = _report_rows(company_data)
        entries = build_eob_catalog(
            from_date=company_params.from_date,
            to_date=company_params.to_date,
            clinic_code=config.clinic_code,
            company_id=cid,
            open_835_rows=[row],
            company_rows=company_rows,
        )
        log.info("  Found %d EOB(s) for company %s", len(entries), cid)
        all_entries.extend(entries)

    return all_entries


def write_eob_catalog(path: Path, entries: list[EobCatalogEntry], *, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta or {},
        "count": len(entries),
        "entries": [asdict(e) for e in entries],
        "selections_template": [e.to_selection_dict() for e in entries],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("Wrote EOB catalog (%d entries) to %s", len(entries), path)


def load_selections(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if "selections" in raw:
            return raw["selections"]
        if "selections_template" in raw:
            return raw["selections_template"]
        if "entries" in raw:
            return [
                {
                    "company_id": e.get("company_id"),
                    "from_date": e.get("from_date"),
                    "to_date": e.get("to_date"),
                    "clinic_code": e.get("clinic_code"),
                    "eob_key": e.get("eob_key"),
                    "check_eft_num": e.get("check_eft_num"),
                    "payor": e.get("payor"),
                    "eob_date": e.get("eob_date"),
                    "detail_rid": e.get("detail_rid", str(EOB_DETAIL_REPORT_ID)),
                }
                for e in raw["entries"]
            ]
    raise ValueError(f"Unrecognized selections file format: {path}")

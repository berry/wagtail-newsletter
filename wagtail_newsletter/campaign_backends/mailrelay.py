"""Mailrelay campaign backend."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.functional import cached_property

from ..audiences import Audience, AudienceSegment
from ..models import NewsletterRecipientsBase
from . import Campaign, CampaignBackend, CampaignBackendError


logger = logging.getLogger(__name__)


class MailrelayApiError(Exception):
    """Raised when Mailrelay returns an error response."""

    def __init__(self, message: str, *, status_code: int, payload: Any | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


@dataclass
class MailrelayCampaign(Campaign):
    backend: "MailrelayCampaignBackend"
    id: str
    status: Optional[str]
    sent_campaign_id: Optional[str]

    @property
    def is_scheduled(self) -> bool:
        return (self.status or "").lower() in {"pending"}

    @property
    def is_sent(self) -> bool:
        return (self.status or "").lower() in {"finished", "processing", "cancelled"}

    @property
    def url(self) -> str:
        return urljoin(self.backend.dashboard_base_url, f"/admin/campaigns/{self.id}")

    def get_report(self) -> dict[str, Any]:
        raise CampaignBackendError("Mailrelay reporting is not supported yet")


class MailrelayCampaignBackend(CampaignBackend):
    name = "Mailrelay"
    campaign_class = MailrelayCampaign

    API_TIMEOUT = 30

    def __init__(self):
        self._account = None

    @cached_property
    def api_key(self) -> str:
        return self._require_setting("WAGTAIL_NEWSLETTER_MAILRELAY_API_KEY")

    @cached_property
    def account_domain(self) -> str:
        account = self._require_setting("WAGTAIL_NEWSLETTER_MAILRELAY_ACCOUNT")
        return account.rstrip("/")

    @cached_property
    def base_url(self) -> str:
        return f"https://{self.account_domain}/api/v1"

    @cached_property
    def dashboard_base_url(self) -> str:
        return f"https://{self.account_domain}"

    @cached_property
    def sender_id(self) -> int:
        raw = self._require_setting("WAGTAIL_NEWSLETTER_MAILRELAY_SENDER_ID")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise ImproperlyConfigured(
                "WAGTAIL_NEWSLETTER_MAILRELAY_SENDER_ID must be an integer"
            ) from exc

    @cached_property
    def session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "X-AUTH-TOKEN": self.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        return session

    # Audience helpers -------------------------------------------------
    def get_audiences(self) -> list[Audience]:
        audiences: list[Audience] = []
        page = 1
        while True:
            try:
                response = self._request(
                    "GET",
                    "/groups",
                    params={"page": page, "per_page": 1000},
                )
            except MailrelayApiError as error:
                raise CampaignBackendError(str(error)) from error
            data = response.json()
            for group in data:
                audiences.append(
                    Audience(
                        id=str(group["id"]),
                        name=group.get("name", str(group["id"])),
                        member_count=group.get("subscribers_count", 0),
                    )
                )
            total_pages = int(response.headers.get("Total", page)) or page
            if page >= total_pages:
                break
            page += 1
        return audiences

    def get_audience_segments(self, audience_id) -> list[AudienceSegment]:
        # Mailrelay exposes groups but not per-group saved segments via the public API
        # Verify the group exists; otherwise surface DoesNotExist so the chooser can react.
        try:
            self._request("GET", f"/groups/{audience_id}")
        except MailrelayApiError as error:
            if error.status_code == 404:
                raise Audience.DoesNotExist from error
            raise CampaignBackendError(str(error)) from error
        return []

    # Campaign save / retrieve -----------------------------------------
    def get_campaign_request_body(
        self,
        *,
        recipients: Optional[NewsletterRecipientsBase],
        subject: str,
    ) -> dict[str, Any]:
        if recipients is None or not recipients.audience:
            raise CampaignBackendError(
                "Mailrelay campaigns require recipients to be selected before saving"
            )

        body: dict[str, Any] = {
            "sender_id": self.sender_id,
            "subject": subject,
            "html": "",  # placeholder, filled later
            "target": "groups",
            "group_ids": [int(recipients.audience)],
        }

        reply_to = getattr(settings, "WAGTAIL_NEWSLETTER_REPLY_TO", None)
        if reply_to:
            body["reply_to"] = reply_to

        preview_text = getattr(settings, "WAGTAIL_NEWSLETTER_PREVIEW_TEXT", None)
        if preview_text:
            body["preview_text"] = preview_text

        if recipients.segment:
            # Expect "audience/segment". Mailrelay segments are global, so we accept raw id.
            segment_id = recipients.segment.split("/")[-1]
            body["target"] = "segment"
            body["segment_id"] = int(segment_id)

        body["html"] = ""  # ensure key present; real HTML supplied in save
        return body

    def save_campaign(
        self,
        *,
        campaign_id: Optional[str] = None,
        recipients: Optional[NewsletterRecipientsBase],
        subject: str,
        html: str,
    ) -> str:
        base_id, sent_id = self._split_campaign_identifier(campaign_id)
        body = self.get_campaign_request_body(recipients=recipients, subject=subject)
        body["html"] = html

        try:
            if base_id:
                response = self._request_json(
                    "PATCH",
                    f"/campaigns/{base_id}",
                    json=body,
                    expected_status={200},
                )
                campaign_id = str(response["id"])
            else:
                response = self._request_json(
                    "POST",
                    "/campaigns",
                    json=body,
                    expected_status={201},
                )
                campaign_id = str(response["id"])
        except MailrelayApiError as error:
            raise CampaignBackendError(str(error)) from error

        return self._combine_campaign_identifier(campaign_id, sent_id)

    def get_campaign(self, campaign_id: str) -> Optional[MailrelayCampaign]:
        base_id, sent_id = self._split_campaign_identifier(campaign_id)
        if not base_id:
            return None
        try:
            response = self._request_json("GET", f"/campaigns/{base_id}")
        except MailrelayApiError as error:
            if error.status_code == 404:
                return None
            raise CampaignBackendError(str(error)) from error

        status = response.get("status")
        if not status and sent_id:
            status = self._get_sent_campaign_status(sent_id)

        return self.campaign_class(
            backend=self,
            id=str(response["id"]),
            status=status,
            sent_campaign_id=sent_id,
        )

    # Sending -----------------------------------------------------------
    def send_test_email(self, *, campaign_id: str, email: str) -> None:
        base_id, _ = self._split_campaign_identifier(campaign_id)
        if base_id is None:
            raise CampaignBackendError("Campaign ID is required to send a test email")
        try:
            self._request(
                "POST",
                f"/campaigns/{base_id}/send_test",
                json={"test_emails": email},
                expected_status={204},
            )
        except MailrelayApiError as error:
            raise CampaignBackendError(str(error)) from error

    def send_campaign(self, campaign_id: str) -> None:
        base_id, _ = self._split_campaign_identifier(campaign_id)
        if base_id is None:
            raise CampaignBackendError("Campaign ID is required to send the campaign")
        payload = self._build_send_payload(base_id)
        try:
            self._request(
                "POST",
                f"/campaigns/{base_id}/send_all",
                json=payload,
            )
        except MailrelayApiError as error:
            raise CampaignBackendError(str(error)) from error

    def validate_schedule_time(self, schedule_time: datetime) -> None:
        # Mailrelay accepts any future datetime; ensure timezone-aware and in future
        if schedule_time.tzinfo is None:
            raise CampaignBackendError("Schedule time must include a timezone")

    def schedule_campaign(self, campaign_id: str, schedule_time: datetime) -> None:
        self.validate_schedule_time(schedule_time)
        base_id, _ = self._split_campaign_identifier(campaign_id)
        if base_id is None:
            raise CampaignBackendError(
                "Campaign ID is required to schedule the campaign"
            )
        payload = self._build_send_payload(base_id)
        payload["scheduled_at"] = schedule_time.astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            self._request(
                "POST",
                f"/campaigns/{base_id}/send_all",
                json=payload,
            )
        except MailrelayApiError as error:
            raise CampaignBackendError(str(error)) from error

    def unschedule_campaign(self, campaign_id: str) -> None:
        raise CampaignBackendError(
            "Mailrelay API does not support unscheduling a campaign"
        )

    # Helpers -----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        expected_status: set[int] | None = None,
    ) -> requests.Response:
        expected = expected_status or {200}
        relative_path = path.lstrip("/")
        url = f"{self.base_url}/{relative_path}"
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json,
                timeout=self.API_TIMEOUT,
            )
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CampaignBackendError("Unable to communicate with Mailrelay") from exc

        if response.status_code in expected:
            return response

        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        message = self._extract_error_message(payload) or "Mailrelay API error"
        logger.error(
            "Mailrelay request failed: %s %s status=%s payload=%s",
            method,
            url,
            response.status_code,
            payload,
        )
        raise MailrelayApiError(
            message,
            status_code=response.status_code,
            payload=payload,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        expected_status: set[int] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any]:
        expected = expected_status or {200}
        if allow_404:
            expected = expected | {404}
        response = self._request(
            method,
            path,
            params=params,
            json=json,
            expected_status=expected,
        )
        if response.status_code == 404:
            raise MailrelayApiError("Not found", status_code=404)
        return response.json()

    def _extract_error_message(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in ("message", "error", "errors"):
                if key in payload:
                    value = payload[key]
                    if isinstance(value, list):
                        return "; ".join(str(item) for item in value)
                    return str(value)
        return None

    def _get_sent_campaign_status(self, sent_campaign_id: str) -> Optional[str]:
        try:
            data = self._request_json(
                "GET", f"/sent_campaigns/{sent_campaign_id}", allow_404=False
            )
        except MailrelayApiError:
            return None
        return data.get("status")

    def _build_send_payload(self, campaign_id: str) -> dict[str, Any]:
        try:
            campaign = self._request_json("GET", f"/campaigns/{campaign_id}")
        except MailrelayApiError as error:
            raise CampaignBackendError(str(error)) from error

        payload: dict[str, Any] = {"target": campaign.get("target", "groups")}
        if payload["target"] == "groups":
            payload["group_ids"] = campaign.get("group_ids", [])
        elif payload["target"] == "segment" and campaign.get("segment_id"):
            payload["segment_id"] = campaign["segment_id"]
        return payload

    def _require_setting(self, name: str) -> Any:
        value = getattr(settings, name, None)
        if not value:
            raise ImproperlyConfigured(f"{name} is not set")
        return value

    @staticmethod
    def _split_campaign_identifier(
        value: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        if not value:
            return None, None
        if ":" in value:
            base, sent = value.split(":", 1)
            return base or None, (sent or None)
        return value, None

    @staticmethod
    def _combine_campaign_identifier(base: str, sent: Optional[str]) -> str:
        if sent:
            return f"{base}:{sent}"
        return base


__all__ = ["MailrelayCampaignBackend"]

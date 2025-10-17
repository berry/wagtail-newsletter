import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
import requests

from wagtail_newsletter.audiences import Audience
from wagtail_newsletter.campaign_backends import CampaignBackendError
from wagtail_newsletter.campaign_backends.mailrelay import (
    MailrelayCampaignBackend,
    MailrelayApiError,
)


def make_response(payload, *, status=200, headers=None):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    response.headers.update(headers or {})
    response.encoding = "utf-8"
    return response


class FakeSession(requests.Session):
    def __init__(self, responses: list[requests.Response]):
        super().__init__()
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, params=None, json=None, timeout=None):  # type: ignore[override]
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
            }
        )
        if not self.responses:
            raise AssertionError("No fake response configured for request")
        return self.responses.pop(0)


@pytest.fixture
def mailrelay_settings(settings):
    settings.WAGTAIL_NEWSLETTER_MAILRELAY_API_KEY = "secret"
    settings.WAGTAIL_NEWSLETTER_MAILRELAY_ACCOUNT = "example.ipzmarketing.com"
    settings.WAGTAIL_NEWSLETTER_MAILRELAY_SENDER_ID = 101
    settings.WAGTAIL_NEWSLETTER_REPLY_TO = "reply@example.com"
    return settings


def make_backend(_settings, responses):
    backend = MailrelayCampaignBackend()
    backend.__dict__["session"] = FakeSession(responses)
    return backend


def test_get_audiences(mailrelay_settings):
    responses = [
        make_response(
            [
                {"id": 1, "name": "Group A", "subscribers_count": 12},
            ],
            headers={"Total": "2"},
        ),
        make_response(
            [
                {"id": 2, "name": "Group B", "subscribers_count": 4},
            ],
            headers={"Total": "2"},
        ),
    ]
    backend = make_backend(mailrelay_settings, responses)

    audiences = backend.get_audiences()

    assert audiences == [
        Audience(id="1", name="Group A", member_count=12),
        Audience(id="2", name="Group B", member_count=4),
    ]
    session = cast(FakeSession, backend.session)
    assert session.calls[0]["params"] == {"page": 1, "per_page": 1000}
    assert session.calls[1]["params"] == {"page": 2, "per_page": 1000}


def test_get_audiences_api_error(mailrelay_settings):
    responses = [
        make_response({"message": "boom"}, status=500),
    ]
    backend = make_backend(mailrelay_settings, responses)

    with pytest.raises(CampaignBackendError) as exc:
        backend.get_audiences()

    assert "boom" in str(exc.value)


def test_get_audience_segments_group_missing(mailrelay_settings):
    responses = [
        make_response({"message": "not found"}, status=404),
    ]
    backend = make_backend(mailrelay_settings, responses)

    with pytest.raises(Audience.DoesNotExist):
        backend.get_audience_segments("123")


def test_get_audience_segments_error(mailrelay_settings):
    responses = [
        make_response({"message": "boom"}, status=500),
    ]
    backend = make_backend(mailrelay_settings, responses)

    with pytest.raises(CampaignBackendError):
        backend.get_audience_segments("123")


def test_save_campaign_creates(mailrelay_settings):
    create_response = make_response({"id": 55})
    backend = make_backend(mailrelay_settings, [create_response])
    recipients = cast(Any, SimpleNamespace(audience="12", segment=None))

    campaign_id = backend.save_campaign(
        recipients=recipients,
        subject="Test Subject",
        html="<p>Hello</p>",
    )

    assert campaign_id == "55"
    call = cast(FakeSession, backend.session).calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/v1/campaigns")
    assert call["json"] == {
        "sender_id": 101,
        "subject": "Test Subject",
        "html": "<p>Hello</p>",
        "target": "groups",
        "group_ids": [12],
        "reply_to": "reply@example.com",
    }


def test_save_campaign_updates_and_preserves_sent_id(mailrelay_settings):
    update_response = make_response({"id": 99})
    backend = make_backend(mailrelay_settings, [update_response])
    recipients = cast(Any, SimpleNamespace(audience="34", segment="34/7"))

    campaign_id = backend.save_campaign(
        campaign_id="55:900",
        recipients=recipients,
        subject="Segment Subject",
        html="<h1>Hi</h1>",
    )

    assert campaign_id == "99:900"
    call = cast(FakeSession, backend.session).calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/api/v1/campaigns/55")
    assert call["json"]["target"] == "segment"
    assert call["json"]["segment_id"] == 7


def test_save_campaign_error(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings, [make_response({"message": "boom"}, status=400)]
    )
    recipients = cast(Any, SimpleNamespace(audience="12", segment=None))

    with pytest.raises(CampaignBackendError) as exc:
        backend.save_campaign(
            recipients=recipients,
            subject="Subject",
            html="<p>Hello</p>",
        )

    assert "boom" in str(exc.value)


def test_get_campaign_not_found_returns_none(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [make_response({"message": "not found"}, status=404)],
    )

    assert backend.get_campaign("42") is None


def test_get_campaign_error(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [make_response({"message": "boom"}, status=500)],
    )

    with pytest.raises(CampaignBackendError):
        backend.get_campaign("42")


def test_send_test_email(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [make_response({}, status=204)],
    )

    backend.send_test_email(campaign_id="10", email="test@example.com")

    call = cast(FakeSession, backend.session).calls[0]
    assert call["url"].endswith("/api/v1/campaigns/10/send_test")
    assert call["json"] == {"test_emails": "test@example.com"}


def test_send_campaign_uses_campaign_configuration(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [
            make_response({"id": 10, "target": "segment", "segment_id": 77}),
            make_response({"id": 1}),
        ],
    )

    backend.send_campaign("10")

    # call 0 is GET /campaigns
    send_call = cast(FakeSession, backend.session).calls[1]
    assert send_call["url"].endswith("/api/v1/campaigns/10/send_all")
    assert send_call["json"] == {"target": "segment", "segment_id": 77}


def test_send_campaign_error(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [
            make_response({"id": 10, "target": "groups", "group_ids": [12]}),
            make_response({"message": "boom"}, status=500),
        ],
    )

    with pytest.raises(CampaignBackendError):
        backend.send_campaign("10")


def test_schedule_campaign_sets_scheduled_at(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [
            make_response({"id": 10, "target": "groups", "group_ids": [12]}),
            make_response({"id": 1}),
        ],
    )
    schedule_time = datetime(2024, 10, 1, 12, 30, tzinfo=timezone.utc)

    backend.schedule_campaign("10", schedule_time)

    send_call = cast(FakeSession, backend.session).calls[1]
    assert send_call["json"]["scheduled_at"] == "2024-10-01 12:30:00"


def test_schedule_campaign_requires_timezone(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [],
    )
    with pytest.raises(CampaignBackendError):
        backend.schedule_campaign("10", datetime(2024, 10, 1, 12, 30))


def test_unschedule_campaign_not_supported(mailrelay_settings):
    backend = make_backend(mailrelay_settings, [])

    with pytest.raises(CampaignBackendError):
        backend.unschedule_campaign("1")


def test_send_test_email_error(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [make_response({"message": "boom"}, status=400)],
    )

    with pytest.raises(CampaignBackendError):
        backend.send_test_email(campaign_id="10", email="test@example.com")


def test_get_audiences_converts_ids_to_strings(mailrelay_settings):
    backend = make_backend(
        mailrelay_settings,
        [make_response([{"id": 1, "name": "One", "subscribers_count": 5}])],
    )

    audiences = backend.get_audiences()

    assert isinstance(audiences[0].id, str)

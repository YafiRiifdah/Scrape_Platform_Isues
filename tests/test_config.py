import textwrap

import pytest
from pydantic import ValidationError

from spi.config import AppConfig, load_config


MINIMAL_YAML = textwrap.dedent(
    """
    accounts:
      - platform: instagram
        username: kompascom
        label: Kompas.com
      - platform: tiktok
        username: detikcom
        label: detikcom
    """
)


def test_load_config_applies_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")

    cfg = load_config(path)

    assert len(cfg.accounts) == 2
    assert cfg.scrape.days == 7
    assert cfg.scrape.max_comments_per_post == 200
    assert cfg.scrape.delay_between_requests == (2.0, 6.0)
    assert cfg.analysis.per_post_model == "claude-haiku-5-5"
    assert cfg.analysis.report_model == "claude-opus-5-5"
    assert cfg.storage.db_path == "data/spi.sqlite"


def test_account_adapter_defaults_by_platform():
    cfg = AppConfig.model_validate(
        {
            "accounts": [
                {"platform": "instagram", "username": "a", "label": "A"},
                {"platform": "tiktok", "username": "b", "label": "B"},
                {"platform": "tiktok", "username": "c", "label": "C", "adapter": "tiktokapi"},
            ]
        }
    )
    assert cfg.accounts[0].adapter == "instaloader"
    assert cfg.accounts[1].adapter == "tiktok_playwright"
    assert cfg.accounts[2].adapter == "tiktokapi"


def test_account_key_combines_platform_and_username():
    cfg = AppConfig.model_validate(
        {"accounts": [{"platform": "instagram", "username": "kompascom", "label": "K"}]}
    )
    assert cfg.accounts[0].key == "instagram:kompascom"


def test_config_requires_at_least_one_account():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"accounts": []})


def test_delay_range_must_be_ascending():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "accounts": [{"platform": "instagram", "username": "a", "label": "A"}],
                "scrape": {"delay_between_requests": [6, 2]},
            }
        )

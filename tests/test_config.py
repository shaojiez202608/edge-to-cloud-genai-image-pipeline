import json

import pytest

from edge_pipeline.config import PipelineConfig


def test_defaults_valid_and_round_trip():
    cfg = PipelineConfig()
    assert PipelineConfig.from_dict(json.loads(cfg.to_json())) == cfg


@pytest.mark.parametrize("change", [
    {"model_mode": "other"}, {"model_id": "not a model"}, {"max_tokens": 10}, {"temperature": 2},
    {"temperature": True}, {"allowed_content_types": ["image/gif"]}, {"copy_locale": "fr-FR"},
    {"categories": ["kitchen"]}, {"model_image_side": 2048, "max_image_side": 1024}, {"unknown_key": 1},
])
def test_invalid_values_rejected(change):
    with pytest.raises(ValueError):
        PipelineConfig.from_dict(change)


def test_from_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_CONFIG", json.dumps({"model_mode": "mock", "copy_locale": "zh-CN"}))
    assert PipelineConfig.from_env().copy_locale == "zh-CN"

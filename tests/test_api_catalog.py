"""OpenAPI parsing and card rendering, against an invented spec."""

from __future__ import annotations

import json

import pytest

from akamai_cloud_docs_mcp.core.api_catalog import (
    ApiCatalogError,
    build_api_docs,
    cli_commands,
    collect_parameters,
    oauth_scopes,
    parse_spec,
    render_body,
)


@pytest.fixture
def docs(fake_spec) -> dict[str, dict]:
    return {doc["id"]: doc for doc in parse_spec(fake_spec)}


class TestParseSpec:
    def test_one_document_per_operation(self, docs):
        assert set(docs) == {
            "POST /widgets",
            "GET /widgets",
            "GET /widgets/{widgetId}",
            "DELETE /widgets/{widgetId}",
            "POST /internal/rebuild",
        }

    def test_api_version_prefix_is_stripped_from_the_id(self, docs):
        assert "POST /widgets" in docs
        assert not any(key.startswith("POST /{apiVersion}") for key in docs)

    def test_document_shape(self, docs):
        doc = docs["POST /widgets"]
        assert doc["kind"] == "api"
        assert doc["title"] == "Create a widget"
        assert doc["operation_id"] == "post-widgets"
        assert doc["tags"] == ["Widgets"]
        assert doc["url"].endswith("/reference/post-widgets")
        assert doc["card"].startswith("## POST /widgets")

    def test_empty_paths_raises(self):
        with pytest.raises(ApiCatalogError):
            parse_spec({"paths": "nope"})


class TestCliMapping:
    def test_command_comes_from_the_path_item_and_action_from_the_operation(self, docs):
        assert docs["POST /widgets"]["cli"] == "linode-cli widgets create"

    def test_action_list_yields_aliases(self, docs):
        doc = docs["GET /widgets"]
        assert doc["cli"] == "linode-cli widgets list"
        assert doc["cli_aliases"] == ["linode-cli widgets ls"]

    def test_skip_flag_suppresses_the_command(self, docs):
        assert "cli" not in docs["POST /internal/rebuild"]

    def test_missing_action_yields_no_command(self, docs):
        assert "cli" not in docs["DELETE /widgets/{widgetId}"]

    def test_cli_commands_helper_handles_a_missing_command(self):
        assert cli_commands({}, {"x-linode-cli-action": "create"}) == []


class TestParameters:
    def test_path_and_operation_parameters_merge(self, fake_spec):
        path_item = fake_spec["paths"]["/{apiVersion}/widgets"]
        merged = collect_parameters(path_item, path_item["get"])
        assert {p["name"] for p in merged} == {"page", "page_size"}

    def test_api_version_parameter_is_dropped(self, fake_spec):
        path_item = fake_spec["paths"]["/{apiVersion}/widgets"]
        merged = collect_parameters(path_item, path_item["get"])
        assert all(p["name"] != "apiVersion" for p in merged)

    def test_rendered_into_the_card(self, docs):
        card = docs["GET /widgets"]["card"]
        assert "- page (integer, optional): The page of a collection to return." in card

    def test_path_parameter_is_marked_required(self, docs):
        assert "- widgetId (integer, required)" in docs["GET /widgets/{widgetId}"]["card"]


class TestBodyRendering:
    def test_required_fields_carry_type_and_description(self, docs):
        card = docs["POST /widgets"]["card"]
        assert "- label (string, required): A name for the widget." in card
        assert "- size (string, required)" in card

    def test_optional_fields_are_listed_by_name_only(self, docs):
        card = docs["POST /widgets"]["card"]
        assert "- optional: tags, notes" in card
        assert "notes about the widget" not in card

    def test_nested_object_expands_one_level(self, docs):
        card = docs["POST /widgets"]["card"]
        assert "- placement (object, required)" in card
        assert "  - region (string, required)" in card

    def test_nesting_stops_at_depth_two(self, docs):
        # `placement.coordinates` is a third level, so it collapses to a count.
        card = docs["POST /widgets"]["card"]
        assert "- coordinates (object, required, 2 fields)" in card
        assert "latitude" not in card

    def test_no_body_renders_nothing(self, docs):
        assert "Body" not in docs["GET /widgets"]["card"]

    def test_empty_schema(self):
        assert render_body({}) == []
        assert render_body({"type": "object"}) == []


class TestCardContent:
    def test_response_schemas_are_never_included(self, docs):
        for doc in docs.values():
            assert "Responses" not in doc["card"]
            assert "widget_id_returned" not in doc["card"]

    def test_scopes_are_listed(self, docs):
        assert "OAuth scopes: widgets:read_write" in docs["POST /widgets"]["card"]

    def test_reference_url_is_present(self, docs):
        assert "Reference: https://techdocs.akamai.com/linode-api/reference/post-widgets" in (
            docs["POST /widgets"]["card"]
        )

    def test_cards_stay_small(self, docs):
        for doc in docs.values():
            assert len(doc["card"]) < 2048, f"{doc['id']} card is {len(doc['card'])} bytes"

    def test_oauth_scopes_helper_deduplicates(self):
        operation = {"security": [{"a": ["x:rw"]}, {"b": ["x:rw", "y:ro"]}]}
        assert oauth_scopes(operation) == ["x:rw", "y:ro"]


class TestSearchText:
    def test_includes_path_summary_cli_and_field_names(self, docs):
        text = docs["POST /widgets"]["text"]
        for fragment in ["POST /widgets", "Create a widget", "linode-cli widgets create", "label"]:
            assert fragment in text

    def test_excludes_the_card_prose(self, docs):
        assert "OAuth scopes" not in docs["POST /widgets"]["text"]


class TestBuildApiDocs:
    def test_returns_documents_and_version(self, fake_spec):
        docs, version = build_api_docs(
            fetcher=lambda url, **kwargs: json.dumps(fake_spec),
            min_operations=5,
            quiet=True,
        )
        assert len(docs) == 5
        assert version == "4.215.0"

    def test_guard_rejects_a_thin_spec(self, fake_spec):
        with pytest.raises(ApiCatalogError, match="expected at least"):
            build_api_docs(
                fetcher=lambda url, **kwargs: json.dumps(fake_spec),
                min_operations=400,
                quiet=True,
            )

    def test_invalid_json_is_reported_clearly(self):
        with pytest.raises(ApiCatalogError, match="not valid JSON"):
            build_api_docs(fetcher=lambda url, **kwargs: "{nope", min_operations=1, quiet=True)

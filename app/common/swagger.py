"""Swagger helpers for the whole API."""

from fastapi import FastAPI


def enable_swagger_file_picker(app: FastAPI) -> None:
    """OpenAPI 3.1 marks uploads as `contentMediaType`, which Swagger UI renders as text inputs.
    Adding `format: binary` restores the "Choose file" button on every upload endpoint."""
    default_openapi = app.openapi

    def _patch(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and "contentMediaType" in node:
                node["format"] = "binary"
            for value in node.values():
                _patch(value)
        elif isinstance(node, list):
            for item in node:
                _patch(item)

    def custom_openapi() -> dict:
        if app.openapi_schema is None:
            _patch(default_openapi())
        return app.openapi_schema

    app.openapi = custom_openapi

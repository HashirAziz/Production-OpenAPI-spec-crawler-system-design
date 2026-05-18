"""
conftest.py — Shared pytest fixtures for the OpenAPI Spec Crawler test suite.
"""

from __future__ import annotations

import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Sample spec fixtures
# ---------------------------------------------------------------------------

OPENAPI_3_YAML = b"""
openapi: "3.0.3"
info:
  title: Pet Store API
  version: "1.2.0"
  description: A sample API for testing.
servers:
  - url: https://api.petstore.example.com/v1
tags:
  - name: pets
    description: Everything about pets
  - name: store
    description: Access to store orders
paths:
  /pets:
    get:
      summary: List all pets
      tags: [pets]
  /pets/{petId}:
    get:
      summary: Info for a specific pet
      tags: [pets]
  /store/orders:
    post:
      summary: Place an order
      tags: [store]
"""

SWAGGER_2_YAML = b"""
swagger: "2.0"
info:
  title: Legacy API
  version: "2.3.1"
  description: A Swagger 2.0 spec.
host: api.legacy.example.com
basePath: /v2
schemes:
  - https
  - http
tags:
  - name: users
paths:
  /users:
    get:
      summary: List users
  /users/{id}:
    get:
      summary: Get user by ID
"""

OPENAPI_3_JSON = b"""
{
  "openapi": "3.1.0",
  "info": {
    "title": "JSON API",
    "version": "0.1.0"
  },
  "paths": {
    "/health": {},
    "/metrics": {}
  }
}
"""

MALFORMED_YAML = b"this: is: not: valid: yaml: :::"

EMPTY_BYTES = b""

NOT_A_SPEC = b"""
openapi: "3.0.0"
# Missing info block entirely
paths: {}
"""

NUMERIC_VERSION_YAML = b"""
openapi: "3.0.0"
info:
  title: Numeric Version
  version: 2
paths:
  /foo: {}
"""


# ---------------------------------------------------------------------------
# Temporary data directory fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Return a temporary data directory with history/ subdirectory."""
    (tmp_path / "history").mkdir()
    return tmp_path
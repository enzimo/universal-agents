"""Generate schemas from the explicit transport and workflow model catalog."""

import importlib
import json
from enum import Enum

from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import models_json_schema


MODEL_MODULES = (
    "custom_messages.contracts.routing_v1",
    "custom_messages.contracts.attachments_v1",
    "custom_messages.contracts.control_v1",
    "custom_messages.contracts.task_v1",
    "custom_messages.contracts.activity_v1",
    "custom_messages.contracts.credentials_v1",
    "custom_messages.contracts.schedule_v1",
    "custom_messages.contracts.delivery_v1",
    "custom_messages.contracts.envelope_v1",
    "custom_messages.records.plans",
    "custom_messages.records.objectives",
    "custom_messages.records.reviews",
)


def generate_all_schemas(target_class: type | None = None) -> str:
    if target_class is not None:
        if not issubclass(target_class, (BaseModel, Enum)):
            raise ValueError(f"Unsupported schema type: {target_class.__name__}")
        return json.dumps(TypeAdapter(target_class).json_schema(), indent=2)
    models = []
    for module_name in MODEL_MODULES:
        module = importlib.import_module(module_name)
        models.extend(
            (model, "validation")
            for model in vars(module).values()
            if isinstance(model, type)
            and issubclass(model, BaseModel)
            and model.__module__ == module_name
        )
    mapping, schema = models_json_schema(models, title="MessagingSchemas")
    return json.dumps(
        {
            "description": "Unified Messaging JSON Schema",
            "$defs": schema.get("$defs", {}),
            "top_level_models": {
                model.__name__: {"$ref": reference["$ref"]}
                for (model, _), reference in mapping.items()
            },
        },
        indent=2,
    )

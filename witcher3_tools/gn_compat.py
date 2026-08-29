"""Geometry Nodes modifier inputs: ID-properties before Blender 5.2, ``modifier.properties.inputs`` from 5.2."""


def _inputs(mod):
    return getattr(getattr(mod, "properties", None), "inputs", None)


def gn_input_identifiers(mod):
    inputs = _inputs(mod)
    if inputs is not None:
        return list(inputs.keys())
    try:
        return [key for key in mod.keys() if not key.endswith(("_use_attribute", "_attribute_name"))]
    except Exception:
        return []


def gn_input_get(mod, identifier, default=None):
    inputs = _inputs(mod)
    try:
        if inputs is not None:
            return getattr(inputs, identifier).value
        return mod[identifier]
    except Exception:
        return default


def gn_input_set(mod, identifier, value):
    inputs = _inputs(mod)
    if inputs is not None:
        getattr(inputs, identifier).value = value
    else:
        mod[identifier] = value
